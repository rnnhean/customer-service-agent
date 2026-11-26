"""上下文预算组装。

不是「超了就截断」，是**先分配预算再组装**。
按 token 估，不按条数——一条长订单列表可能顶二十句「好的」。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

# 各部分的 token 预算（粗估：中文 1 字 ≈ 1 token，英文 4 字符 ≈ 1 token）
BUDGET = {
    "system_prompt": 1500,
    "long_memory": 500,
    "retrieved_docs": 2500,
    "messages": 3000,
}

# 对话历史至少保留几条，即使超预算
KEEP_MIN_MESSAGES = 4


def estimate_tokens(text: str) -> int:
    """粗略估 token。中文按字，英文按 4 字符。宁可高估。"""
    if not text:
        return 0
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    other = len(text) - cjk
    return cjk + other // 4 + 1


def message_tokens(msg: BaseMessage) -> int:
    content = msg.content
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += estimate_tokens(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                total += estimate_tokens(part["text"])
        return total
    return 0


def fit_by_budget(text: str, budget: int) -> str:
    """文本超预算就从尾部截。用于记忆、检索结果这类可裁剪内容。"""
    if estimate_tokens(text) <= budget:
        return text
    # 按字符粗截，留 10% 余量
    approx_chars = int(budget * 0.9)
    return text[:approx_chars] + "…"


def fit_docs_by_score(docs: list[dict], budget: int) -> list[dict]:
    """检索结果超预算，从 score 最低的丢。"""
    if not docs:
        return []

    def doc_tokens(d: dict) -> int:
        return estimate_tokens(str(d.get("name", "")) + str(d.get("value", ""))) + 10

    sorted_docs = sorted(docs, key=lambda d: d.get("score", 0), reverse=True)
    kept: list[dict] = []
    used = 0
    for d in sorted_docs:
        cost = doc_tokens(d)
        if used + cost > budget:
            break
        kept.append(d)
        used += cost
    return kept


def trim_history(
    messages: list[BaseMessage],
    budget: int,
    keep_min: int = KEEP_MIN_MESSAGES,
) -> list[BaseMessage]:
    """对话历史超预算，从最旧的丢，但至少保留 keep_min 条。"""
    if not messages:
        return []

    total = sum(message_tokens(m) for m in messages)
    if total <= budget:
        return list(messages)

    kept: list[BaseMessage] = []
    used = 0
    # 从最新往前收
    for msg in reversed(messages):
        cost = message_tokens(msg)
        if used + cost > budget and len(kept) >= keep_min:
            break
        kept.append(msg)
        used += cost

    kept.reverse()
    return kept


def render_docs(docs: list[dict]) -> str:
    """把检索结果渲染成给模型看的文本。"""
    if not docs:
        return ""
    lines = ["【检索到的权益条款】"]
    for d in docs:
        lines.append(
            f"[权益-{d.get('code', '?')}] {d.get('name', '')}：{d.get('value', '')}"
            f"（等级 {d.get('level', '-')}，"
            f"{'可叠加' if d.get('stackable') else '不可叠加'}，"
            f"有效期 {d.get('expire_days', '-')} 天）"
        )
    lines.append("")
    lines.append("回答时必须引用上面的编号，格式 [权益-编号]。检索结果里没有的不要编。")
    return "\n".join(lines)


def render_memory(profile: dict | None) -> str:
    """渲染长期记忆。只放稳定偏好，不放订单号这类会变的东西。"""
    if not profile:
        return ""
    items = profile.get("items") or []
    if not items:
        return ""
    lines = []
    for it in items[-10:]:  # 最多 10 条，防止无限膨胀
        kind = it.get("kind", "preference")
        content = it.get("content", "")
        if content:
            lines.append(f"- [{kind}] {content}")
    return "\n".join(lines)


def assemble_messages(
    system_prompt: str,
    history: list[BaseMessage],
    retrieved: list[dict] | None = None,
    profile: dict | None = None,
    facts: str | None = None,
) -> list[BaseMessage]:
    """按预算组装最终送给模型的消息列表。

    优先级：系统提示词 > 本轮工具结果 > 检索结果 > 对话历史 > 长期记忆
    系统提示词永不裁。

    ``facts`` 是本轮工具查询到的**事实**（订单状态、工单号等）。不传的事实
    模型就会自己编——这是订单节点最初把时间金额全编错的根因。
    """
    result: list[BaseMessage] = [
        SystemMessage(fit_by_budget(system_prompt, BUDGET["system_prompt"]))
    ]

    mem = render_memory(profile)
    if mem:
        result.append(
            SystemMessage(
                "以下是该用户的历史背景，仅作参考。"
                "凡涉及制度、权益、订单状态的，一律以本轮工具查询结果为准。\n"
                + fit_by_budget(mem, BUDGET["long_memory"])
            )
        )

    if facts:
        result.append(
            SystemMessage(
                fit_by_budget(
                    "【本轮工具查询结果】\n" + facts + "\n"
                    "以上是唯一可信的事实。回答只能使用这里出现的信息，"
                    "这里没有的时间、金额、状态、单号一律不许编造。",
                    BUDGET["retrieved_docs"],
                )
            )
        )

    if retrieved:
        kept = fit_docs_by_score(retrieved, BUDGET["retrieved_docs"])
        docs_text = render_docs(kept)
        if docs_text:
            result.append(SystemMessage(docs_text))

    result.extend(trim_history(history, BUDGET["messages"]))
    return result


def last_user_text(messages: list[BaseMessage]) -> str:
    """取最后一条用户消息的文本。"""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage) and isinstance(msg.content, str):
            return msg.content
    return ""


def last_ai_text(messages: list[BaseMessage]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and isinstance(msg.content, str) and msg.content:
            return msg.content
    return ""
