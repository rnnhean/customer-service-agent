"""审批台接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.api.errors import err_conflict, err_not_found, err_unprocessable
from app.db import repo
from app.graph.build import get_graph

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class DecisionBody(BaseModel):
    approved: bool
    reviewer: str = Field(min_length=1, max_length=32)
    comment: str = Field(default="", max_length=200)


@router.get("")
async def list_approvals(
    status: str = "pending",
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    if status not in ("pending", "approved", "rejected", "expired", "all"):
        status = "pending"
    page = max(1, page)
    page_size = min(max(1, page_size), 100)

    items, total = await repo.list_approvals(status=status, page=page, page_size=page_size)
    return {
        "items": [_decorate(i) for i in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/{apply_id}/decision")
async def decide(apply_id: str, body: DecisionBody) -> dict[str, Any]:
    """审批决策。

    **顺序不能颠倒**：先 CAS 占位，再 resume 图。
    反过来的话两个并发请求会都通过检查、都 resume，退款执行两次。
    """
    # 驳回必须写理由
    if not body.approved and not body.comment.strip():
        raise err_unprocessable("驳回时必须填写理由")

    existing = await repo.get_approval(apply_id)
    if not existing:
        raise err_not_found(f"审批单 {apply_id} 不存在")

    # ---- 1) CAS：抢不到说明已被处理 ----
    apply = await repo.cas_update_approval(
        apply_id=apply_id,
        approved=body.approved,
        reviewer=body.reviewer,
        comment=body.comment,
    )
    if apply is None:
        # CAS 没抢到：并发场景下另一个请求可能刚刚改了状态，
        # 必须**重查**再决定提示什么，不能用前面读到的 existing（那是脏快照）。
        latest = await repo.get_approval(apply_id)
        if latest and latest["status"] == "expired":
            raise err_conflict("该申请已过期", error_no=40902)
        status_now = latest["status"] if latest else existing["status"]
        raise err_conflict(f"该申请已处理（当前状态：{status_now}）")

    # ---- 2) 再恢复图 ----
    try:
        graph = await get_graph()
        await graph.ainvoke(
            Command(resume={"approved": body.approved, "comment": body.comment}),
            config={"configurable": {"thread_id": apply["thread_id"]}},
        )
    except Exception as e:
        # 审批单已经落库了，图恢复失败要让调用方知道
        return {
            "ok": False,
            "apply_id": apply_id,
            "status": apply["status"],
            "warning": f"审批已记录，但恢复会话失败：{type(e).__name__}",
        }

    return {
        "ok": True,
        "apply_id": apply_id,
        "status": apply["status"],
        "decided_at": apply["decided_at"],
    }


@router.post("/expire-stale")
async def expire_stale() -> dict[str, Any]:
    """把过期的 pending 审批单置为 expired。生产环境由定时任务调。"""
    count = await repo.expire_stale_approvals()
    return {"ok": True, "expired": count}


def _decorate(row: dict[str, Any]) -> dict[str, Any]:
    """补上给前端展示用的字段。"""
    amount = row.get("amount") or 0
    return {
        **row,
        "summary": (
            f"退款申请：会员 {row.get('member_id')}，金额 {amount / 100:.2f} 元，"
            f"原因：{row.get('reason') or '未说明'}"
        ),
    }
