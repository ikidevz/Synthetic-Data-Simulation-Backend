"""Row-level operations on one entity, shared by the legacy per-entity routes
(`/orders`) and the per-config routes (`/v1/configs/{id}/data`).

Each function takes the entity's config and table, plus (where it creates rows) the
`tables` / `configs` it may reference — the whole built-in set for legacy routes, one
provider's configs for `/v1`. That scope is what keeps a provider's `ref` fields from
ever resolving to someone else's rows.

`label` is the name shown in error messages. A provider's config lives in a
namespaced table but is always talked about under the name they gave it.

`strict=True` (used by /v1) validates what the caller sends: only real, writable
fields; values of the right type; refs that point at a real parent row. The legacy
routes keep their original, looser behaviour.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from ..config.entities import EntityConfig, FieldConfig
from ..core.errors import ApiError
from .changefeed import log_change
from .generator import generate_row
from .scheduler import _existing_ids, _next_version


def maybe_inject_failure(cfg: EntityConfig) -> None:
    fi = cfg.failure_injection
    if fi.latency_ms:
        time.sleep(fi.latency_ms / 1000)
    if fi.fail_rate and random.random() < fi.fail_rate:
        raise ApiError(
            "injected_failure",
            f"synthetic failure injected for testing (fail_rate={fi.fail_rate})",
            500,
        )


def row_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


def not_found(label: str, item_id: str) -> ApiError:
    return ApiError("not_found", f"{label} '{item_id}' not found", 404)


# ------------------------------------------------------------------ reads ----
def list_rows(
    engine: Engine, cfg: EntityConfig, table: Any, limit: int, after: Optional[str] = None
) -> Dict[str, Any]:
    """Live rows ordered by primary key. `after` is a keyset cursor: pass the previous
    page's `next_after` to get the next page."""
    maybe_inject_failure(cfg)
    pk = table.c[cfg.primary_key_field]
    stmt = select(table).order_by(pk).limit(limit)
    if after is not None:
        stmt = stmt.where(pk > after)
    if cfg.soft_delete_field:
        stmt = stmt.where(table.c[cfg.soft_delete_field].is_(None))
    with engine.connect() as conn:
        rows = [row_to_dict(dict(r))
                for r in conn.execute(stmt).mappings().all()]
    return {"items": rows, "next_after": rows[-1][cfg.primary_key_field] if len(rows) == limit else None}


def get_row(engine: Engine, cfg: EntityConfig, table: Any, item_id: str, label: str) -> Dict[str, Any]:
    maybe_inject_failure(cfg)
    deleted_field = cfg.soft_delete_field
    with engine.connect() as conn:
        row = conn.execute(select(table).where(
            table.c[cfg.primary_key_field] == item_id)).mappings().first()
    if not row or (deleted_field and row[deleted_field] is not None):
        raise not_found(label, item_id)
    return row_to_dict(dict(row))


# ----------------------------------------------------------- validation ----
def _coerce(name: str, field: FieldConfig, value: Any) -> Any:
    def bad(expected: str) -> ApiError:
        return ApiError("invalid_value", f"'{name}' must be {expected}", 422)

    if value is None:
        if field.nullable:
            return None
        raise ApiError("invalid_value", f"'{name}' can't be null", 422)
    kind = field.type
    if kind in ("string", "uuid", "ref"):
        if not isinstance(value, str):
            raise bad("a string")
    elif kind == "enum":
        if value not in (field.values or []):
            raise bad(f"one of {field.values}")
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad("an integer")
    elif kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise bad("a number")
        value = float(value)
    elif kind == "bool":
        if not isinstance(value, bool):
            raise bad("true or false")
    elif kind == "timestamp":
        if not isinstance(value, str):
            raise bad("an ISO 8601 timestamp string")
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise bad("an ISO 8601 timestamp, e.g. 2026-01-31T12:00:00Z")
    return value


def check_payload(
    conn: Connection, cfg: EntityConfig, tables: Dict[str, Any], payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Validate a /v1 write body and return it with values converted for the database."""
    out: Dict[str, Any] = {}
    for name, value in payload.items():
        field = cfg.fields.get(name)
        if field is None:
            raise ApiError(
                "unknown_field", f"unknown field '{name}'. Fields: {list(cfg.fields)}", 422)
        if field.primary_key or field.auto:
            raise ApiError(
                "read_only_field", f"'{name}' is managed by the system and can't be set", 422)
        value = _coerce(name, field, value)
        if field.type == "ref" and value is not None:
            pk = next(iter(tables[field.ref_entity].primary_key.columns))
            if conn.execute(select(pk).where(pk == value)).first() is None:
                raise ApiError(
                    "invalid_reference", f"'{name}': no such row '{value}' in the referenced config", 422)
        out[name] = value
    return out


# ----------------------------------------------------------------- writes ----
def create_row(
    engine: Engine, cfg: EntityConfig, table: Any, tables: Dict[str, Any], configs: Dict[str, EntityConfig],
    payload: Optional[Dict[str, Any]], label: str, strict: bool = False,
) -> Dict[str, Any]:
    maybe_inject_failure(cfg)
    needed = {cfg.entity} | {
        f.ref_entity for f in cfg.fields.values() if f.type == "ref"}
    with engine.begin() as conn:
        values = check_payload(conn, cfg, tables, payload or {
        }) if strict else dict(payload or {})
        row = generate_row(cfg, _existing_ids(conn, tables, configs, needed))
        row.update(values)
        row["version"] = _next_version(conn, table)
        conn.execute(insert(table).values(**row))
        log_change(conn, cfg.entity,
                   row[cfg.primary_key_field], "insert", row["version"])
    return row_to_dict(row)


def update_row(
    engine: Engine, cfg: EntityConfig, table: Any, tables: Dict[str, Any], item_id: str,
    payload: Optional[Dict[str, Any]], label: str, strict: bool = False,
) -> Dict[str, Any]:
    maybe_inject_failure(cfg)
    pk_col = table.c[cfg.primary_key_field]
    with engine.begin() as conn:
        existing = conn.execute(select(table).where(
            pk_col == item_id)).mappings().first()
        if not existing or (strict and cfg.soft_delete_field and existing[cfg.soft_delete_field] is not None):
            raise not_found(label, item_id)
        values = check_payload(conn, cfg, tables, payload or {
        }) if strict else dict(payload or {})
        values["version"] = _next_version(conn, table)
        for fname, field in cfg.fields.items():
            if field.auto == "updated":
                values[fname] = datetime.now(timezone.utc)
        conn.execute(update(table).where(pk_col == item_id).values(**values))
        log_change(conn, cfg.entity, item_id, "update", values["version"])
        row = conn.execute(select(table).where(
            pk_col == item_id)).mappings().first()
    return row_to_dict(dict(row))


def delete_row(engine: Engine, cfg: EntityConfig, table: Any, item_id: str, label: str) -> Dict[str, Any]:
    maybe_inject_failure(cfg)
    deleted_field = cfg.soft_delete_field
    if not deleted_field:
        raise ApiError("not_supported",
                       f"{label} has no soft-delete field configured", 400)
    pk_col = table.c[cfg.primary_key_field]
    with engine.begin() as conn:
        if not conn.execute(select(pk_col).where(pk_col == item_id)).first():
            raise not_found(label, item_id)
        new_version = _next_version(conn, table)
        conn.execute(
            update(table).where(pk_col == item_id)
            .values(**{deleted_field: datetime.now(timezone.utc), "version": new_version})
        )
        log_change(conn, cfg.entity, item_id, "delete", new_version)
    return {"deleted": True, cfg.primary_key_field: item_id}
