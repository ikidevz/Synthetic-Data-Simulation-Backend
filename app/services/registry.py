"""Database-backed registry: providers, their API keys, and the configs they published.

A *provider* is a publisher of configs (an `id` and a `full_name`). Each provider
authenticates with one or more API keys. Keys are random 256-bit values; only their
SHA-256 is stored (a fast hash is appropriate precisely because the secret is
high-entropy — there is nothing to brute-force), and the key itself is returned
exactly once, when it is issued.

The `API_KEY` / `API_KEYS` environment variables remain the *superuser* keys: they
create providers and can see and change everything. They never touch this table.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, insert, select, update

from ..core.errors import ApiError
from ..db import models
from ..db.engine import engine

logger = logging.getLogger("registry")

SYSTEM_PROVIDER_ID = "prov_system"
SYSTEM_PROVIDER_NAME = "System (built-in examples)"
MAX_FULL_NAME = 100
_LAST_USED_REFRESH = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str, nbytes: int = 6) -> str:
    return f"{prefix}_{secrets.token_hex(nbytes)}"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def clean_full_name(value: Any) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not name or len(name) > MAX_FULL_NAME:
        raise ApiError("invalid_provider",
                       f"full_name must be 1-{MAX_FULL_NAME} characters", 422)
    return name


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


# ------------------------------------------------------------ providers ----
def _provider_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "is_active": bool(row["is_active"]),
        "is_system": bool(row["is_system"]),
        "created_at": _iso(row["created_at"]),
    }


def ensure_system_provider() -> None:
    """The owner of the built-in YAML examples. It has no API key: only superuser keys act for it."""
    with engine.begin() as conn:
        exists = conn.execute(
            select(models.registry_providers.c.id).where(
                models.registry_providers.c.id == SYSTEM_PROVIDER_ID)
        ).first()
        if not exists:
            conn.execute(
                insert(models.registry_providers).values(
                    id=SYSTEM_PROVIDER_ID, full_name=SYSTEM_PROVIDER_NAME,
                    is_active=True, is_system=True, created_at=_now(),
                )
            )


def create_provider(full_name: Any) -> Dict[str, Any]:
    name = clean_full_name(full_name)
    pid = new_id("prov")
    with engine.begin() as conn:
        conn.execute(
            insert(models.registry_providers).values(
                id=pid, full_name=name, is_active=True, is_system=False, created_at=_now()
            )
        )
        row = conn.execute(select(models.registry_providers).where(
            models.registry_providers.c.id == pid)).mappings().one()
    return _provider_dict(dict(row))


def get_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    with engine.connect() as conn:
        row = conn.execute(
            select(models.registry_providers).where(
                models.registry_providers.c.id == provider_id)
        ).mappings().first()
    return _provider_dict(dict(row)) if row else None


def provider_names() -> Dict[str, str]:
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(select(models.registry_providers.c.id, models.registry_providers.c.full_name))}


def update_provider(provider_id: str, full_name: Any = None, is_active: Optional[bool] = None) -> Dict[str, Any]:
    current = get_provider(provider_id)
    if not current:
        raise ApiError("not_found", f"provider '{provider_id}' not found", 404)
    if current["is_system"]:
        raise ApiError("not_supported",
                       "the built-in system provider can't be modified", 409)
    values: Dict[str, Any] = {}
    if full_name is not None:
        values["full_name"] = clean_full_name(full_name)
    if is_active is not None:
        values["is_active"] = bool(is_active)
    if values:
        with engine.begin() as conn:
            conn.execute(
                update(models.registry_providers).where(
                    models.registry_providers.c.id == provider_id).values(**values)
            )
    return get_provider(provider_id)  # type: ignore[return-value]


def list_providers() -> List[Dict[str, Any]]:
    """Every provider with its keys (never the secrets) and how many configs it owns."""
    with engine.connect() as conn:
        providers = [dict(r) for r in conn.execute(
            select(models.registry_providers).order_by(
                models.registry_providers.c.created_at, models.registry_providers.c.id)
        ).mappings()]
        keys = [dict(r) for r in conn.execute(
            select(models.registry_api_keys).order_by(
                models.registry_api_keys.c.created_at)
        ).mappings()]
        counts = dict(conn.execute(
            select(models.registry_configs.c.provider_id, func.count()
                   ).group_by(models.registry_configs.c.provider_id)
        ).all())
    out = []
    for p in providers:
        item = _provider_dict(p)
        item["config_count"] = counts.get(p["id"], 0)
        item["keys"] = [_key_dict(k)
                        for k in keys if k["provider_id"] == p["id"]]
        out.append(item)
    return out


# ----------------------------------------------------------------- keys ----
def _key_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "key_prefix": row["key_prefix"],
        "label": row["label"],
        "created_at": _iso(row["created_at"]),
        "revoked_at": _iso(row["revoked_at"]),
        "last_used_at": _iso(row["last_used_at"]),
    }


def issue_key(provider_id: str, label: Optional[str] = None) -> Dict[str, Any]:
    """Create a key for a provider. The returned dict is the ONLY place the secret appears."""
    provider = get_provider(provider_id)
    if not provider:
        raise ApiError("not_found", f"provider '{provider_id}' not found", 404)
    if provider["is_system"]:
        raise ApiError(
            "not_supported", "the built-in system provider acts through the superuser keys; it takes no key", 409)
    if label is not None and (not isinstance(label, str) or len(label) > 100):
        raise ApiError("invalid_label",
                       "label must be a string of at most 100 characters", 422)
    secret = secrets.token_urlsafe(32)
    key_id = new_id("key")
    with engine.begin() as conn:
        conn.execute(
            insert(models.registry_api_keys).values(
                id=key_id, provider_id=provider_id, key_hash=hash_key(secret),
                key_prefix=secret[:8], label=label, created_at=_now(),
            )
        )
        row = conn.execute(select(models.registry_api_keys).where(
            models.registry_api_keys.c.id == key_id)).mappings().one()
    return {**_key_dict(dict(row)), "api_key": secret}


def revoke_key(provider_id: str, key_id: str) -> Dict[str, Any]:
    with engine.begin() as conn:
        row = conn.execute(
            select(models.registry_api_keys).where(
                models.registry_api_keys.c.id == key_id, models.registry_api_keys.c.provider_id == provider_id
            )
        ).mappings().first()
        if not row:
            raise ApiError(
                "not_found", f"key '{key_id}' not found for provider '{provider_id}'", 404)
        if row["revoked_at"] is None:
            conn.execute(
                update(models.registry_api_keys).where(
                    models.registry_api_keys.c.id == key_id).values(revoked_at=_now())
            )
        row = conn.execute(select(models.registry_api_keys).where(
            models.registry_api_keys.c.id == key_id)).mappings().one()
    return _key_dict(dict(row))


def authenticate(key: str) -> Optional[Dict[str, Any]]:
    """The active provider that owns this key, or None. Looks the key up by its hash."""
    k, p = models.registry_api_keys, models.registry_providers
    with engine.connect() as conn:
        row = conn.execute(
            select(k.c.id, k.c.last_used_at, p.c.id.label(
                "provider_id"), p.c.full_name)
            .join(p, p.c.id == k.c.provider_id)
            .where(k.c.key_hash == hash_key(key), k.c.revoked_at.is_(None), p.c.is_active.is_(True))
        ).mappings().first()
    if not row:
        return None
    last = row["last_used_at"]
    if last is not None and last.tzinfo is None:
        # SQLite hands back naive datetimes
        last = last.replace(tzinfo=timezone.utc)
    if last is None or _now() - last > _LAST_USED_REFRESH:
        try:  # bookkeeping only: never fail (or block) a request over it
            with engine.begin() as conn:
                conn.execute(update(k).where(
                    k.c.id == row["id"]).values(last_used_at=_now()))
        except Exception:  # pragma: no cover - e.g. a locked SQLite file
            logger.debug("could not update last_used_at", exc_info=True)
    return {"provider_id": row["provider_id"], "full_name": row["full_name"], "key_id": row["id"]}


# -------------------------------------------------------------- configs ----
def list_config_rows(source: Optional[str] = None) -> List[Dict[str, Any]]:
    c = models.registry_configs
    stmt = select(c).order_by(c.c.created_at, c.c.id)
    if source:
        stmt = stmt.where(c.c.source == source)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def insert_config_row(row: Dict[str, Any]) -> None:
    with engine.begin() as conn:
        conn.execute(insert(models.registry_configs).values(**row))


def update_config_row(config_id: str, **values: Any) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(models.registry_configs).where(
                models.registry_configs.c.id == config_id)
            .values(updated_at=_now(), **values)
        )


def delete_config_row(config_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(delete(models.registry_configs).where(
            models.registry_configs.c.id == config_id))


def sync_yaml_rows(rows: List[Dict[str, Any]]) -> None:
    """Make the registry's "yaml" rows mirror the YAML files exactly: the files are the
    source of truth for those configs. `is_only_me` is preserved across restarts."""
    c = models.registry_configs
    wanted = {r["id"] for r in rows}
    with engine.begin() as conn:
        existing = {r[0] for r in conn.execute(
            select(c.c.id).where(c.c.source == "yaml"))}
        for gone in existing - wanted:
            conn.execute(delete(c).where(c.c.id == gone))
        for r in rows:
            if r["id"] in existing:
                conn.execute(update(c).where(c.c.id == r["id"]).values(
                    config_json=r["config_json"], physical_name=r["physical_name"], name=r["name"], updated_at=_now(
                    )
                ))
            else:
                conn.execute(insert(c).values(**r))
