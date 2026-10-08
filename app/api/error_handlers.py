"""One consistent error envelope for every failure mode.

Every error the API can produce — an `HTTPException` raised in a route, a request
validation failure, an `ApiError` raised deep in the engine — comes back shaped
the same, so a client only ever has to parse one thing:

    {"error": {"code": "...", "message": "..."}}

`register` installs the handlers on the FastAPI app; `batch_http_error` lets a
route lift a service-layer `BatchError` into a normal HTTPException.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..core.errors import ApiError
from ..services.batch import BatchError


async def http_exception_handler(request, exc: HTTPException):
    detail = exc.detail
    body = detail if isinstance(detail, dict) and "error" in detail else {
        "error": {"code": "http_error", "message": str(detail)}
    }
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error",
                           "message": jsonable_encoder(exc.errors())}},
    )


async def api_error_handler(request, exc: ApiError):
    return JSONResponse(status_code=exc.status,
                        content={"error": {"code": exc.code, "message": exc.message}})


def batch_http_error(exc: BatchError) -> HTTPException:
    """A service-layer BatchError, already carrying its own code and status."""
    return HTTPException(
        status_code=exc.status,
        detail={"error": {"code": exc.code, "message": exc.message}},
    )


def register(app: FastAPI) -> None:
    """Install every handler on `app`."""
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError,
                              validation_exception_handler)
    app.add_exception_handler(ApiError, api_error_handler)
