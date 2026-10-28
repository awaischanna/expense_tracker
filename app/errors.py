"""One error format for the whole API:

{"error": {"code": "...", "message": "...", "details": [...]}}
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        details: list[Any] | None = None,
        headers: dict[str, str] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []
        self.headers = headers
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class BadRequest(AppError):
    status_code, code = 400, "bad_request"


class Unauthorized(AppError):
    status_code, code = 401, "unauthorized"

    def __init__(self, message: str = "Invalid or expired token", **kw: Any) -> None:
        kw.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(message, **kw)


class NotFound(AppError):
    status_code, code = 404, "not_found"

    def __init__(self, message: str = "Resource not found", **kw: Any) -> None:
        super().__init__(message, **kw)


class Conflict(AppError):
    status_code, code = 409, "conflict"


class PreconditionFailed(AppError):
    status_code, code = 412, "version_conflict"


class Unprocessable(AppError):
    status_code, code = 422, "validation_error"


class PreconditionRequired(AppError):
    status_code, code = 428, "precondition_required"


class TooManyRequests(AppError):
    status_code, code = 429, "rate_limited"


def error_body(code: str, message: str, details: list[Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or []}}


_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    412: "version_conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    428: "precondition_required",
    429: "rate_limited",
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, exc.details), status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse(error_body("validation_error", "Request validation failed", details), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        return JSONResponse(
            error_body(code, str(exc.detail)), status_code=exc.status_code, headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:  # pragma: no cover - safety net
        logger.exception("Unhandled error", exc_info=exc)
        return JSONResponse(error_body("internal_error", "Internal server error"), status_code=500)
