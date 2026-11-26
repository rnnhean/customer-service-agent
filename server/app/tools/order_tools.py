"""订单查询工具。只读，不涉及写操作。"""

from __future__ import annotations

from langchain_core.tools import tool

from app.db import repo


async def _decorate(row: dict) -> dict:
    """补上展示名，模型不用自己翻译，减少出错。

    **状态中文一律从 lookups 字典表读**，不在代码里维护映射：
    之前这里有两张写死的表，`STATUS_CN` 尚能与库对上，`TYPE_CN` 则映射的是
    `open/renew/refund` 而库里 `type` 存的是「会员开通」「会员续费」这类中文，
    key 一个都命中不了 —— 等于一张永远不生效的死表，还让人误以为做了翻译。
    """
    status = row.get("status", "")
    labels = {r["code"]: r["label"] for r in await repo.get_lookups("order_status")}
    return {
        **row,
        "status_cn": labels.get(status, status),
        "amount_yuan": f"{(row.get('amount') or 0) / 100:.2f}",
    }


@tool
async def get_order(order_id: str) -> dict:
    """按订单号查订单详情。

    Args:
        order_id: 订单号，形如 O20261106001

    Returns:
        {"found": bool, "order": {...}} 或 {"found": false, "error": "..."}
    """
    try:
        row = await repo.get_order(order_id)
    except Exception as e:
        return {"found": False, "error": f"查询失败: {e}"}
    if not row:
        return {"found": False, "error": f"订单 {order_id} 不存在"}
    return {"found": True, "order": await _decorate(row)}


@tool
async def list_member_orders(member_id: str, limit: int = 5) -> dict:
    """列出某个会员最近的订单，用于用户没提供订单号的场景。

    Args:
        member_id: 会员 ID
        limit: 最多返回几条，默认 5
    """
    try:
        rows = await repo.get_orders(member_id, limit=limit)
    except Exception as e:
        return {"found": False, "error": f"查询失败: {e}"}
    if not rows:
        return {"found": False, "error": "该会员没有订单记录"}
    return {"found": True, "count": len(rows), "orders": [_decorate(r) for r in rows]}


ORDER_TOOLS = [get_order, list_member_orders]
