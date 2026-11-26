"""权益问答节点 —— 父图节点，内部跑 benefit_agent 子图。

职责划分：
- ``benefit_agent``（子图）：自己决定查几次、查什么，拿到条款后组织回答；
- 本节点：先做一道**成本闸**（检索门限不过就别让 Agent 白跑），
  跑完再做**事实闸**（引用编号 / 数字必须来自真实检索结果）。

推理交给 Agent，事实边界交给代码。这道分工是防幻觉能成立的前提：
Agent 的循环会提升灵活性，但也提升了编造的机会，所以校验必须留在外面。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from app.db import repo
from app.graph.agents import benefit_agent, final_ai_text, tool_payloads, tools_used_in
from app.graph.context import last_user_text
from app.graph.guard import enforce_citation_or_fallback, extract_citations
from app.graph.state import AgentState
from app.tools.benefit_tools import search_benefits

#: 「我有哪些权益」这类泛问：不指向某条具体条款，直接按等级列出来
GENERIC_HINTS = (
    "有哪些权益",
    "什么权益",
    "权益有哪些",
    "会员权益",
    "有什么权益",
    "权益是啥",
    "介绍一下权益",
)


def _is_generic(query: str) -> bool:
    return any(h in query for h in GENERIC_HINTS)


async def benefit_node(state: AgentState) -> dict:
    """检索门限 → benefit_agent 子图 → 引用/数字校验。"""
    history = state.get("messages") or []
    query = last_user_text(history)
    member_id = state.get("member_id")

    if not query:
        return {"messages": [AIMessage("您想了解哪方面的权益？")], "degraded": True}

    # ---- 0) 先拿会员等级：检索和回答都要用到 ----
    level: str | None = None
    if member_id:
        member = await repo.get_member(member_id)
        level = member["level"] if member else None

    # ---- 1) 预检索：只为两件事，**不用来拦截** ----
    # a) 泛问（「我有哪些权益」）是确定性的，直接按等级列，不必花 Agent 一轮；
    # b) 命中结果作为后面引用校验的兜底语料。
    #
    # 刻意**不**在这里「没命中就降级」：整句关键词的 2-gram 打分很低
    # （「我买的年卡，现在还能领几次免运费」只有 0.086），拿它当门禁会把
    # Agent 改写关键词的能力废掉——它本来可以把句子拆成「免运费」再查。
    # 查不到该由 Agent 自己调完工具后决定，而不是预检索替它决定。
    if _is_generic(query) and level:
        prehit = await repo.get_benefits_by_level(level)
    else:
        try:
            search_result = await search_benefits.ainvoke({"keyword": query, "level": level})
            prehit = search_result.get("results") or []
        except Exception as e:
            return {
                "messages": [AIMessage("权益查询服务暂时不可用，帮您转人工处理。")],
                "degraded": True,
                "error": f"search_failed: {e}",
            }

    # ---- 2) 跑 Agent 子图：它自己会调 search_benefits / get_benefit_detail ----
    # 等级必须注入：不注入的话 Agent 会照提示词「等级不明确先问清」反问用户，
    # 而等级我们明明刚查到。
    profile = state.get("profile")
    agent = benefit_agent(member_id, profile, level)
    try:
        result = await agent.ainvoke(
            {"messages": [*history[-6:], HumanMessage(query)]},
            # recursion_limit 兜底：Agent 自己决定循环次数，但不能无限
            config={"recursion_limit": 12},
        )
    except Exception as e:
        return {
            "messages": [AIMessage("模型服务暂时不可用，请稍后重试或转人工。")],
            "degraded": True,
            "error": f"agent_failed: {e}",
        }

    agent_messages = result.get("messages") or []
    answer = final_ai_text(agent_messages)
    used_tools = [*state.get("tools_used", []), *tools_used_in(agent_messages)]

    # ---- 3) 事实闸：Agent 实际检索到的条款才是校验依据 ----
    # 用预检索结果兜底：Agent 可能没调工具（直接凭上下文答），
    # 此时引用校验的「合法编号集合」仍然要来自真实数据，否则整道闸失效。
    retrieved = _collect_retrieved(agent_messages) or prehit

    # Agent 和预检索都没拿到任何条款 —— 这才是真正的「查不到」
    if not retrieved:
        return {
            "messages": [
                AIMessage(
                    "这个权益我这边没有查到对应条款。为避免给您错误信息，"
                    "帮您转接人工客服核实，可以吗？"
                )
            ],
            "retrieved": [],
            "citations": [],
            "degraded": True,
            "tools_used": used_tools or [*state.get("tools_used", []), "search_benefits"],
        }

    tool_corpus = [
        f"{r.get('code')} {r.get('name')} {r.get('value')} {r.get('expire_days')}"
        for r in retrieved
    ]
    final_answer, forced_degraded = enforce_citation_or_fallback(
        answer, retrieved, tool_corpus, require_citation=True
    )

    citations = [
        {"code": c, "name": next((r["name"] for r in retrieved if r["code"] == c), "")}
        for c in extract_citations(final_answer)
    ]

    return {
        "messages": [AIMessage(final_answer)],
        "retrieved": retrieved,
        "citations": citations,
        # degraded 只表示「回答被强制换成了兜底话术」。
        # 不能用 is_degraded() 扫措辞——正常回答末尾「可帮您转接人工」是礼貌兜底，
        # 被算成降级会让整条链路的降级率统计失真。
        "degraded": bool(forced_degraded),
        "tools_used": used_tools,
        "tokens_out": state.get("tokens_out", 0) + len(final_answer),
    }


def _collect_retrieved(messages: list) -> list[dict]:
    """从子图消息里回收工具实际返回的条款，作为校验的合法集合。

    Agent 可能调了多次检索（先宽后窄），把所有工具返回值并起来。
    ToolMessage.content 是 JSON 字符串，走 ``tool_payloads`` 统一解析。
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for payload in tool_payloads(messages):
        for r in payload.get("results") or []:
            code = r.get("code")
            if code and code not in seen:
                seen.add(code)
                rows.append(r)
        benefit = payload.get("benefit")
        if isinstance(benefit, dict) and benefit.get("code") not in seen:
            seen.add(benefit["code"])
            rows.append(benefit)
    return rows
