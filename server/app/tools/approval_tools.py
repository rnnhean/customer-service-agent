"""退款申请工具。

关键设计：这个工具**只创建审批单，不执行退款**。
真正的退款动作在 interrupt 恢复之后、由图的节点代码执行。
LLM 能做的只是「发起申请」，不能让它直接动钱。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from langchain_core.tools import tool

from app.config import settings
from app.db import repo


@tool
async def submit_refund_request(
    order_id: str,
    amount: int,
    reason: str,
    member_id: str,
) -> dict:
    """提交退款申请，进入人工审批流程。

    Args:
        order_id: 要退款的订单号
        amount: 退款金额，**整数分**。必须来自订单信息，不能自己编
        reason: 退款原因，一句话
        member_id: 申请人的会员 ID

    Returns:
        成功：{"ok": true, "apply_id": ..., "expires_at": ...}
        失败：{"ok": false, "error": "..."}
    """
    try:
        order = await repo.get_order(order_id)
    except Exception as e:
        return {"ok": False, "error": f"查询订单失败: {e}"}

    if not order:
        return {"ok": False, "error": f"订单 {order_id} 不存在"}

    if order["status"] != "EFFECTIVE":
        return {
            "ok": False,
            "error": f"订单当前状态为「{order['status']}」，不能发起退款",
            "order_status": order["status"],
        }

    # 金额校验：不能让模型自由发挥
    order_amount = int(order.get("amount") or 0)
    if amount <= 0:
        return {"ok": False, "error": "退款金额必须大于 0"}
    if amount > order_amount:
        return {
            "ok": False,
            "error": f"退款金额不能超过订单实付金额 {order_amount / 100:.2f} 元",
            "max_amount": order_amount,
        }

    expires_at = datetime.now(UTC) + timedelta(hours=settings.approval_ttl_hours)

    try:
        apply = await repo.create_approval(
            thread_id="",  # 由节点在 interrupt 前回填
            member_id=member_id,
            type="refund",
            amount=amount,
            reason=reason,
            expires_at=expires_at,
        )
    except Exception as e:
        return {"ok": False, "error": f"创建审批单失败: {e}"}

    return {
        "ok": True,
        "apply_id": apply["apply_id"],
        "amount": amount,
        "amount_yuan": f"{amount / 100:.2f}",
        "expires_at": expires_at.isoformat(),
        "message": "申请已提交，等待人工审批",
    }


@tool
async def check_approval_status(apply_id: str) -> dict:
    """查询审批单当前状态。用户催进度时用。"""
    try:
        row = await repo.get_approval(apply_id)
    except Exception as e:
        return {"found": False, "error": f"查询失败: {e}"}
    if not row:
        return {"found": False, "error": f"审批单 {apply_id} 不存在"}

    status_cn = {
        "pending": "待审批",
        "approved": "已通过",
        "rejected": "已驳回",
        "expired": "已过期",
    }
    return {
        "found": True,
        "apply_id": row["apply_id"],
        "status": row["status"],
        "status_cn": status_cn.get(row["status"], row["status"]),
        "amount_yuan": f"{(row.get('amount') or 0) / 100:.2f}",
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


REFUND_TOOLS = [submit_refund_request, check_approval_status]
