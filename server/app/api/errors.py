"""统一响应与错误结构。"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse


class AppError(HTTPException):
    """业务错误。error_no 前三位是 HTTP 状态码，后两位是细分。"""

    def __init__(
        self,
        status_code: int,
        error_no: int,
        error_msg: str,
        detail: Any = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=error_msg)
        self.error_no = error_no
        self.error_msg = error_msg
        self.detail = detail


def error_response(
    error_no: int, error_msg: str, status_code: int, detail: Any = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error_no": error_no, "error_msg": error_msg, "detail": detail},
    )


# 常用错误
def err_bad_request(msg: str, detail: Any = None) -> AppError:
    return AppError(400, 40001, msg, detail)


def err_not_found(msg: str) -> AppError:
    return AppError(404, 40401, msg)


def err_conflict(msg: str, error_no: int = 40901) -> AppError:
    return AppError(409, error_no, msg)


def err_unprocessable(msg: str) -> AppError:
    return AppError(422, 42201, msg)
