"""LangGraph 状态定义。

设计要点：
- messages 用 add_messages reducer，节点返回的消息是「追加」不是「覆盖」
- total=False：LangGraph 的 state 是增量合并，节点只需返回自己改的字段
- 所有字段必须是可序列化的（能进 checkpoint），不放连接、客户端之类对象
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

Intent = Literal[
    "benefit_qa",      # 权益咨询
    "order_status",    # 订单查询
    "change_request",  # 退订/变更（走审批）
    "complaint",       # 投诉
    "unknown",         # 意图不明，走澄清
]

RiskLevel = Literal["low", "high"]


class AgentState(TypedDict, total=False):
    """图状态。"""

    # ---- 对话 ----
    messages: Annotated[list[AnyMessage], add_messages]

    # ---- 会话与身份 ----
    thread_id: str
    member_id: str | None
    turn: int

    # ---- 路由 ----
    intent: Intent
    confidence: float
    route: str

    # ---- 槽位 ----
    slots: dict[str, str]          # order_id / amount / reason / category

    # ---- 业务 ----
    approval_id: str | None
    risk_level: RiskLevel
    citations: list[dict]          # [{"code": "...", "name": "...", "value": "..."}]
    retrieved: list[dict]          # 检索命中的权益条目（含 score）
    profile: dict | None           # 会员长期记忆，由 classify 载入一次，全链路复用

    # ---- 诊断 ----
    error: str | None
    degraded: bool                 # 是否走了降级/转人工
    tools_used: list[str]
    tokens_in: int
    tokens_out: int


def new_state(thread_id: str, member_id: str | None = None) -> AgentState:
    """构造一份干净的初始状态。"""
    return AgentState(
        thread_id=thread_id,
        member_id=member_id,
        turn=0,
        slots={},
        citations=[],
        retrieved=[],
        tools_used=[],
        degraded=False,
        error=None,
        risk_level="low",
        tokens_in=0,
        tokens_out=0,
    )
