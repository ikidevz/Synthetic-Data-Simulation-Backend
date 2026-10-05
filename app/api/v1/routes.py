"""The /v1 API: providers, the configs they publish, and the data generated from them.

  identity    GET  /v1/me
  superuser   POST/GET /v1/admin/providers, PATCH .../{id}, POST/DELETE .../{id}/keys
  configs     POST/GET /v1/configs, POST /v1/configs/validate,
              GET/PATCH/DELETE /v1/configs/{config_id}
  data        /v1/configs/{config_id}/data[/{row_id}], /changes, /export, /ddl, /metrics, /runs
  generate    POST /v1/configs/{config_id}/batch, /simulate
  bundle      GET  /v1/export?configs=a,b

Every route that takes a `config_id` goes through `catalog.resolve`, which is the single
place the access rules live: reads need ownership or a public config; writes need
ownership; a private config looks nonexistent (404) to everyone else; the superuser can
do anything. Routes only ever pass the entry's own scope to the engine, so a provider's
operations cannot reach another provider's tables.
"""
from __future__ import annotations

import random
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from ...config.provider import get_limits, normalize, parse_body, validate
from ...core.errors import ApiError
from ...db.engine import engine
from ...security.api_keys import Principal, current_principal, require_superuser
from ...services import entity_ops, registry
from ...services import metrics as metrics_module
from ...services.batch import DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE, MAX_ROWS_PER_ENTITY, run_batch, run_changes
from ...services.catalog import Entry, catalog, scrub
from ...services.changefeed import CursorAheadError, get_changes
from ...services.ddl import generate_ddl_labeled
from ...services.export import build_zip, iter_spool, open_entity_export, parse_since
from ...services.generator import MissingReferenceError

router = APIRouter(prefix="/v1", tags=["v1"])

Dialect = Literal["sqlite", "postgresql"]
Format = Literal["csv", "ndjson", "sql"]


# =========================================================== identity ====
@router.get("/me")
def me(principal: Principal = Depends(current_principal)):
    """Who this key is, and the quotas that apply to provider-created configs."""
    owned = None
    if principal.provider_id:
        owned = sum(1 for e in list(catalog.entries.values())
                    if e.source == "api" and e.provider_id == principal.provider_id)
    return {
        "role": principal.role,
        "id": principal.provider_id,
        "full_name": principal.full_name,
        "configs_owned": owned,
        "limits": get_limits().as_dict(),
    }


# ====================================================== superuser: providers ====
class ProviderCreate(BaseModel):
    full_name: str


class ProviderUpdate(BaseModel):
    full_name: Optional[str] = None
    is_active: Optional[bool] = None


class KeyCreate(BaseModel):
    label: Optional[str] = None


_SHOWN_ONCE = "Store this key now: it is shown once, and only a hash of it is kept."


@router.post("/admin/providers", status_code=201, dependencies=[Depends(require_superuser)])
def create_provider(body: ProviderCreate):
    """Create a provider (an `id` and a `full_name`) and its first API key."""
    provider = registry.create_provider(body.full_name)
    key = registry.issue_key(provider["id"], label="initial")
    return {**provider, "api_key": key.pop("api_key"), "key": key, "note": _SHOWN_ONCE}


@router.get("/admin/providers", dependencies=[Depends(require_superuser)])
def list_providers():
    return {"items": registry.list_providers()}


@router.patch("/admin/providers/{provider_id}", dependencies=[Depends(require_superuser)])
def update_provider(provider_id: str, body: ProviderUpdate):
    """Rename a provider, or deactivate / reactivate it (a deactivated provider's keys stop working)."""
    return registry.update_provider(provider_id, full_name=body.full_name, is_active=body.is_active)


@router.post("/admin/providers/{provider_id}/keys", status_code=201, dependencies=[Depends(require_superuser)])
def issue_provider_key(provider_id: str, body: Optional[KeyCreate] = None):
    """Issue an additional key (e.g. to rotate: issue a new one, then revoke the old)."""
    key = registry.issue_key(provider_id, label=body.label if body else None)
    return {"api_key": key.pop("api_key"), "key": key, "note": _SHOWN_ONCE}


@router.delete("/admin/providers/{provider_id}/keys/{key_id}", dependencies=[Depends(require_superuser)])
def revoke_provider_key(provider_id: str, key_id: str):
    return registry.revoke_key(provider_id, key_id)


# ================================================================ configs ====
def _owner_for(principal: Principal, provider_id: Optional[str]) -> str:
    """Whose config is being created. Providers act for themselves; the superuser names a
    provider (default: the built-in system provider)."""
    if principal.is_superuser:
        owner = provider_id or registry.SYSTEM_PROVIDER_ID
        if registry.get_provider(owner) is None:
            raise ApiError("not_found", f"provider '{owner}' not found", 404)
        return owner
    if provider_id and provider_id != principal.provider_id:
        raise ApiError(
            "forbidden", "you can only create configs for yourself", 403)
    return principal.provider_id  # type: ignore[return-value]


async def _body(request: Request) -> Dict[str, Any]:
    return parse_body(await request.body(), request.headers.get("content-type"), get_limits())


@router.post("/configs/validate")
async def validate_config(request: Request, provider_id: Optional[str] = Query(default=None),
                          principal: Principal = Depends(current_principal)):
    """Dry run of POST /v1/configs: checks everything, saves nothing, and shows the
    normalized config (with `version` added if you left it out)."""
    raw = await _body(request)
    owner = _owner_for(principal, provider_id)
    owned = [e for e in list(catalog.entries.values())
             if e.provider_id == owner]
    stored, flag = validate(
        raw, {e.name: e.stored for e in owned if e.source == "api"}, get_limits())
    return {
        "valid": True,
        "name_available": stored.entity not in {e.name for e in owned},
        "is_only_me": bool(flag),
        "config": normalize(stored),
    }


@router.post("/configs", status_code=201)
async def create_config(request: Request, provider_id: Optional[str] = Query(default=None),
                        principal: Principal = Depends(current_principal)):
    """Publish a config. Send JSON (`Content-Type: application/json`) or YAML — a file from
    `configs/` posts as-is. Add `is_only_me: true` to keep it private; by default other
    providers may read (never change) it. You are the owner, taken from your API key."""
    raw = await _body(request)
    owner = _owner_for(principal, provider_id)
    entry, seeded = await run_in_threadpool(catalog.create, owner, raw, get_limits())
    return {**catalog.describe(entry, principal, detail=True), "seeded_rows": seeded}


@router.get("/configs")
def list_configs(scope: Literal["mine", "shared", "all"] = "all", principal: Principal = Depends(current_principal)):
    """Configs you can read. `mine` = yours, `shared` = other providers' public ones."""
    owners = registry.provider_names()
    return {"items": [catalog.describe(e, principal, owners) for e in catalog.visible(principal, scope)]}


@router.get("/configs/{config_id}")
def get_config(config_id: str, principal: Principal = Depends(current_principal)):
    return catalog.describe(catalog.resolve(config_id, principal, "read"), principal, detail=True)


@router.patch("/configs/{config_id}")
async def patch_config(config_id: str, request: Request, confirm: bool = False,
                       principal: Principal = Depends(current_principal)):
    """Change `is_only_me`, `seed`, `update_schedule`, `failure_injection` (each merged into
    what's there) or `fields` (replaced; this deletes and re-seeds the data, so it needs
    `?confirm=true`, and isn't allowed while another config references this one)."""
    entry = catalog.resolve(config_id, principal, "write")
    patch = await _body(request)
    entry, seeded = await run_in_threadpool(catalog.update, entry, patch, confirm, get_limits())
    return {**catalog.describe(entry, principal, detail=True), "data_reset": seeded is not None, "seeded_rows": seeded}


@router.delete("/configs/{config_id}")
def delete_config(config_id: str, confirm: bool = False, principal: Principal = Depends(current_principal)):
    """Delete a config and ALL its data. Needs `?confirm=true`."""
    entry = catalog.resolve(config_id, principal, "write")
    if not confirm:
        raise ApiError("confirmation_required",
                       "deleting a config deletes all of its data; confirm it (confirm=true)", 400)
    catalog.delete(entry)
    return {"deleted": True, "id": entry.id, "name": entry.name}


# ============================================================ data reads ====
def _read(config_id: str, principal: Principal) -> Entry:
    return catalog.resolve(config_id, principal, "read")


@router.get("/configs/{config_id}/data")
def list_data(config_id: str, limit: int = Query(default=20, ge=1, le=500), after: Optional[str] = None,
              principal: Principal = Depends(current_principal)):
    """Live rows by primary key. Pass the response's `next_after` as `after` for the next page."""
    entry = _read(config_id, principal)
    return entity_ops.list_rows(engine, entry.cfg, entry.table, limit, after)


@router.get("/configs/{config_id}/data/{row_id}")
def get_data(config_id: str, row_id: str, principal: Principal = Depends(current_principal)):
    entry = _read(config_id, principal)
    return entity_ops.get_row(engine, entry.cfg, entry.table, row_id, entry.name)


@router.get("/configs/{config_id}/changes")
def get_changes_route(config_id: str, since: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500),
                      principal: Principal = Depends(current_principal)):
    """Inserts, updates and deletes after version `since` — the change feed."""
    entry = _read(config_id, principal)
    try:
        with engine.connect() as conn:
            return get_changes(conn, entry.table, entry.cfg, since=since, limit=limit)
    except CursorAheadError as exc:
        raise ApiError(
            "cursor_ahead_of_source",
            f"cursor {exc.since} is ahead of the latest version ({exc.latest}) for '{entry.name}'. "
            f"The dataset was probably reset; restart from since=0.",
            409,
        )


@router.get("/configs/{config_id}/export")
def export_config(
    config_id: str,
    fmt: Format = Query(default="csv", alias="format"),
    since: Optional[int] = Query(default=None, ge=0),
    include_deleted: bool = False,
    dialect: Dialect = "sqlite",
    principal: Principal = Depends(current_principal),
):
    """Snapshot of this config's data, or (with `since`) the changes after that version."""
    entry = _read(config_id, principal)
    chunks, headers, media_type = open_entity_export(
        engine, entry.table, entry.cfg, fmt=fmt, since=since,
        include_deleted=include_deleted, dialect=dialect, label=entry.name,
    )
    return StreamingResponse(chunks, media_type=media_type, headers=headers)


@router.get("/configs/{config_id}/ddl", response_class=PlainTextResponse)
def ddl_config(config_id: str, dialect: Dialect = "sqlite", include_parents: bool = True,
               principal: Principal = Depends(current_principal)):
    """CREATE TABLE / CREATE INDEX for this config, under the names you chose. With
    `include_parents` (the default) the tables it references are included so the script runs alone."""
    entry = _read(config_id, principal)
    configs, _ = catalog.scope(entry)
    return generate_ddl_labeled(configs, catalog.labels(entry), [entry.physical], dialect, include_parents)


@router.get("/configs/{config_id}/metrics")
def metrics_config(config_id: str, principal: Principal = Depends(current_principal)):
    entry = _read(config_id, principal)
    m = metrics_module.get_metrics(engine, {entry.physical: entry.table}, {
                                   entry.physical: entry.cfg})[entry.physical]
    return {"id": entry.id, "name": entry.name, **m, "max_rows": entry.cfg.max_rows}


@router.get("/configs/{config_id}/runs")
def runs_config(config_id: str, limit: int = Query(default=20, ge=1, le=200),
                principal: Principal = Depends(current_principal)):
    """Recent background-job runs for this config."""
    entry = _read(config_id, principal)
    runs = metrics_module.get_scheduler_runs(engine, entry.physical, limit)
    for run in runs:
        run["entity"] = entry.name
    return {"runs": runs}


# ===================================================== data writes (owner) ====
def _write_scope(entry: Entry):
    configs, tables = catalog.scope(entry)
    return tables, configs, catalog.labels(entry)


@router.post("/configs/{config_id}/data", status_code=201)
def create_data(config_id: str, payload: Optional[Dict[str, Any]] = Body(default=None),
                principal: Principal = Depends(current_principal)):
    """Add a row. Anything you leave out is generated; unknown or system-managed fields are rejected."""
    entry = catalog.resolve(config_id, principal, "write")
    tables, configs, labels = _write_scope(entry)
    try:
        return entity_ops.create_row(engine, entry.cfg, entry.table, tables, configs, payload, entry.name, strict=True)
    except MissingReferenceError as exc:
        raise ApiError("missing_parent_rows", scrub(str(exc), labels), 409)


@router.put("/configs/{config_id}/data/{row_id}")
def update_data(config_id: str, row_id: str, payload: Optional[Dict[str, Any]] = Body(default=None),
                principal: Principal = Depends(current_principal)):
    entry = catalog.resolve(config_id, principal, "write")
    tables, _, _ = _write_scope(entry)
    return entity_ops.update_row(engine, entry.cfg, entry.table, tables, row_id, payload, entry.name, strict=True)


@router.delete("/configs/{config_id}/data/{row_id}")
def delete_data(config_id: str, row_id: str, principal: Principal = Depends(current_principal)):
    entry = catalog.resolve(config_id, principal, "write")
    return entity_ops.delete_row(engine, entry.cfg, entry.table, row_id, entry.name)


# ================================================ generation (owner only) ====
class BatchRequest(BaseModel):
    mode: Literal["append", "replace", "reset"] = "append"
    count: Optional[int] = Field(default=None, ge=0, le=MAX_ROWS_PER_ENTITY)
    batch_size: int = Field(default=DEFAULT_BATCH_SIZE,
                            ge=1, le=MAX_BATCH_SIZE)
    seed: Optional[int] = None
    confirm: bool = False


class SimulateRequest(BaseModel):
    inserts: Optional[int] = Field(default=None, ge=0, le=MAX_ROWS_PER_ENTITY)
    updates: Optional[int] = Field(default=None, ge=0, le=MAX_ROWS_PER_ENTITY)
    deletes: Optional[int] = Field(default=None, ge=0, le=MAX_ROWS_PER_ENTITY)
    batch_size: int = Field(default=DEFAULT_BATCH_SIZE,
                            ge=1, le=MAX_BATCH_SIZE)
    seed: Optional[int] = None


def _total_rows(entry: Entry) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(entry.table)).scalar() or 0


def _relabel(result: Dict[str, Any], labels: Dict[str, str]) -> Dict[str, Any]:
    for item in result.get("entities", []):
        item["entity"] = labels.get(item["entity"], item["entity"])
    if "auto_included" in result:
        result["auto_included"] = [labels.get(
            n, n) for n in result["auto_included"]]
    if "notes" in result:
        result["notes"] = [scrub(n, labels) for n in result["notes"]]
    return result


def _over_quota(cap: int, resulting: int) -> ApiError:
    return ApiError(
        "quota_exceeded",
        f"that would leave {resulting} rows; a config may hold at most {cap}",
        422,
    )


@router.post("/configs/{config_id}/batch")
def batch_config(config_id: str, req: BatchRequest, principal: Principal = Depends(current_principal)):
    """Bulk-generate rows (`append`) or refresh the data (`replace` / `reset`, which delete
    data and need `confirm: true`). `count` defaults to the config's seed.initial_count.
    Configs that reference this one are refreshed along with it for replace / reset."""
    entry = catalog.resolve(config_id, principal, "write")
    configs, tables = catalog.scope(entry)
    labels = catalog.labels(entry)
    cap = entry.cfg.max_rows
    if cap is not None:
        wanted = req.count if req.count is not None else entry.cfg.seed.initial_count
        resulting = _total_rows(
            entry) + wanted if req.mode == "append" else wanted
        if resulting > cap:
            raise _over_quota(cap, resulting)
    try:
        result = run_batch(
            engine, tables, configs, mode=req.mode, entities=[entry.physical],
            counts={entry.physical: req.count} if req.count is not None else None,
            batch_size=req.batch_size, seed=req.seed, confirm=req.confirm,
        )
    except ApiError as exc:
        raise ApiError(exc.code, scrub(exc.message, labels), exc.status)
    return _relabel(result, labels)


@router.post("/configs/{config_id}/simulate")
def simulate_config(config_id: str, req: SimulateRequest, principal: Principal = Depends(current_principal)):
    """Apply one batch of inserts, updates and soft-deletes on demand, all recorded in the
    change feed. Anything you omit defaults to one scheduler tick for this config."""
    entry = catalog.resolve(config_id, principal, "write")
    configs, tables = catalog.scope(entry)
    labels = catalog.labels(entry)
    inserts = req.inserts
    cap = entry.cfg.max_rows
    if cap is not None:
        headroom = max(0, cap - _total_rows(entry))
        if inserts is None:
            low, high = entry.cfg.update_schedule.new_records
            inserts = min(random.randint(low, high), headroom)
        elif inserts > headroom:
            raise _over_quota(cap, _total_rows(entry) + inserts)
    try:
        result = run_changes(
            engine, tables, configs, entities=[
                entry.physical], inserts=inserts,
            updates=req.updates, deletes=req.deletes, batch_size=req.batch_size, seed=req.seed,
        )
    except ApiError as exc:
        raise ApiError(exc.code, scrub(exc.message, labels), exc.status)
    return _relabel(result, labels)


# ================================================================ bundle ====
@router.get("/export")
def export_bundle(
    configs: List[str] = Query(...,
                               description="config ids, repeated or comma-separated"),
    fmt: Format = Query(default="csv", alias="format"),
    dialect: Dialect = "sqlite",
    include_deleted: bool = False,
    since: List[str] = Query(
        default=[], description="<config_id>=<version>: that config's changes instead of a snapshot"),
    principal: Principal = Depends(current_principal),
):
    """A zip with schema.sql, one data file per config (parents first) and a manifest.json.
    All configs must have the same owner, so table names and relationships stay consistent."""
    ids = list(dict.fromkeys(i.strip()
               for raw in configs for i in raw.split(",") if i.strip()))
    if not ids:
        raise ApiError("invalid_request",
                       "give at least one config id in `configs`", 422)
    entries = [catalog.resolve(i, principal, "read") for i in ids]
    first = entries[0]
    if any(catalog.scope_key(e) != catalog.scope_key(first) for e in entries):
        raise ApiError(
            "mixed_owners", "a bundle holds configs from one owner, so names and relationships stay consistent", 422)
    cfgs, tbls = catalog.scope(first)
    labels = catalog.labels(first)

    by_id = {e.id: e for e in entries}
    since_by_table: Dict[str, int] = {}
    for config_id, version in parse_since(since).items():
        if config_id not in by_id:
            raise ApiError(
                "since_not_targeted", f"since given for '{config_id}', which is not in `configs`", 422)
        since_by_table[by_id[config_id].physical] = version

    try:
        spool = build_zip(
            engine, tbls, cfgs, fmt=fmt, dialect=dialect, entities=[
                e.physical for e in entries],
            include_deleted=include_deleted, since=since_by_table, labels=labels,
            handoff=(
                "Load each snapshot in load_order, then catch up with "
                "GET /v1/configs/<config_id>/export?since=<snapshot_cursor> "
                "(or /changes?since=<snapshot_cursor>)."
            ),
            file_extra={e.physical: {"config_id": e.id} for e in entries},
        )
    except ApiError as exc:
        raise ApiError(exc.code, scrub(exc.message, labels), exc.status)
    return StreamingResponse(
        iter_spool(spool), media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="synthetic-export.zip"'},
    )
