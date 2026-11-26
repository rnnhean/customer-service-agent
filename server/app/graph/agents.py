"""四个业务 Agent 的子图定义 —— 本项目的「多 Agent」就在这里成立。

**为什么是子图而不是普通节点**：
每个 ``create_react_agent`` 都是**独立的推理循环**（think → act → observe → think），
模型自己决定调几次工具、调哪个、什么时候收尾。这才是 Agent；
「固定调一次工具 + 一次生成」的节点只是工作流。

**工具集互斥**：每个 Agent 只拿到自己那一组工具。如果四个 Agent 共用一套工具，
那就只是同一个 Agent 换了四次提示词，面试时会被问穿。

**与父图的关系**：
- 父图负责路由（classify）、后置校验、持久化；
- 子图只负责「按提示词 + 工具，自己把问题查清楚并给出回答」；
- 校验不过时由父图节点替换成兜底话术 —— 推理交给 Agent，事实边界交给代码。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from app.graph.context import render_memory
from app.graph.prompts import BENEFIT_PROMPT, COMPLAINT_PROMPT, ORDER_PROMPT, REFUND_PROMPT
from app.llm import get_llm
from app.tools import BENEFIT_TOOLS, COMPLAINT_TOOLS, ORDER_TOOLS, REFUND_TOOLS

#: 每个 Agent 的推理循环上限。超过就强制收尾，防止死循环烧 token。
MAX_ITERATIONS = 3


def _facts_text(member_id: str | None, profile: dict | None, level: str | None = None) -> str:
    """把「这个用户是谁」拼成一段系统提示，Agent 就不用反复问。

    Agent 拿不到 member_id 会向用户追问，而客服场景用户不该被问内部 ID。
    **等级也必须注入**：只注入 member_id 而不注入等级，权益 Agent 会照着
    「等级不明确就先问清」的提示词反问用户——而这个信息我们明明已经有。
    """
    blocks: list[str] = []
    if member_id:
        blocks.append(
            f"当前用户会员 ID：{member_id}。"
            "调用任何需要 member_id 的工具时直接用这个值，不要向用户索要。"
        )
    if level:
        blocks.append(
            f"当前用户会员等级：{level}。"
            "**已知信息，不要向用户确认等级**，检索时直接把 level 传进工具。"
        )
    mem = render_memory(profile)
    if mem:
        blocks.append("以下是该用户的历史背景，仅作参考：\n" + mem)
    return "\n\n".join(blocks)


def _build(
    name: str,
    prompt: str,
    tools: list,
    member_id: str | None,
    profile: dict | None,
    level: str | None = None,
):
    """按「提示词 + 工具集 + 身份注入」装配一个 ReAct 子图。

    **不缓存**：身份和记忆是每次请求的动态数据，缓存实例会串号。
    ``create_react_agent`` 本身很轻，只是把 nodes/edges 连起来，真正开销在 LLM 调用。

    ``prompt`` 只接受 SystemMessage / str / 回调，**不接受 list**（会抛
    ``Got unexpected type for prompt``）。所以身份和记忆直接拼进同一个系统消息里。
    """
    prompt_text = prompt
    extra = _facts_text(member_id, profile, level)
    if extra:
        prompt_text = prompt + "\n\n" + extra
    return create_react_agent(
        get_llm(),
        tools,
        prompt=SystemMessage(prompt_text),
        name=name,
    )


def benefit_agent(member_id: str | None, profile: dict | None = None, level: str | None = None):
    return _build("benefit_agent", BENEFIT_PROMPT, BENEFIT_TOOLS, member_id, profile, level)


def order_agent(member_id: str | None, profile: dict | None = None):
    return _build("order_agent", ORDER_PROMPT, ORDER_TOOLS, member_id, profile)


def refund_agent(member_id: str | None, profile: dict | None = None):
    return _build("refund_agent", REFUND_PROMPT, REFUND_TOOLS, member_id, profile)


def complaint_agent(member_id: str | None, profile: dict | None = None):
    return _build("complaint_agent", COMPLAINT_PROMPT, COMPLAINT_TOOLS, member_id, profile)


AGENT_BUILDERS = {
    "benefit_qa": benefit_agent,
    "order_status": order_agent,
    "change_request": refund_agent,
    "complaint": complaint_agent,
}


def final_ai_text(messages: list) -> str:
    """子图跑完后，取最后一条 AI 消息的正文（跳过纯 tool_call 消息）。"""
    for m in reversed(messages or []):
        if not isinstance(m, AIMessage):
            continue
        content = m.content
        text = content if isinstance(content, str) else str(content)
        if text.strip():
            return text
    return ""


def tool_payloads(messages: list) -> list[dict]:
    """回收子图里工具的真实返回值。

    **注意 content 是 JSON 字符串不是 dict**（ToolNode 会把 dict 序列化）。
    直接判 ``isinstance(content, dict)`` 会全部漏掉，导致事实校验拿到空集合，
    进而把正确回答误判成幻觉。这一点用真跑子图确认过。
    """
    import json

    payloads: list[dict] = []
    for m in messages or []:
        if not isinstance(m, ToolMessage):
            continue
        content = m.content
        if isinstance(content, dict):
            payloads.append(content)
        elif isinstance(content, str):
            try:
                parsed = json.loads(content)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                payloads.append(parsed)
    return payloads


def tools_used_in(messages: list) -> list[str]:
    """从子图消息流里抽出实际被调用的工具名（有调用才记，不是工具集清单）。"""
    names: list[str] = []
    for m in messages or []:
        for call in getattr(m, "tool_calls", None) or []:
            n = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if n:
                names.append(n)
    return names


__all__ = [
    "AGENT_BUILDERS",
    "MAX_ITERATIONS",
    "benefit_agent",
    "complaint_agent",
    "final_ai_text",
    "order_agent",
    "refund_agent",
    "tool_payloads",
    "tools_used_in",
]
