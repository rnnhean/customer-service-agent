"""LangGraph 持久化 checkpoint（PostgreSQL）。

用 ``AsyncPostgresSaver``，不用 ``MemorySaver``——后者进程重启现场就没了，
HITL 的恢复能力直接失效；也不能用 SQLite saver，多实例部署时会锁库。

**连接池为什么是独立的一套**：``AsyncPostgresSaver`` 要求连接 ``autocommit=True``
（它的 ``setup()`` 和内部写入都按自动提交设计）。如果和业务连接池共用，
业务层的 ``rollback()``（CAS 抢不到时要回滚）就失去意义。所以这里单独建一个小池，
把它的 ``max_size`` 压住，保证「业务池 + checkpoint 池 ≤ PG 的 max_connections」。
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings

_saver = None
_pool: AsyncConnectionPool | None = None


async def get_checkpointer():
    """惰性初始化，返回全局单例。"""
    global _saver, _pool
    if _saver is not None:
        return _saver

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    _pool = AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=1,
        max_size=5,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
        name="csa-checkpoint-pool",
    )
    await _pool.open(wait=True, timeout=30)

    _saver = AsyncPostgresSaver(_pool)
    await _saver.setup()
    return _saver


async def close_checkpointer() -> None:
    global _saver, _pool
    if _pool is not None:
        await _pool.close()
    _pool = None
    _saver = None


async def delete_thread(thread_id: str) -> None:
    """删掉某个会话的所有 checkpoint。删会话时用。"""
    saver = await get_checkpointer()
    await saver.adelete_thread(thread_id)
