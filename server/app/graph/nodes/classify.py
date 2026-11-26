"""意图分类节点。

一次结构化输出同时拿到 intent + 槽位 + 置信度，不做两轮调用。
低置信度不硬猜，直接走澄清——客服场景猜错的代价高于多问一句。
"""

from __future__ import annotations

from typing import Literal

from langchain_core.messages import SystemMessage
from pydantic import BaseModel, Field

from app.config import settings
from app.db import repo
from app.graph.prompts import CLASSIFY_PROMPT
from app.graph.state import AgentState
from app.llm import get_llm


class Route(BaseModel):
    """分类结果。字段顺序会影响模型输出顺序，先输出 intent。"""

    intent: Literal["benefit_qa", "order_status", "change_request", "complaint", "unknown"] = Field(
        description="用户意图"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="判断确信程度 0.0-1.0")
    order_id: str | None = Field(default=None, description="订单号，没有就 null")
    reason: str | None = Field(default=None, description="用户给出的原因，不超过 30 字")


async def classify_node(state: AgentState) -> dict:
    """返回 intent / confidence / slots / profile。任何异常都降级为 unknown，不阻断对话。"""
    messages = state.get("messages") or []
    if not messages:
        return {"intent": "unknown", "confidence": 0.0, "route": "clarify"}

    # 长期记忆在这里载入一次，全链路复用——各 Agent 不用重复查库
    profile = state.get("profile")
    if profile is None and state.get("member_id"):
        try:
            row = await repo.get_member_profile(state["member_id"])
            if row:
                profile = row.get("profile") or None
        except Exception:
            profile = None  # 画像读失败不影响主流程

    llm = get_llm(temperature=0.0).with_structured_output(Route)

    try:
        result: Route = await llm.ainvoke(
            [
                SystemMessage(CLASSIFY_PROMPT),
                *messages[-settings.history_window_messages :],
            ]
        )
    except Exception as e:
        # 分类失败不能让整通对话崩掉，降级走澄清
        return {
            "intent": "unknown",
            "confidence": 0.0,
            "route": "clarify",
            "profile": profile,
            "error": f"classify_failed: {e}",
            "degraded": True,
        }

    # 低置信度一律走澄清
    if result.confidence < settings.classify_confidence_threshold:
        return {
            "intent": "unknown",
            "confidence": result.confidence,
            "route": "clarify",
            "profile": profile,
        }

    slots: dict[str, str] = {}
    if result.order_id:
        slots["order_id"] = result.order_id
    if result.reason:
        slots["reason"] = result.reason

    return {
        "intent": result.intent,
        "confidence": result.confidence,
        "slots": {**state.get("slots", {}), **slots},
        "route": result.intent,
        "profile": profile,
    }


#: 意图 -> 节点名。条件边用它的 key 做 path_map 的索引。
INTENT_TO_NODE: dict[str, str] = {
    "benefit_qa": "benefit_rag",
    "order_status": "order_flow",
    "change_request": "refund_approval",
    "complaint": "complaint",
    "unknown": "clarify",
}


def route_by_intent(state: AgentState) -> str:
    """条件边函数。

    **必须返回意图 key，不是节点名**：LangGraph 会拿这个返回值去 ``path_map``
    里查表，查不到就抛 KeyError。节点名的映射交给 ``build.py`` 的 path_map。
    """
    intent = state.get("intent") or "unknown"
    return intent if intent in INTENT_TO_NODE else "unknown"
