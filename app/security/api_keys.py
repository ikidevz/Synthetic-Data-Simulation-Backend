"""API-key authentication — secure by default.

  * Declared as an OpenAPI security scheme, so /docs gets an "Authorize" button.
  * Applied GLOBALLY (see `FastAPI(dependencies=[...])` in main.py): every route
    requires the key unless its path is in PUBLIC_PATHS. A route added later is
    protected automatically; you have to opt a route OUT, never IN.
  * Header only (`X-API-Key`). Keys in query strings leak into logs and history,
    so they are deliberately not accepted.
  * Constant-time comparison, and several keys at once so you can rotate them:
    add the new key, move clients over, then remove the old one.
  * 401 with a `WWW-Authenticate` header and the standard error envelope.

Who is calling? (identity)
  * A key from API_KEY / API_KEYS is a SUPERUSER key: it can read and write everything
    (every provider's configs and data, including ones marked is_only_me), creates and
    manages providers and their keys, and is the only kind that reaches the legacy
    routes (/orders, /admin/batch, /metrics, ...).
  * A key issued to a provider (POST /v1/admin/providers) authenticates that provider.
    Provider keys are stored hashed in the database (see registry.py).
  * With no superuser key configured, authentication is off and every caller is an
    anonymous superuser — provider keys only take effect once a superuser key is set.
  `verify_api_key` resolves the caller to a `Principal` and stores it on
  `request.state`; routes read it through the `current_principal` dependency.

Configuration (environment):
  API_KEY / API_KEYS    one key, or comma-separated keys (both are read)
  REQUIRE_API_KEY=true  refuse to start without a key (use this in deployments)
  ENABLE_DOCS=false     turn off /docs, /redoc and /openapi.json

With no key configured, auth is OFF — convenient locally and in tests, and
logged as a warning at startup. Generate a key with:  python -m app.cli genkey
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import List, Optional

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

logger = logging.getLogger("security")

HEADER_NAME = "X-API-Key"
PUBLIC_PATHS = frozenset({"/health"})
MIN_RECOMMENDED_LENGTH = 24

api_key_header = APIKeyHeader(
    name=HEADER_NAME,
    auto_error=False,  # we raise our own 401 so the error envelope stays consistent
    description="API key. Generate one with `python -m app.cli genkey`.",
)


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_api_keys() -> List[str]:
    """Keys from API_KEYS (comma-separated) and API_KEY, trimmed and de-duplicated."""
    raw = f"{os.environ.get('API_KEYS', '')},{os.environ.get('API_KEY', '')}"
    keys: List[str] = []
    for key in raw.split(","):
        key = key.strip()
        if key and key not in keys:
            keys.append(key)
    return keys


# Read at import time. Tests replace this list to switch auth on and off.
API_KEYS: List[str] = load_api_keys()


def auth_required() -> bool:
    return _truthy(os.environ.get("REQUIRE_API_KEY"))


def docs_enabled() -> bool:
    value = os.environ.get("ENABLE_DOCS")
    return True if value is None else _truthy(value)


def key_is_valid(provided: Optional[str], keys: Optional[List[str]] = None) -> bool:
    """Constant-time check against every configured key (no early exit)."""
    keys = API_KEYS if keys is None else keys
    if not provided:
        return False
    candidate = provided.encode("utf-8")
    valid = False
    for key in keys:
        valid |= secrets.compare_digest(candidate, key.encode("utf-8"))
    return valid


def check_configuration(keys: Optional[List[str]] = None, require: Optional[bool] = None) -> None:
    """Called at startup. Fails fast when auth is required but no key is set."""
    keys = API_KEYS if keys is None else keys
    require = auth_required() if require is None else require
    if require and not keys:
        raise RuntimeError(
            "REQUIRE_API_KEY is set but no API key is configured. Set API_KEY "
            "(or API_KEYS=key1,key2). Generate one with: python -m app.cli genkey"
        )
    if not keys:
        logger.warning(
            "No API key configured: authentication is DISABLED and every route is open. "
            "Set API_KEY before exposing this beyond your machine."
        )
    for key in keys:
        if len(key) < MIN_RECOMMENDED_LENGTH:
            logger.warning(
                "An API key is shorter than %d characters. Use `python -m app.cli genkey`.",
                MIN_RECOMMENDED_LENGTH,
            )


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: the superuser (env key), or a provider (database key)."""

    role: str  # "superuser" | "provider"
    provider_id: Optional[str] = None
    full_name: str = "superuser"
    key_id: Optional[str] = None

    @property
    def is_superuser(self) -> bool:
        return self.role == "superuser"


SUPERUSER = Principal(role="superuser", full_name="superuser")
ANONYMOUS_SUPERUSER = Principal(
    role="superuser", full_name="anonymous (authentication disabled)")


def _unauthorized(request: Request) -> HTTPException:
    # Never log the key that was sent — only that a request failed.
    logger.warning(
        "auth failed: %s %s from %s",
        request.method, request.url.path, request.client.host if request.client else "unknown",
    )
    return HTTPException(
        status_code=401,
        detail={"error": {"code": "unauthorized",
                          "message": "invalid or missing API key"}},
        headers={"WWW-Authenticate": "ApiKey"},
    )


def verify_api_key(request: Request, provided: Optional[str] = Security(api_key_header)) -> None:
    """Global dependency: reject the request unless it carries a valid key, and
    record who the caller is."""
    if request.url.path in PUBLIC_PATHS:
        return
    # authentication is off: everyone is an (anonymous) superuser
    if not API_KEYS:
        request.state.principal = ANONYMOUS_SUPERUSER
        return
    if key_is_valid(provided):
        request.state.principal = SUPERUSER
        return
    if provided:
        # imported here: registry needs the engine, this module must stay light
        from ..services import registry

        found = registry.authenticate(provided)
        if found:
            request.state.principal = Principal(
                role="provider", provider_id=found["provider_id"],
                full_name=found["full_name"], key_id=found["key_id"],
            )
            return
    raise _unauthorized(request)


def current_principal(request: Request) -> Principal:
    """Route dependency: who is calling (set by the global `verify_api_key`)."""
    principal = getattr(request.state, "principal", None)
    if principal is None:  # only possible for a PUBLIC_PATHS route
        raise _unauthorized(request)
    return principal


def require_superuser(principal: Principal = Depends(current_principal)) -> Principal:
    """Route dependency: superuser keys only."""
    if not principal.is_superuser:
        raise HTTPException(
            status_code=403,
            detail={"error": {"code": "forbidden",
                              "message": "this route needs a superuser API key"}},
        )
    return principal
