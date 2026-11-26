"""会话与 trace 接口。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter

from app.api.errors import err_not_found
from app.db import repo
from app.graph.checkpointer import delete_thread

router = APIRouter(prefix="/api", tags=["sessions"])


@router.get("/sessions")
async def list_sessions(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    items, total = await repo.list_sessions(page=page, page_size=page_size)

    decorated = []
    for s in items:
        pending, _ = await repo.list_approvals(status="pending", page=1, page_size=100)
        has_pending = any(p["thread_id"] == s["thread_id"] for p in pending)
        decorated.append({**s, "has_pending_approval": has_pending})

    return {"items": decorated, "total": total}


@router.get("/sessions/{thread_id}/messages")
async def get_session_messages(thread_id: str) -> dict[str, Any]:
    """历史消息只返回纯文本。

    当时的 tool 调用链和审批卡片状态会过期，重新渲染会误导用户。
    需要追溯请用 /api/traces/{thread_id}。
    """
    session = await repo.get_session(thread_id)
    if not session:
        raise err_not_found(f"会话 {thread_id} 不存在")

    from app.graph.build import get_graph

    graph = await get_graph()
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    raw = (state.values or {}).get("messages", []) if state else []

    messages = []
    for i, m in enumerate(raw):
        role = getattr(m, "type", "")
        if role == "human":
            role = "user"
        elif role in ("ai", "assistant"):
            role = "assistant"
        else:
            continue
        content = m.content if isinstance(m.content, str) else str(m.content)
        messages.append(
            {
                "id": getattr(m, "id", None) or f"msg-{i}",
                "role": role,
                "content": content,
                "created_at": session.get("updated_at", ""),
            }
        )

    return {"thread_id": thread_id, "messages": messages}


@router.delete("/sessions/{thread_id}")
async def delete_session(thread_id: str) -> dict[str, Any]:
    """删会话 = 删 checkpoints + 会话元数据。不删审批单（那是业务记录）。"""
    await delete_thread(thread_id)
    await repo.delete_session(thread_id)
    return {"ok": True}


@router.get("/traces/{thread_id}")
async def get_traces(thread_id: str) -> dict[str, Any]:
    rows = await repo.get_traces(thread_id)
    items = []
    for r in rows:
        tools = r.get("tools") or "[]"
        if isinstance(tools, str):
            try:
                tools = json.loads(tools)
            except Exception:
                tools = []
        items.append({**r, "tools": tools})
    return {"thread_id": thread_id, "items": items}


@router.get("/members/{member_id}")
async def get_member(member_id: str) -> dict[str, Any]:
    """调试用：看会员基本信息。"""
    member = await repo.get_member(member_id)
    if not member:
        raise err_not_found(f"会员 {member_id} 不存在")
    status = await repo.get_member_status(member_id)
    orders = await repo.get_orders(member_id, limit=5)
    return {"member": member, "status": status, "recent_orders": orders}
