"""权益检索工具。

本期不接向量库：权益规则只有几十条，SQL 关键词匹配 + 简单打分就够。
如果将来规则上千条，再换 sqlite-vec —— 规模决定选型，不为简历硬上向量库。
"""

from __future__ import annotations

import re

from langchain_core.tools import tool

from app.db import repo


def _score(query: str, item: dict) -> float:
    """关键词命中打分。粗糙但可解释，适合规则条数少的场景。"""
    q = query.lower()
    name = (item.get("name") or "").lower()
    value = (item.get("value") or "").lower()

    score = 0.0
    # 名称完整命中权重最高
    if q and q in name:
        score += 0.6
    if q and q in value:
        score += 0.25

    # 二元组命中：中文按 2 字切，英文按词切
    grams = _grams(q)
    if grams:
        name_hits = sum(1 for g in grams if g in name)
        value_hits = sum(1 for g in grams if g in value)
        score += 0.5 * (name_hits / len(grams))
        score += 0.2 * (value_hits / len(grams))

    return min(score, 1.0)


def _grams(text: str) -> set[str]:
    """抽出 2-gram。中文没空格，按字切；英文按词。"""
    words = re.findall(r"[a-z]{3,}", text)
    chars = re.findall(r"[一-鿿]", text)
    bigrams = {"".join(chars[i : i + 2]) for i in range(len(chars) - 1)}
    return set(words) | bigrams


#: 泛问（「我有哪些权益」）不指向任何具体条款。整句拿去打分一定是 0 分，
#: 因为里面全是「我」「有」「哪些」这种高频虚词，没有一条权益条款能匹配上。
#: 这种情况下正确的动作是**按等级把所有权益列出来**，而不是回「查不到」。
GENERIC_HINTS = (
    "有哪些权益",
    "什么权益",
    "权益有哪些",
    "会员权益",
    "有什么权益",
    "权益是啥",
    "介绍一下权益",
    "全部权益",
)

#: 提示词告诉 Agent「泛问就传『权益』」，所以裸词必须也被认成泛问。
#: 少了这一条，Agent 按提示词传了「权益」，工具却当普通关键词打 0 分，
#: 它会以为失败、反复重试直到 recursion_limit 耗尽。
GENERIC_EXACT = {"权益", "会员权益", "所有权益", "全部权益", "权益清单", "权益列表"}


def _is_generic(query: str) -> bool:
    q = query.strip().strip("？?。.！!")
    if q in GENERIC_EXACT:
        return True
    return any(h in query for h in GENERIC_HINTS)


@tool
async def search_benefits(keyword: str, level: str | None = None) -> dict:
    """检索会员权益条款。

    Args:
        keyword: 用户关心的权益关键词，例如「免运费」「生日礼」「退款」。
            **用短语，不要整句**——整句会被虚词稀释成 0 分。
            如果用户是泛问（「我有哪些权益」），这里就传「权益」两个字。
        level: 会员等级 normal/silver/gold/platinum，不确定就留空

    Returns:
        {"found": bool, "min_score": float, "threshold": float, "results": [...]}
        results 里每项含 code / name / value / level / stackable / expire_days / score
    """
    try:
        # ---- 泛问：按等级全列，不参与打分 ----
        if level and _is_generic(keyword):
            rows = await repo.get_benefits_by_level(level)
            return {
                "found": bool(rows),
                "min_score": 1.0,
                "threshold": 0.35,
                "mode": "by_level",
                "results": [{**r, "score": 1.0} for r in rows],
            }

        rows = await repo.search_benefits(keyword, level=level, limit=20)
        if not rows and level:
            # 关键词整句匹配不上（例如「我买的年卡还能领几次免运费」），
            # 退回按等级取候选，由后面的打分决定是否命中，不能因为 LIKE 没中就判「查不到」。
            rows = await repo.get_benefits_by_level(level)
    except Exception as e:  # 数据层异常不能让 Agent 崩
        return {"found": False, "error": f"检索失败: {e}", "results": [], "min_score": 0.0}

    scored = [{**r, "score": round(_score(keyword, r), 3)} for r in rows]
    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:5]

    threshold = 0.35
    best = top[0]["score"] if top else 0.0
    found = bool(top) and best >= threshold

    return {
        "found": found,
        "min_score": best,
        "threshold": threshold,
        "results": top if found else [],
        # 没命中时给出提示，让 Agent 知道下一步该换个短关键词再试，
        # 而不是直接告诉用户「查不到」。
        "hint": (
            None
            if found
            else "没命中。请换个更短的核心词（如「免运费」「生日礼」）重试，"
            "或者直接传「权益」做泛问列举。"
        ),
    }


@tool
async def get_benefit_detail(code: str) -> dict:
    """按权益编号查详情。code 形如 B-GOLD-001。"""
    try:
        row = await repo.get_benefit_by_code(code)
    except Exception as e:
        return {"found": False, "error": f"查询失败: {e}"}
    if not row:
        return {"found": False, "error": f"权益编号 {code} 不存在"}
    return {"found": True, "benefit": row}


BENEFIT_TOOLS = [search_benefits, get_benefit_detail]
