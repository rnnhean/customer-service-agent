"""投诉工单工具。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from langchain_core.tools import tool

from app.config import settings
from app.db import repo


@tool
async def create_complaint(
    member_id: str,
    category: str,
    priority: int,
    summary: str,
) -> dict:
    """创建投诉工单。

    Args:
        member_id: 投诉人会员 ID
        category: 投诉分类，从工具返回的 candidate_categories 里选；
            实在对不上就传「其他」，**不要自造分类名**
        priority: 紧急程度。1=最高（用户提到曝光/12315/媒体），2=较高（反复未解决），3=一般
        summary: 投诉内容摘要，一句话，不超过 50 字

    Returns:
        {"ok": bool, "ticket_id": ..., "sla_deadline": ...} 或 {"ok": false, "error": ...}
    """
    # 分类来自 lookups 字典表，不在代码里写死词表：
    # 之前代码白名单是「物流问题」，而库里真实分类是「物流延迟」，
    # Agent 照历史工单抄了真实值，反被工具以「分类不合法」拒掉。
    # 归一不到任何分类时才拒绝——那种情况说明字典表没配 fallback。
    resolved = await repo.resolve_lookup("complaint_category", category)
    if resolved is None:
        return {
            "ok": False,
            "error": f"分类「{category}」无法识别，且字典表未配置兜底分类",
        }
    if resolved != category:
        # 记下归一动作，便于排查「为什么工单分类和我说的不一样」
        normalized_from = category
    else:
        normalized_from = None

    if priority not in settings.complaint_sla_hours:
        return {"ok": False, "error": "priority 只能是 1 / 2 / 3"}

    hours = settings.complaint_sla_hours[priority]
    sla_deadline = datetime.now(UTC) + timedelta(hours=hours)

    try:
        ticket = await repo.create_complaint(
            member_id=member_id,
            category=resolved,
            priority=priority,
            sla_deadline=sla_deadline,
            summary=summary,
        )
    except Exception as e:
        return {"ok": False, "error": f"创建工单失败: {e}"}

    return {
        "ok": True,
        "ticket_id": ticket["ticket_id"],
        "category": resolved,
        "normalized_from": normalized_from,
        "priority": priority,
        "sla_hours": hours,
        "sla_deadline": sla_deadline.isoformat(),
        "summary": summary,
        "message": f"工单已创建，承诺 {hours} 小时内首次回复",
    }


@tool
async def list_member_complaints(member_id: str) -> dict:
    """查该会员的历史投诉，用于判断是否为反复投诉。"""
    try:
        rows = await repo.get_complaints(member_id)
    except Exception as e:
        return {"found": False, "error": f"查询失败: {e}"}
    if not rows:
        return {"found": False, "count": 0, "complaints": []}
    return {"found": True, "count": len(rows), "complaints": rows}


COMPLAINT_TOOLS = [create_complaint, list_member_complaints]
