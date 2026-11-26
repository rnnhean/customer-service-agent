"""数据访问层：PostgreSQL 连接池、建表与仓储函数。

对外常用入口：

    from app.db import get_conn, init_db

    async with get_conn() as conn:
        ...
"""

from __future__ import annotations

from app.db.connection import close_pool, get_conn, get_pool, init_db

__all__ = ["close_pool", "get_conn", "get_pool", "init_db"]
