"""订单查询节点 —— 父图节点，内部跑 order_agent 子图。

**这里踩过的坑必须写下来**：
最初是「固定调一次工具 + 一次 LLM 生成」，而且生成时**工具结果压根没进 prompt**，
模型就凭空编了一套「2024-03-15 / ¥299 / 已生效」——时间、金额、状态三项全错，
读起来完全像真的。这个 bug 说明两件事：

1. 没有事实注入的 LLM 调用，输出和随机数没区别；
2. 「流式输出看着正常」不代表内容正确，必须有后置校验。

现在的结构：
- ``order_agent`` 子图自己决定查订单还是列最近订单（真正的 Agent 决策）；
- 本节点从子图消息里**回收工具实际返回的订单数据**，做状态/日期/金额三重校验，
  对不上就整段换成工具原文 —— 宁可回答得机械，也不能回答得错误。
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.graph.agents import final_ai_text, order_agent, tool_payloads, tools_used_in
from app.graph.state import AgentState
from app.tools.order_tools import STATUS_CN

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
#: 「¥299」「299 元」「299.00元」这类金额表述
MONEY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)")
#: 出现在回答里的状态词（中文），用于反查是否与工具结果一致
STATUS_WORDS = set(STATUS_CN.values())


def _order_facts(tool_results: list[dict]) -> str:
    """把工具结果渲染成给模型看的事实清单。"""
    lines: list[str] = []
    for r in tool_results:
        if r.get("order"):
            o = r["order"]
            lines.append(
                f"订单号：{o.get('order_id')}\n"
                f"状态：{o.get('status_cn')}（{o.get('status')}）\n"
                f"金额：{o.get('amount_yuan')} 元\n"
                f"下单时间：{o.get('created_at')}\n"
                f"最后更新：{o.get('updated_at')}\n"
                f"类型：{o.get('type_cn')}"
            )
        elif r.get("orders"):
            lines.append(f"该会员共有 {r.get('count', 0)} 条订单记录：")
            for o in r["orders"]:
                lines.append(
                    f"- {o.get('order_id')}｜{o.get('status_cn')}｜"
                    f"{o.get('amount_yuan')} 元｜{o.get('created_at')}｜{o.get('type_cn')}"
                )
    return "\n".join(lines)


def _money_candidates(tool_results: list[dict]) -> set[str]:
    """从工具返回里收集所有「可能被写进回答」的金额，统一成两位小数字符串。

    **不能只在 JSON 文本上跑「数字+元」的正则**：工具返回的是
    ``"amount_yuan": "49.00"``，没有「元」字，正则一条都抓不到，
    导致金额校验集合恒为空、正确回答被判成幻觉。
    """
    out: set[str] = set()

    def add(val: Any) -> None:
        try:
            out.add(f"{float(val):.2f}")
        except (TypeError, ValueError):
            return

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("amount_yuan", "amount"):
                    add(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for r in tool_results:
        walk(r)

    # amount 是「分」，amount_yuan 是「元」——两者都在集合里，
    # 回答写 4900 或 49 都能对上，不会因为单位歧义误杀。
    extra = set()
    for s in out:
        extra.add(s.rstrip("0").rstrip(".") if "." in s else s)
    return out | extra


def _validate_order_answer(answer: str, tool_results: list[dict]) -> tuple[bool, str]:
    """校验回答里的状态 / 日期 / 金额都在工具结果里。

    **校验语料用工具返回的完整 JSON**，不是精选后的摘要。曾经只从
    ``_order_facts`` 的摘要里取日期，模型如实报了 ``updated_at``（工具确实返回了）
    却被判成幻觉，正确回答被替换成机械兜底——校验过严和过松同样是 bug。
    """
    corpus = json.dumps(tool_results, ensure_ascii=False)

    fact_dates = set(DATE_RE.findall(corpus))
    fact_money = _money_candidates(tool_results)

    bad_dates = [d for d in DATE_RE.findall(answer) if d not in fact_dates]
    if bad_dates:
        return False, f"日期不在工具结果中: {bad_dates}"

    for raw in MONEY_RE.findall(answer):
        norm = f"{float(raw):.2f}"
        if norm in fact_money or raw in fact_money:
            continue
        return False, f"金额不在工具结果中: {raw}"

    # 状态词：回答里出现的每个中文状态，必须确实出现在这批工具结果里
    bad_status = [w for w in STATUS_WORDS if w in answer and w not in corpus]
    if bad_status:
        return False, f"状态与工具结果不符: {bad_status}"

    return True, ""


def _fallback_answer(tool_results: list[dict]) -> str:
    """校验不过 / 模型不可用时的保底回答：直接念工具结果。"""
    for r in tool_results:
        if r.get("order"):
            o = r["order"]
            status = STATUS_CN.get(o.get("status", ""), o.get("status", ""))
            return (
                f"订单 {o.get('order_id')} 当前状态是「{status}」，"
                f"金额 {o.get('amount_yuan')} 元，"
                f"创建于 {str(o.get('created_at', ''))[:10]}。"
            )
        if r.get("orders"):
            lines = ["您最近的订单："]
            for o in r["orders"][:5]:
                status = STATUS_CN.get(o.get("status", ""), o.get("status", ""))
                lines.append(f"- {o.get('order_id')}  {status}  {o.get('amount_yuan')} 元")
            return "\n".join(lines)
    return "订单查询服务暂时不可用，请稍后重试。"


async def order_node(state: AgentState) -> dict:
    history = state.get("messages") or []
    slots = state.get("slots") or {}
    member_id = state.get("member_id")
    order_id = slots.get("order_id")
    tools_used = list(state.get("tools_used", []))

    # ---- 前置条件不足：不进 Agent，省一次推理循环 ----
    if not order_id and not member_id:
        return {
            "messages": [AIMessage("请提供订单号，或者告诉我您的会员 ID，我帮您查。")],
            "degraded": True,
        }

    hint = f"用户要查的订单号是 {order_id}。" if order_id else "用户没给订单号，先列出他最近的订单。"
    query = HumanMessage(
        f"{_last_user(history)}\n\n（补充信息：{hint}"
        f"{'会员 ID 为 ' + member_id if member_id else ''}）"
    )

    # ---- 1) 跑 Agent 子图：自己决定调哪个工具、查几次 ----
    agent = order_agent(member_id, state.get("profile"))
    try:
        result = await agent.ainvoke(
            {"messages": [*history[-6:], query]},
            config={"recursion_limit": 12},
        )
    except Exception as e:
        return {
            "messages": [AIMessage("订单查询服务暂时不可用，请稍后重试或转人工。")],
            "tools_used": tools_used,
            "degraded": True,
            "error": f"agent_failed: {e}",
        }

    agent_messages = result.get("messages") or []
    answer = final_ai_text(agent_messages)
    tools_used.extend(tools_used_in(agent_messages))

    # ---- 2) 事实闸：回收工具真实返回值 ----
    tool_results = tool_payloads(agent_messages)
    if not tool_results:
        # Agent 没调任何工具就作答 —— 这正是幻觉的高发形态，直接判不过
        return {
            "messages": [
                AIMessage("我需要先查一下您的订单信息，请确认一下订单号或会员 ID好吗？")
            ],
            "tools_used": tools_used,
            "degraded": True,
            "error": "order_agent_no_tool_call",
        }

    ok, reason = _validate_order_answer(answer, tool_results)
    if not ok:
        answer = _fallback_answer(tool_results)

    return {
        "messages": [AIMessage(answer)],
        "tools_used": tools_used,
        "degraded": not ok,
        "error": f"order_answer_invalid: {reason}" if not ok else None,
        "tokens_out": state.get("tokens_out", 0) + len(answer),
    }


def _last_user(history: list) -> str:
    for m in reversed(history):
        if isinstance(m, HumanMessage) and isinstance(m.content, str):
            return m.content
    return "帮我查一下订单"
