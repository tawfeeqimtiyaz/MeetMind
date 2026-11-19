"""Consistent JSON error responses: {"error": {"code": ..., "message": ..., "details": ...}}."""
import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


def error_response(status: int, code: str, message: str, details=None, headers=None) -> JSONResponse:
    err = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return JSONResponse({"error": err}, status_code=status, headers=headers)


def _field_name(loc: tuple) -> str:
    # FastAPI prefixes locations with where the value came from: ("body", "email") -> "email"
    parts = loc[1:] if loc and loc[0] in {"body", "query", "path", "header"} else loc
    return ".".join(str(p) for p in parts) or str(loc[0])


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_: Request, e: APIError):
        return error_response(e.status, e.code, e.message, e.details, e.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, e: RequestValidationError):
        details = [{"field": _field_name(err["loc"]), "message": err["msg"]} for err in e.errors()]
        return error_response(422, "validation_error", "Invalid request data", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, e: StarletteHTTPException):
        try:
            phrase = HTTPStatus(e.status_code).phrase
        except ValueError:
            phrase = "Error"
        message = e.detail if isinstance(e.detail, str) else phrase
        return error_response(e.status_code, phrase.lower().replace(" ", "_"), message, headers=e.headers)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, e: Exception):
        log.exception("Unhandled error", exc_info=e)
        return error_response(500, "internal_error", "Something went wrong")
