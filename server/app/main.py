"""FastAPI 入口。"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import approvals, chat, sessions
from app.api.errors import AppError, error_response
from app.config import settings
from app.db import init_db
from app.db.connection import close_pool
from app.graph.checkpointer import close_checkpointer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("数据库就绪: %s", settings.db_endpoint())
    if not settings.llm_configured():
        logger.warning("LLM 未配置，对话接口会失败。请检查 server/.env")
    yield
    await close_checkpointer()
    await close_pool()


def create_app() -> FastAPI:
    app = FastAPI(
        title="客服 Agent",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ---- CORS：开发期前端直连（vite 默认端口 5180）----
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5180",
            "http://127.0.0.1:5180",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- 请求日志 ----
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s -> %d (%.0fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
        )
        return response

    # ---- 全局异常处理：不把栈泄露给前端 ----
    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        # 业务错误必须带上 error_no / error_msg，前端靠它区分 409「已被处理」等情况。
        # 不注册这个 handler 的话 FastAPI 只回 {"detail": "..."}，业务码就丢了。
        return error_response(
            error_no=exc.error_no,
            error_msg=exc.error_msg,
            status_code=exc.status_code,
            detail=exc.detail,
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.exception("未捕获异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error_no": 50001,
                "error_msg": "服务内部错误，请稍后重试",
                "detail": None,
            },
        )

    app.include_router(chat.router)
    app.include_router(approvals.router)
    app.include_router(sessions.router)

    @app.get("/health")
    async def health():
        from app.llm import llm_ready

        return {"ok": True, "llm_configured": llm_ready()}

    return app


app = create_app()
