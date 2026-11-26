"""PostgreSQL 连接池：建库、建表与连接上下文。

设计要点：

- 全进程共用**一个** ``AsyncConnectionPool``（不是每个请求新建连接）。
  池上限受 ``DB_POOL_MAX`` 控制，部署时压在 PG 的 ``max_connections`` 之下。
- 所有连接默认 ``row_factory=dict_row``，调用方拿到行就能 ``dict(row)``。
- 时间列是 ``timestamptz``，psycopg 返回 ``datetime``；由 :mod:`app.db.repo`
  统一转成 ISO 字符串，避免 datetime 漏进 SSE 的 JSON 编码。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

#: 建表脚本，与本模块放在同一目录
SCHEMA_PATH: Path = Path(__file__).with_name("schema.sql")

_pool = None


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串，给外部展示 / 拼接提示词用。

    注意：**不要把它的返回值再塞回 SQL 比较**，那里直接用 ``datetime``，
    让 PG 自己去比 ``timestamptz``。
    """
    return datetime.now(UTC).isoformat()


async def get_pool():
    """惰性创建全局连接池。首次调用会等到池里至少有一条可用连接。"""
    global _pool
    if _pool is not None:
        return _pool

    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    _pool = AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        kwargs={"row_factory": dict_row},
        open=False,
        name="csa-pool",
    )
    await _pool.open(wait=True, timeout=30)
    logger.info("PG 连接池就绪 (min=%d, max=%d)", settings.db_pool_min, settings.db_pool_max)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
    _pool = None


@asynccontextmanager
async def get_conn() -> AsyncIterator:
    """从池里借一条连接，用完自动归还。

    正常退出时提交，抛异常时回滚——这是 ``psycopg_pool`` 的内置行为，
    所以调用方不需要在每个函数末尾写 ``commit()``。显式写也不会出错。
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        yield conn


async def init_db() -> None:
    """执行 ``schema.sql``，把所有表与索引建出来。

    幂等：脚本里全部是 ``CREATE ... IF NOT EXISTS``。多实例部署时只应有一个
    实例在启动阶段执行（或者抽成 init Job），否则会并发抢 DDL 锁。
    """
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    async with get_conn() as conn:
        await conn.execute(schema)
        await conn.commit()
