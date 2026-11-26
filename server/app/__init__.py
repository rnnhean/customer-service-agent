"""客服 Agent 后端包。

Windows 上 psycopg 的异步模式**不支持**默认的 ``ProactorEventLoop``，
必须在任何事件循环创建之前切到 ``SelectorEventLoop``。放在包入口，
保证 ``import app.*`` 的进程（uvicorn、seed、pytest）都会生效。
"""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
