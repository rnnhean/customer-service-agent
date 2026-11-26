"""应用配置。

所有敏感值只从环境变量读取，绝不打印。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- LLM ----
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.2
    llm_timeout_seconds: int = 60

    # ---- Embedding（本期不启用，留位）----
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_dimension: int = 1024

    # ---- Langfuse ----
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ---- 服务 ----
    app_host: str = "127.0.0.1"
    # 8000 / 8088 / 3001 / 5173 在本机已被占用，后端统一 8020、前端 5180
    app_port: int = 8020
    # 前端地址。写死在代码里的话，换端口/上域名就会整站 CORS 失败
    cors_origins: list[str] = ["http://localhost:5180", "http://127.0.0.1:5180"]

    # ---- PostgreSQL ----
    # 本地默认指向 docker-compose 起的 csa_postgres（5433，不占 5432）
    database_url: str = "postgresql://csa:csa_local_dev@127.0.0.1:5433/customer_service"
    db_pool_min: int = 2
    db_pool_max: int = 20

    # ---- 业务参数 ----
    rag_min_score: float = 0.35
    approval_ttl_hours: int = 24
    classify_confidence_threshold: float = 0.6
    history_window_messages: int = 6
    #: 子图 ReAct 循环上限。Agent 自己决定调几次工具，但总得有个天花板防止烧 token
    agent_recursion_limit: int = 12
    #: 投诉优先级 -> 首次响应时限（小时）。造数和建单共用这一份，
    #: 否则新旧工单同一优先级会有两套 SLA。
    complaint_sla_hours: dict[int, int] = {1: 2, 2: 8, 3: 24}

    def llm_configured(self) -> bool:
        """只判断是否配置，不暴露值。"""
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    def db_endpoint(self) -> str:
        """只返回 host/db，不含口令，方便日志里确认连的是哪个库。"""
        tail = self.database_url.rsplit("@", 1)[-1]
        return tail


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
