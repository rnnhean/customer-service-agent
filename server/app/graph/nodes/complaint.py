"""投诉受理节点 —— 父图节点，内部跑 complaint_agent 子图。

Agent 负责：判断分类和优先级、决定要不要先查历史投诉、组织共情话术。
代码负责：**工单号和 SLA 时限只能由代码写入**。

为什么要这么切：模型写出来的单号看起来特别真（CP20240415-88273 这种），
但它是编的——用户拿着这个号去查，什么也查不到。所以单号一律由
``create_complaint`` 工具生成，任何出现在模型输出里的「单号」字样连同
那句话一起抹掉，正文末尾用代码拼上真实单号。
"""

from __future__ import annotations

import re

from langchain_core.messages import AIMessage, HumanMessage

from app.graph.agents import complaint_agent, final_ai_text, tool_payloads, tools_used_in
from app.graph.state import AgentState
from app.graph.context import last_user_text

#: 模型爱自己编单号（形如 CP20240415-88273 / TK123456789）。
#: 单号只能由代码生成，任何出现在模型输出里的一律抹掉。
_FAKE_ID_RE = re.compile(r"[A-Z]{2,4}\d{6,}(?:-\d+)?")


def _strip_ticket_lines(text: str) -> str:
    """抹掉模型编造的单号，以及提到单号的那句话。"""
    kept = []
    for line in text.splitlines():
        if "工单号" in line or "单号" in line or _FAKE_ID_RE.search(line):
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    return cleaned or "我们已将您反映的问题记录在案。"


async def complaint_node(state: AgentState) -> dict:
    history = state.get("messages") or []
    member_id = state.get("member_id")
    text = last_user_text(history)

    if not member_id:
        return {
            "messages": [
                AIMessage("很抱歉给您带来不好的体验。请提供会员 ID，我马上为您建单处理。")
            ],
            "degraded": True,
        }

    # ---- 1) 跑 Agent 子图：自己决定分类 / 优先级 / 要不要查历史投诉 ----
    agent = complaint_agent(member_id, state.get("profile"))
    query = HumanMessage(
        f"{text}\n\n（补充信息：投诉人会员 ID 为 {member_id}，调用工具时直接用，不要向用户索要）"
    )
    try:
        result = await agent.ainvoke(
            {"messages": [*history[-6:], query]},
            config={"recursion_limit": 12},
        )
    except Exception as e:
        return {
            "messages": [AIMessage("投诉受理服务暂时不可用，我帮您转人工处理，很抱歉。")],
            "degraded": True,
            "error": f"agent_failed: {e}",
        }

    agent_messages = result.get("messages") or []
    body = final_ai_text(agent_messages)
    tools_used = [*state.get("tools_used", []), *tools_used_in(agent_messages)]

    # ---- 2) 取工单：Agent 没建单就由代码兜底建一张 ----
    ticket = _find_ticket(agent_messages)
    if ticket is None:
        from app.tools.complaint_tools import create_complaint

        category, priority = _fallback_classify(text, state)
        created = await create_complaint.ainvoke(
            {
                "member_id": member_id,
                "category": category,
                "priority": priority,
                "summary": text[:50],
            }
        )
        if not created.get("ok"):
            return {
                "messages": [AIMessage("工单创建失败，我帮您转人工处理，很抱歉。")],
                "tools_used": [*tools_used, "create_complaint"],
                "degraded": True,
                "error": created.get("error"),
            }
        ticket = created
        tools_used.append("create_complaint")

    ticket_id = ticket["ticket_id"]
    hours = ticket.get("sla_hours") or _sla_hours(ticket.get("priority"))

    # ---- 3) 关键事实由代码拼接，模型编的单号先抹掉 ----
    tail = f"\n\n工单号：{ticket_id}\n我们承诺在 {hours} 小时内首次回复您。"
    answer = _strip_ticket_lines(body) + tail

    return {
        "messages": [AIMessage(answer)],
        "tools_used": tools_used,
        "slots": {
            **(state.get("slots") or {}),
            "ticket_id": ticket_id,
            "category": ticket.get("category") or "其他",
        },
        "tokens_out": state.get("tokens_out", 0) + len(answer),
    }


def _find_ticket(messages: list) -> dict | None:
    """从子图的工具返回值里找建单结果。后出现的优先（用户可能要建第二张）。"""
    found: dict | None = None
    for payload in tool_payloads(messages):
        if payload.get("ok") and payload.get("ticket_id"):
            found = payload
    return found


def _sla_hours(priority: int | None) -> int:
    from app.tools.complaint_tools import SLA_HOURS

    return SLA_HOURS.get(int(priority or 3), 24)


def _fallback_classify(text: str, state: AgentState) -> tuple[str, int]:
    """Agent 没建单时的兜底分类。规则写死在代码里，不交给模型。"""
    if any(k in text for k in ("曝光", "12315", "媒体", "起诉", "法律", "工信部", "消费者协会")):
        priority = 1
    elif any(k in text for k in ("第三次", "很多次", "一直没", "很久没", "反复", "又来了")):
        priority = 2
    else:
        priority = 3

    rules = [
        ("服务态度", ("态度", "骂", "凶", "不理", "敷衍", "挂我电话")),
        ("物流问题", ("快递", "物流", "发货", "没到", "签收", "配送")),
        ("商品质量", ("质量", "破损", "坏了", "假货", "瑕疵", "有问题")),
        ("退款纠纷", ("退款", "不退", "钱没", "扣款", "少退")),
    ]
    for category, kws in rules:
        if any(k in text for k in kws):
            return category, priority
    return "其他", priority
