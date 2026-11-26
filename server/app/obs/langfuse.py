"""Langfuse 可观测。

没配 key 时返回 None，LangChain 会自动忽略，本地开发不受影响。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.config import settings


@lru_cache
def get_langfuse_handler() -> Any | None:
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception:
        # 可观测不能阻断主流程
        return None
