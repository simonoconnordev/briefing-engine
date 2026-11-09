"""Errors as RFC 9457 problem details, each carrying the request id (§5.1)."""

from contextvars import ContextVar
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

TITLES = {
    400: "Bad request",
    401: "Unauthorised",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    422: "Unprocessable content",
    503: "Service unavailable",
}


class Problem(Exception):
    def __init__(
        self,
        status: int,
        type_: str,
        detail: str,
        *,
        title: str | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(detail)
        self.status = status
        self.type = type_
        self.title = title or TITLES.get(status, "Error")
        self.detail = detail
        self.headers = headers or {}
        self.extra = extra or {}


def problem_response(
    status: int,
    type_: str,
    title: str,
    detail: str,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body = {
        "type": f"/problems/{type_}" if type_ != "about:blank" else type_,
        "title": title,
        "status": status,
        "detail": detail,
        "request_id": request_id_var.get(),
        **(extra or {}),
    }
    return JSONResponse(
        body, status_code=status, headers=headers, media_type="application/problem+json"
    )


def install(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _problem(_request: Request, exc: Problem) -> JSONResponse:
        return problem_response(exc.status, exc.type, exc.title, exc.detail, exc.headers, exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
            for e in exc.errors()
        ]
        return problem_response(
            422,
            "validation-error",
            "Unprocessable content",
            "The request has invalid fields.",
            extra={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(
            exc.status_code, "about:blank", TITLES.get(exc.status_code, "Error"), str(exc.detail)
        )
