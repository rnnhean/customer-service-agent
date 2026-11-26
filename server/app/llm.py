"""LLM 客户端工厂。

只从 settings 读配置，永不打印 key。走 OpenAI 兼容协议，换模型只改 .env。
"""

from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI

from app.config import settings


@lru_cache
def get_llm(temperature: float | None = None) -> ChatOpenAI:
    """返回全局复用的 ChatOpenAI 实例。

    没配 .env 时抛 RuntimeError（启动阶段就该拦住，不要等到第一次对话）。
    """
    if not settings.llm_configured():
        raise RuntimeError(
            "LLM 未配置：请在 server/.env 里设置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL"
        )

    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key,  # type: ignore[arg-type]  # pydantic 接受 SecretStr 或 str
        base_url=settings.llm_base_url,
        temperature=settings.llm_temperature if temperature is None else temperature,
        timeout=settings.llm_timeout_seconds,
        max_retries=2,
    )


def llm_ready() -> bool:
    """供健康检查用：只返回布尔，不碰实际调用。"""
    return settings.llm_configured()
