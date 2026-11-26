"""退款/变更链路 —— 拆成两个节点，这是本项目的核心设计之一。

``refund_approval``   ：跑 refund_agent 子图，由 Agent 判断资格、组织话术、发起申请
``refund_wait``       ：纯粹的 interrupt 挂起点 + 恢复后重查 + 执行退款

**为什么必须拆开**：
LangGraph 的 ``interrupt()`` 恢复时，**节点函数是从头重跑的**（走到 interrupt 那行才
拿到 resume 值返回）。如果 Agent 和 interrupt 在同一个节点里，恢复时 Agent 会
第二遍执行、第二遍调用 ``submit_refund_request``，数据库里就多出一张审批单。
把它拆到独立节点后，恢复时只重跑那个无 LLM、无副作用的等待节点。

**第二个必须遵守的顺序**：审批单必须在 ``interrupt()`` 之前落库。
反过来的话，中断发生、进程重启，审批台里什么都没有，人工无从处理。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import interrupt

from app.config import settings
from app.db import get_conn, repo
from app.graph.agents import final_ai_text, refund_agent, tool_payloads, tools_used_in
from app.graph.state import AgentState


def _yuan(fen: int) -> str:
    return f"{fen / 100:.2f}"


async def refund_approval_node(state: AgentState) -> dict:
    """Agent 判断 + 发起申请。**不做 interrupt**。"""
    history = state.get("messages") or []
    slots = state.get("slots") or {}
    member_id = state.get("member_id")
    order_id = slots.get("order_id")

    if not member_id:
        return {
            "messages": [AIMessage("请先告诉我您的会员 ID，我需要核实身份后再处理退款。")],
            "degraded": True,
        }

    # 没给订单号：直接列可退款订单让用户选，不值得启动 Agent
    if not order_id:
        orders = await repo.get_orders(member_id, limit=5)
        refundable = [o for o in orders if o.get("status") == "EFFECTIVE"]
        if not refundable:
            return {
                "messages": [
                    AIMessage("您名下没有可退款的订单。如果是其他问题，可以告诉我具体情况。")
                ],
            }
        lines = ["请问是哪一笔订单？"]
        for o in refundable:
            lines.append(f"- {o['order_id']}  金额 {_yuan(o['amount'])} 元")
        return {"messages": [AIMessage("\n".join(lines))]}

    # ---- 前置快检：状态不对就别让 Agent 白跑一轮 ----
    order = await repo.get_order(order_id)
    if not order:
        return {"messages": [AIMessage(f"没有查到订单 {order_id}，请核对一下订单号。")]}
    if order["status"] != "EFFECTIVE":
        return {
            "messages": [
                AIMessage(
                    f"订单 {order_id} 当前状态是「{order['status']}」，无法发起退款。"
                    "如果是其他问题，我可以帮您转人工。"
                )
            ],
        }

    amount = int(order["amount"])
    reason = slots.get("reason", "用户申请退款")

    # ---- 跑 Agent 子图：它自己决定要不要再查一次、怎么跟用户解释 ----
    agent = refund_agent(member_id, state.get("profile"))
    query = HumanMessage(
        f"{_last_user(history)}\n\n"
        f"（补充信息：会员 ID {member_id}；订单 {order_id}，状态 EFFECTIVE，"
        f"实付 {_yuan(amount)} 元（{amount} 分）。用户给的退款原因：{reason}）"
    )
    try:
        result = await agent.ainvoke(
            {"messages": [*history[-6:], query]},
            config={"recursion_limit": 12},
        )
    except Exception as e:
        return {
            "messages": [AIMessage("退款申请服务暂时不可用，我帮您转人工处理。")],
            "degraded": True,
            "error": f"agent_failed: {e}",
        }

    agent_messages = result.get("messages") or []
    answer = final_ai_text(agent_messages)
    tools_used = [*state.get("tools_used", []), *tools_used_in(agent_messages)]

    # ---- 回收审批单：Agent 调过 submit_refund_request 就复用它 ----
    apply = _find_approval(agent_messages)
    if apply is None:
        # Agent 没建单（可能只是在解释流程）——由代码补建，保证审批台一定有单可看
        expires_at = datetime.now(UTC) + timedelta(hours=settings.approval_ttl_hours)
        try:
            apply = await repo.create_approval(
                thread_id=state.get("thread_id", ""),
                member_id=member_id,
                type="refund",
                amount=amount,
                reason=reason,
                expires_at=expires_at,
            )
        except Exception as e:
            return {
                "messages": [AIMessage("审批单创建失败，我帮您转人工处理。")],
                "tools_used": tools_used,
                "degraded": True,
                "error": f"create_approval_failed: {e}",
            }
        tools_used.append("create_approval")

    return {
        "messages": [AIMessage(answer or "已为您提交退款申请，等待人工审批。")],
        "approval_id": apply["apply_id"],
        "tools_used": tools_used,
        "tokens_out": state.get("tokens_out", 0) + len(answer or ""),
    }


async def refund_wait_node(state: AgentState) -> dict:
    """挂起点。恢复后重查订单再执行退款。

    **这个节点里不要放 LLM、不要放写操作的准备逻辑** —— 它会被重跑。
    """
    apply_id = state.get("approval_id")
    if not apply_id:
        return {"messages": [AIMessage("审批单状态异常，请重新提交申请。")], "degraded": True}

    apply = await repo.get_approval(apply_id)
    if not apply:
        return {"messages": [AIMessage("审批单状态异常，请重新提交申请。")], "degraded": True}

    order_id = (state.get("slots") or {}).get("order_id", "")
    amount = int(apply.get("amount") or 0)
    summary = (
        f"退款申请：订单 {order_id}，会员 {apply.get('member_id')}，"
        f"金额 {_yuan(amount)} 元，原因：{apply.get('reason') or '未说明'}"
    )

    # ---- 挂起，把现场交给人工 ----
    decision = interrupt(
        {
            "type": "approval_required",
            "apply_id": apply_id,
            "amount": amount,
            "summary": summary,
            "expires_at": apply["expires_at"],
        }
    )

    # ================= 以下只有 resume 之后才会执行 =================
    approved = bool(decision.get("approved")) if isinstance(decision, dict) else False
    comment = (decision.get("comment") or "") if isinstance(decision, dict) else ""

    if not approved:
        reason_text = f"原因：{comment}" if comment else ""
        return {
            "messages": [
                AIMessage(
                    f"您的退款申请（单号 {apply_id}）未通过审批。{reason_text}\n"
                    "如有疑问可以联系人工客服进一步沟通。"
                )
            ]
        }

    # ---- 恢复后重查：审批可能隔了几小时，订单状态早就变了 ----
    fresh = await repo.get_order(order_id)
    if not fresh:
        return {
            "messages": [AIMessage(f"订单 {order_id} 已不存在，退款无法执行，请联系人工核实。")],
            "degraded": True,
        }
    if fresh["status"] != "EFFECTIVE":
        return {
            "messages": [
                AIMessage(
                    f"订单状态已变更为「{fresh['status']}」，无需重复退款。"
                    "如果这不是您预期的结果，我帮您转人工处理。"
                )
            ],
            "degraded": True,
        }

    # ---- 执行退款（这里只是改状态，真实系统应该是调支付网关）----
    try:
        await _do_refund(order_id)
    except Exception as e:
        return {
            "messages": [AIMessage("退款执行失败，已为您转人工处理，请稍候。")],
            "degraded": True,
            "error": f"refund_failed: {e}",
        }

    return {
        "messages": [
            AIMessage(
                f"退款已提交（单号 {apply_id}），金额 {_yuan(amount)} 元，"
                "预计 1-3 个工作日原路退回。请注意查收。"
            )
        ]
    }


async def _do_refund(order_id: str) -> None:
    """把订单置为已退款。真实项目这里是调支付网关 + 发消息。"""
    async with get_conn() as conn:
        await conn.execute(
            "UPDATE orders SET status = 'REFUNDED', updated_at = %s WHERE order_id = %s",
            (datetime.now(UTC), order_id),
        )
        await conn.commit()


def _find_approval(messages: list) -> dict | None:
    """从子图工具返回值里找已创建的审批单（content 是 JSON 字符串，统一解析）。"""
    found: dict | None = None
    for payload in tool_payloads(messages):
        if payload.get("ok") and payload.get("apply_id"):
            found = payload
    return found


def _last_user(history: list) -> str:
    for m in reversed(history):
        if isinstance(m, HumanMessage) and isinstance(m.content, str):
            return m.content
    return "我要退款"
