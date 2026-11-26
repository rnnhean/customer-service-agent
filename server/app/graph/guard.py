"""防幻觉校验。

四道闸的实现。**能查的不用 LLM，能校验的不靠提示词。**
模型可以编，但编出来的东西过不了这几关。
"""

from __future__ import annotations

import re

# 回答里所有方括号内容，取出内部串。
# **不把「权益-」这类提示词前缀写进正则**：前缀是我们给模型的格式约定，
# 不是数据事实；提示词一改、模型换个措辞，写死的正则就整道失效。
# 合法的判据只有一个 —— 引用的编号是否真的出现在本轮检索结果里。
BRACKET_RE = re.compile(r"\[([^\[\]]{2,40})\]")

# 匹配数字（金额、天数、次数）
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

# 常见「不敢说不知道」的措辞，用来判断模型是否真的降级了
HEDGE_PHRASES = ("不确定", "帮您转人工", "转接人工", "查不到", "没有找到")


def _normalize_bracket(raw: str) -> str:
    """把方括号内容归一成可能的权益编号。

    ``[权益-silver_01]`` / ``[权益：silver_01]`` / ``[silver_01]``
    都归一成 ``silver_01``；``[B-GOLD-001]`` 保持原样。

    剥前缀的规则：**只在第一个片段是纯中文**（提示词给的中文前缀，如「权益」）
    时才丢掉它。否则 ``B-GOLD-001`` 这种编码会被从最后一个连字符切开。
    """
    text = raw.strip()
    for sep in ("-", "：", ":", " "):
        if sep not in text:
            continue
        head, _, tail = text.partition(sep)
        # 头部是纯中文 → 是「权益」这类前缀，丢掉；否则整个串就是编号
        if head.strip() and all("一" <= ch <= "鿿" for ch in head.strip()):
            return tail.strip() or text
        return text
    return text


def extract_citations(answer: str) -> set[str]:
    """抽出回答里引用的权益编号。

    取所有方括号内容并归一化；**是否算「引用」由调用方用真实检索结果判定**，
    这里不做格式白名单。像 ``[注意]`` 这种内容归一化后自然对不上任何 code，
    会在 validate_citations 里被当成引用了不存在的编号——这正好是想要的行为：
    模型不该在回答里编造方括号编号。
    """
    out: set[str] = set()
    for raw in BRACKET_RE.findall(answer):
        code = _normalize_bracket(raw)
        # 只收「像编号」的：含下划线，或者主体是 ASCII 字母数字。
        # [步骤1] / [注意] 这种中文方括号是正文排版，不是编号引用。
        has_underscore = "_" in code
        ascii_alnum = sum(1 for ch in code if ch.isascii() and ch.isalnum())
        if has_underscore or ascii_alnum >= 3:
            out.add(code)
    return out


def validate_citations(
    answer: str, retrieved: list[dict], *, require: bool = False
) -> tuple[bool, str]:
    """校验引用是否全部来自真实检索结果。

    Args:
        require: 为 True 时「一条引用都没有」也算失败。权益问答必须开这个，
            否则模型完全可以不引用、绕过整道闸。
    """
    cited = extract_citations(answer)
    valid = {r.get("code") for r in retrieved if r.get("code")}

    if not cited:
        if require:
            return False, "回答里没有任何权益引用"
        # 没引用不一定是错——可能只是在说客套话。交给下面的策略判断
        return True, ""

    hallucinated = cited - valid
    if hallucinated:
        return False, f"引用了不存在的权益编号: {sorted(hallucinated)}"
    return True, ""


def validate_numbers(answer: str, tool_results: list[str]) -> tuple[bool, str]:
    """校验回答里的数字都能在工具结果里找到。

    注意：不做严格全量校验——像「3 个工作日」这种约定俗成的表述，
    工具里不一定有。只校验**看起来是业务数据**的数字（带单位或较大的数）。
    """
    corpus = " ".join(tool_results)
    corpus_nums = set(NUMBER_RE.findall(corpus))

    suspicious: list[str] = []
    for num in NUMBER_RE.findall(answer):
        if num in corpus_nums:
            continue
        # 单个小数字（1-3 这种「1-3 个工作日」）放过
        try:
            val = float(num)
        except ValueError:
            continue
        if val <= 3:
            continue
        suspicious.append(num)

    if suspicious:
        return False, f"数字未在工具结果中出现: {suspicious}"
    return True, ""


def is_degraded(answer: str) -> bool:
    """判断模型是否走了降级话术。用于统计转人工率。"""
    return any(p in answer for p in HEDGE_PHRASES)


def enforce_citation_or_fallback(
    answer: str,
    retrieved: list[dict],
    tool_results: list[str],
    *,
    require_citation: bool = False,
) -> tuple[str, bool]:
    """最终把关：校验不过就把回答换成兜底话术。

    Returns:
        (最终回答, 是否被降级)
    """
    ok_cite, _reason_cite = validate_citations(answer, retrieved, require=require_citation)
    ok_num, _reason_num = validate_numbers(answer, tool_results)

    if ok_cite and ok_num:
        return answer, False

    return (
        "这个问题我核实不了，为避免给您错误信息，我帮您转接人工客服处理。",
        True,
    )
