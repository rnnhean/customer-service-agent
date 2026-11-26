"""收尾节点。写会话、记录 trace、更新长期记忆。"""

from __future__ import annotations

import json
import uuid

from app.db import repo
from app.graph.context import last_user_text
from app.graph.state import AgentState


async def finalize_node(state: AgentState) -> dict:
    """所有分支都汇到这里。做持久化，不改对话内容。"""
    thread_id = state.get("thread_id", "")
    messages = state.get("messages") or []

    if not thread_id:
        return {}

    # 会话表同步
    try:
        session = await repo.get_session(thread_id)
        user_text = last_user_text(messages)
        if session is None:
            await repo.create_session(
                thread_id=thread_id,
                member_id=state.get("member_id"),
                title=(user_text[:20] or "新会话"),
            )
        else:
            await repo.touch_session(thread_id, user_text[:100])
    except Exception:
        pass  # 会话元数据写失败不影响主流程

    # trace 落库
    try:
        turn = await repo.get_next_turn(thread_id)
        await repo.record_trace(
            trace_id=f"tr-{uuid.uuid4().hex[:12]}",
            thread_id=thread_id,
            turn=turn,
            intent=state.get("intent"),
            route=state.get("route"),
            tools=json.dumps(state.get("tools_used", []), ensure_ascii=False),
            latency_ms=None,
            tokens_in=state.get("tokens_in", 0),
            tokens_out=state.get("tokens_out", 0),
            degraded=1 if state.get("degraded") else 0,
        )
    except Exception:
        pass

    return {}
