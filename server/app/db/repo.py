"""业务仓储层：所有对 PostgreSQL 的读写都在这里。

约定

- 每个函数都是 ``async def``，内部从连接池借连接，调用方不需要管理事务。
  正常返回自动提交，抛异常自动回滚（``psycopg_pool`` 的行为）。
- 返回值一律是纯 Python 的 ``dict`` / ``list[dict]``，**不会**把 psycopg 的
  Row 泄漏到上层；``datetime`` 统一转成 ISO 字符串，保证能直接进
  LangGraph 的 state 和 SSE 的 JSON。
- 时间参数用 ``datetime`` 传，让 PG 自己比较 ``timestamptz``。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

from psycopg.types.json import Jsonb

from app.db.connection import get_conn

# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _norm(value: Any) -> Any:
    """把 PG 返回的特殊类型转成可 JSON 序列化的值。"""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _row_to_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """把一行聚合成纯 dict；没有数据时返回 None。"""
    return {k: _norm(v) for k, v in row.items()} if row is not None else None


def _rows_to_dicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: _norm(v) for k, v in row.items()} for row in rows]


def _now() -> datetime:
    return datetime.now(UTC)


async def _fetch_one(conn, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        row = await cur.fetchone()
    return _row_to_dict(row)


async def _fetch_all(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    return _rows_to_dicts(list(rows))


# ---------------------------------------------------------------------------
# 业务枚举字典
# ---------------------------------------------------------------------------

#: 字典缓存：kind -> (读取时间戳, 行列表)。
#: 字典几乎不变，但**不能永久缓存**——运营新增一个投诉分类不该要求重启服务。
#: 60 秒 TTL 在这个规模下等于「零成本 + 最多晚一分钟生效」。
_LOOKUP_TTL_SECONDS = 60.0
_lookup_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


async def get_lookups(kind: str, *, use_cache: bool = True) -> list[dict[str, Any]]:
    """取某类业务枚举（按 sort 排好序，只取 active）。

    枚举属于数据不属于代码：订单类型、投诉分类会随业务迭代变化，
    写死在工具和提示词里必然与库里实际值漂移。
    """
    now = _now().timestamp()
    if use_cache:
        cached = _lookup_cache.get(kind)
        if cached and now - cached[0] < _LOOKUP_TTL_SECONDS:
            return cached[1]

    async with get_conn() as conn:
        rows = await _fetch_all(
            conn,
            """
            SELECT code, label, sort, fallback
              FROM lookups
             WHERE kind = %s AND active
             ORDER BY sort, code
            """,
            (kind,),
        )
    _lookup_cache[kind] = (now, rows)
    return rows


async def get_lookup_codes(kind: str) -> list[str]:
    """只要落库值，用于校验和给模型列候选。"""
    return [r["code"] for r in await get_lookups(kind)]


async def resolve_lookup(kind: str, value: str) -> str | None:
    """把 Agent 传来的值映射成合法的落库值。

    - 精确命中 code 或 label → 返回 code（模型可能传中文展示名）
    - 都没命中 → 归一到该 kind 的 fallback 项（没有 fallback 则返回 None）

    **不做「命中不了就拒绝」**：那会让一条真实诉求因为分类名对不上而丢失，
    归类失败的正确处理是降级到「其他」，而不是把用户的投诉弹回去。
    """
    rows = await get_lookups(kind)
    key = (value or "").strip()
    if not key:
        return None
    for r in rows:
        if key == r["code"] or key == r["label"]:
            return r["code"]
    for r in rows:
        if r.get("fallback"):
            return r["code"]
    return None


def invalidate_lookup_cache(kind: str | None = None) -> None:
    """字典改动后主动失效（造数 / 测试用）。"""
    if kind is None:
        _lookup_cache.clear()
    else:
        _lookup_cache.pop(kind, None)


# ---------------------------------------------------------------------------
# 会员
# ---------------------------------------------------------------------------


async def get_member(member_id: str) -> dict[str, Any] | None:
    """按会员 ID 查会员基础信息（member_id / level / created_at）。"""
    async with get_conn() as conn:
        return await _fetch_one(conn, "SELECT * FROM members WHERE member_id = %s", (member_id,))


async def get_member_status(member_id: str) -> dict[str, Any] | None:
    """按会员 ID 查会员状态（status / effective_at / expire_at / auto_renew）。"""
    async with get_conn() as conn:
        return await _fetch_one(
            conn, "SELECT * FROM member_status WHERE member_id = %s", (member_id,)
        )


# ---------------------------------------------------------------------------
# 权益
# ---------------------------------------------------------------------------


async def get_benefits_by_level(level: str) -> list[dict[str, Any]]:
    """列出某个等级下的全部权益规则。"""
    async with get_conn() as conn:
        return await _fetch_all(
            conn, "SELECT * FROM benefits WHERE level = %s ORDER BY code", (level,)
        )


async def get_benefit_by_code(code: str) -> dict[str, Any] | None:
    """按权益编码查单条权益规则。"""
    async with get_conn() as conn:
        return await _fetch_one(conn, "SELECT * FROM benefits WHERE code = %s", (code,))


async def search_benefits(
    keyword: str, level: str | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """按关键词模糊搜索权益（匹配 name 或 value），可选按等级过滤。

    ``LIKE`` 里的 ``%`` / ``_`` 会被转义成字面量，避免用户输入把整表匹配出来。
    """
    pattern = "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    sql = "SELECT * FROM benefits WHERE (name LIKE %s ESCAPE '\\' OR value LIKE %s ESCAPE '\\')"
    params: list[Any] = [pattern, pattern]
    if level:
        sql += " AND level = %s"
        params.append(level)
    sql += " ORDER BY length(name), code LIMIT %s"
    params.append(max(1, int(limit)))

    async with get_conn() as conn:
        return await _fetch_all(conn, sql, tuple(params))


# ---------------------------------------------------------------------------
# 订单
# ---------------------------------------------------------------------------


async def get_orders(member_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """列出某个会员最近的下单记录（按创建时间倒序）。"""
    async with get_conn() as conn:
        return await _fetch_all(
            conn,
            "SELECT * FROM orders WHERE member_id = %s ORDER BY created_at DESC LIMIT %s",
            (member_id, max(1, int(limit))),
        )


async def get_order(order_id: str) -> dict[str, Any] | None:
    """按订单号查单条订单。"""
    async with get_conn() as conn:
        return await _fetch_one(conn, "SELECT * FROM orders WHERE order_id = %s", (order_id,))


# ---------------------------------------------------------------------------
# 审批
# ---------------------------------------------------------------------------


async def create_approval(
    thread_id: str,
    member_id: str,
    type: str,
    amount: int | None,
    reason: str | None,
    expires_at: datetime,
) -> dict[str, Any]:
    """新建一条待审批的申请单，返回创建后的完整记录。"""
    apply_id = f"AP{datetime.now(UTC):%Y%m%d%H%M%S}{uuid.uuid4().hex[:6].upper()}"
    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO approvals
                (apply_id, thread_id, member_id, type, amount, reason,
                 status, version, reviewer, created_at, decided_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', 0, NULL, %s, NULL, %s)
            """,
            (
                apply_id,
                thread_id,
                member_id,
                type,
                amount,
                reason,
                _now(),
                expires_at,
            ),
        )
        await conn.commit()
        result = await _fetch_one(conn, "SELECT * FROM approvals WHERE apply_id = %s", (apply_id,))
    assert result is not None  # 刚插入一定存在
    return result


async def get_approval(apply_id: str) -> dict[str, Any] | None:
    """按申请单号查审批单。"""
    async with get_conn() as conn:
        return await _fetch_one(conn, "SELECT * FROM approvals WHERE apply_id = %s", (apply_id,))


async def list_approvals(
    status: str = "pending", page: int = 1, page_size: int = 20
) -> tuple[list[dict[str, Any]], int]:
    """分页列出某一状态的审批单，返回 ``(当前页数据, 总数)``。"""
    page = max(1, int(page))
    page_size = max(1, int(page_size))
    where = "" if status in ("", "all", None) else " WHERE status = %s"
    params: list[Any] = [] if not where else [status]

    async with get_conn() as conn:
        total_row = await _fetch_one(
            conn, f"SELECT COUNT(*) AS n FROM approvals{where}", tuple(params)
        )
        total = int(total_row["n"]) if total_row else 0

        rows = await _fetch_all(
            conn,
            f"SELECT * FROM approvals{where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            tuple(params + [page_size, (page - 1) * page_size]),
        )
    return rows, total


async def cas_update_approval(
    apply_id: str, approved: bool, reviewer: str, comment: str
) -> dict[str, Any] | None:
    """用 CAS（乐观锁）方式把待审批单置为 approved / rejected。

    只有当前状态仍是 ``pending`` 且尚未过期时才会更新。影响行数为 0 说明
    这单已被别人处理或已过期，返回 ``None``。

    在 PostgreSQL 上，这条 UPDATE 会对命中的行加**行级排他锁**：两个并发
    事务里只有一个 ``rowcount == 1``，另一个阻塞到前者提交后看到 ``status``
    已变、``rowcount == 0``。这是防重复审批真正成立的地方。
    """
    now = _now()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE approvals
                   SET status = %s, reviewer = %s, comment = %s, decided_at = %s,
                       version = version + 1
                 WHERE apply_id = %s AND status = 'pending' AND expires_at > %s
                """,
                (
                    "approved" if approved else "rejected",
                    reviewer,
                    comment,
                    now,
                    apply_id,
                    now,
                ),
            )
            if cur.rowcount == 0:
                await conn.rollback()
                return None
            await conn.commit()
        return await _fetch_one(conn, "SELECT * FROM approvals WHERE apply_id = %s", (apply_id,))


async def expire_stale_approvals() -> int:
    """把所有已过期但仍为 pending 的审批单改成 expired，返回影响行数。"""
    now = _now()
    async with get_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            """
                UPDATE approvals
                   SET status = 'expired', decided_at = %s, version = version + 1
                 WHERE status = 'pending' AND expires_at <= %s
                """,
            (now, now),
        )
        affected = cur.rowcount
        await conn.commit()
    return int(affected or 0)


# ---------------------------------------------------------------------------
# 投诉
# ---------------------------------------------------------------------------


async def create_complaint(
    member_id: str,
    category: str,
    priority: int,
    sla_deadline: datetime,
    summary: str = "",
) -> dict[str, Any]:
    """新建一条投诉工单，返回创建后的完整记录。

    ``summary`` 必须落库：之前工具收了这个参数却没存，
    工单里只剩分类和优先级，投诉正文永久丢失，客服回看时不知道用户到底说了什么。
    """
    ticket_id = f"TK{datetime.now(UTC):%Y%m%d%H%M%S}{uuid.uuid4().hex[:6].upper()}"
    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO complaints
                (ticket_id, member_id, category, priority, sla_deadline, status, summary)
            VALUES (%s, %s, %s, %s, %s, 'open', %s)
            """,
            (ticket_id, member_id, category, int(priority), sla_deadline, summary),
        )
        await conn.commit()
        result = await _fetch_one(
            conn, "SELECT * FROM complaints WHERE ticket_id = %s", (ticket_id,)
        )
    assert result is not None
    return result


async def get_complaints(member_id: str) -> list[dict[str, Any]]:
    """列出某个会员的全部投诉工单（按 SLA 截止时间升序，最紧急的在前）。"""
    async with get_conn() as conn:
        return await _fetch_all(
            conn,
            "SELECT * FROM complaints WHERE member_id = %s ORDER BY sla_deadline ASC",
            (member_id,),
        )


# ---------------------------------------------------------------------------
# 会员画像（jsonb）
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    """jsonb 读出来可能是 dict（psycopg 配了 loader）也可能是 str。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, dict) else {}
        except ValueError:
            return {}
    return {}


async def get_member_profile(member_id: str) -> dict[str, Any] | None:
    """读取会员画像，``profile`` 一定是 dict。"""
    async with get_conn() as conn:
        row = await _fetch_one(
            conn, "SELECT * FROM member_profile WHERE member_id = %s", (member_id,)
        )
    if row is None:
        return None
    row["profile"] = _as_dict(row.get("profile"))
    return row


async def upsert_member_profile(member_id: str, profile: dict[str, Any]) -> None:
    """插入或更新会员画像。"""
    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO member_profile (member_id, profile, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (member_id) DO UPDATE SET
                profile = excluded.profile,
                updated_at = excluded.updated_at
            """,
            (member_id, Jsonb(profile), _now()),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


async def create_session(thread_id: str, member_id: str | None, title: str) -> dict[str, Any]:
    """新建会话记录，返回创建后的完整记录。"""
    now = _now()
    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO sessions
                (thread_id, member_id, title, message_count, last_message, created_at, updated_at)
            VALUES (%s, %s, %s, 0, NULL, %s, %s)
            ON CONFLICT (thread_id) DO NOTHING
            """,
            (thread_id, member_id, title, now, now),
        )
        await conn.commit()
        result = await _fetch_one(
            conn, "SELECT * FROM sessions WHERE thread_id = %s", (thread_id,)
        )
    assert result is not None
    return result


async def get_session(thread_id: str) -> dict[str, Any] | None:
    """按会话 ID 查会话。"""
    async with get_conn() as conn:
        return await _fetch_one(conn, "SELECT * FROM sessions WHERE thread_id = %s", (thread_id,))


async def list_sessions(page: int = 1, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
    """分页列出会话（最近更新的在前），返回 ``(当前页数据, 总数)``。"""
    page = max(1, int(page))
    page_size = max(1, int(page_size))
    async with get_conn() as conn:
        total_row = await _fetch_one(conn, "SELECT COUNT(*) AS n FROM sessions")
        total = int(total_row["n"]) if total_row else 0

        rows = await _fetch_all(
            conn,
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT %s OFFSET %s",
            (page_size, (page - 1) * page_size),
        )
    return rows, total


async def touch_session(thread_id: str, last_message: str) -> None:
    """更新会话的最后一条消息、消息数 +1 与更新时间。"""
    now = _now()
    async with get_conn() as conn:
        await conn.execute(
            """
            UPDATE sessions
               SET last_message = %s,
                   message_count = message_count + 1,
                   updated_at = %s
             WHERE thread_id = %s
            """,
            (last_message, now, thread_id),
        )
        await conn.commit()


async def delete_session(thread_id: str) -> None:
    """删除会话记录（对应的 traces 由调用方自行决定是否清理）。"""
    async with get_conn() as conn:
        await conn.execute("DELETE FROM sessions WHERE thread_id = %s", (thread_id,))
        await conn.commit()


# ---------------------------------------------------------------------------
# 链路追踪
# ---------------------------------------------------------------------------


async def get_next_turn(thread_id: str) -> int:
    """返回该会话下一个可用的轮次号（当前最大 turn + 1，空会话从 1 开始）。"""
    async with get_conn() as conn:
        row = await _fetch_one(
            conn, "SELECT MAX(turn) AS max_turn FROM traces WHERE thread_id = %s", (thread_id,)
        )
    max_turn = row.get("max_turn") if row else None
    return (int(max_turn) if max_turn is not None else 0) + 1


async def record_trace(
    trace_id: str,
    thread_id: str,
    turn: int,
    intent: str | None,
    route: str | None,
    tools: Any,
    latency_ms: int | None,
    tokens_in: int | None,
    tokens_out: int | None,
    degraded: bool | int,
) -> None:
    """写入一条链路追踪记录。``tools`` 可传 list 或 JSON 字符串，落 jsonb。"""
    payload: Any = tools
    if isinstance(tools, str):
        try:
            payload = json.loads(tools)
        except ValueError:
            payload = []
    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO traces
                (trace_id, thread_id, turn, intent, route, tools,
                 latency_ms, tokens_in, tokens_out, degraded, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                trace_id,
                thread_id,
                int(turn),
                intent,
                route,
                Jsonb(payload),
                latency_ms,
                tokens_in,
                tokens_out,
                bool(degraded),
                _now(),
            ),
        )
        await conn.commit()


async def get_traces(thread_id: str) -> list[dict[str, Any]]:
    """列出某个会话的所有追踪记录，按轮次升序。"""
    async with get_conn() as conn:
        return await _fetch_all(
            conn,
            "SELECT * FROM traces WHERE thread_id = %s ORDER BY turn ASC, created_at ASC",
            (thread_id,),
        )
