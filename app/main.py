"""Synthetic Data Simulation Backend — FastAPI entrypoint.

On startup: loads every entity config from CONFIG_DIR, builds the
matching database tables, seeds initial data (in dependency order so
'ref' fields always resolve), starts one background scheduler job per
entity, and registers CRUD + change-feed routes for every entity —
all driven entirely by the YAML configs, with no per-entity code.

Two surfaces share one engine:
  * the legacy routes (/orders, /admin/batch, /metrics, ...) serve the built-in YAML
    entities and need a SUPERUSER key;
  * /v1 (app/api/v1/routes.py) lets providers publish their own configs and read or
    manage the data generated from them, with the access rules in
    app/services/catalog.py.

This module is the entrypoint only: it builds the app, owns the process-wide
CONFIGS / TABLES state, seeds it, and registers the routes. Everything it calls
lives one layer down.
"""
from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Body, Depends
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, insert, func

from .api.error_handlers import batch_http_error
from .api.error_handlers import register as register_error_handlers
from .api.v1.routes import router as v1_router
from .config.entities import EntityConfig, get_config_dir, load_entity_configs
from .config.provider import get_limits
from .db import models
from .db.engine import engine
from .security.api_keys import check_configuration, docs_enabled, require_superuser, verify_api_key
from .services import entity_ops
from .services import metrics as metrics_module
from .services.batch import DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE, MAX_ROWS_PER_ENTITY, BatchError, run_batch, run_changes
from .services.catalog import catalog
from .services.changefeed import CursorAheadError, get_changes, log_change
from .services.ddl import generate_ddl
from .services.export import build_zip, iter_spool, open_entity_export, parse_since
from .services.generator import generate_row
from .services.scheduler import start_scheduler, _next_version, _existing_ids

CONFIG_DIR = get_config_dir()


@asynccontextmanager
async def lifespan(app: FastAPI):
    on_startup()
    yield
    on_shutdown()


# Auth is a GLOBAL dependency: every route needs the API key unless its path is in
# api_keys.PUBLIC_PATHS. Routes added later are protected automatically.
_docs = {} if docs_enabled() else {"docs_url": None,
                                   "redoc_url": None, "openapi_url": None}
app = FastAPI(
    title="Synthetic Data Simulation Backend",
    lifespan=lifespan,
    dependencies=[Depends(verify_api_key)],
    **_docs,
)

# One {"error": {code, message}} envelope for every failure mode: HTTPException,
# request validation errors, and injected failures all come back shaped the same.
register_error_handlers(app)
app.include_router(v1_router)

CONFIGS: Dict[str, EntityConfig] = {}
TABLES: Dict[str, Any] = {}
_scheduler = None
_routes_registered = False


# The superuser-only gate for every legacy route (the built-in entities and the global
# admin tools). Provider keys reach /v1 only.
SUPERUSER = [Depends(require_superuser)]


# --------------------------------------------------------------------------
# Seeding — in dependency order, so 'ref' fields always resolve to a real row
# --------------------------------------------------------------------------
def _seed_entity(cfg: EntityConfig, table) -> None:
    with engine.begin() as conn:
        count = conn.execute(select(func.count()).select_from(table)).scalar()
        if count and count > 0:
            return
        existing_ids = _existing_ids(conn, TABLES, CONFIGS)
        for _ in range(cfg.seed.initial_count):
            row = generate_row(cfg, existing_ids)
            row["version"] = _next_version(conn, table)
            conn.execute(insert(table).values(**row))
            log_change(conn, cfg.entity,
                       row[cfg.primary_key_field], "insert", row["version"])
            existing_ids.setdefault(cfg.entity, []).append(
                row[cfg.primary_key_field])


def _seed_all() -> None:
    def deps(cfg: EntityConfig):
        return {f.ref_entity for f in cfg.fields.values() if f.type == "ref"}

    remaining = dict(CONFIGS)
    seeded: set = set()
    while remaining:
        progressed = False
        for name, cfg in list(remaining.items()):
            if deps(cfg) <= seeded:
                _seed_entity(cfg, TABLES[name])
                seeded.add(name)
                del remaining[name]
                progressed = True
        if not progressed:
            raise RuntimeError(
                f"Circular or unresolved 'ref' dependency among: {list(remaining)}")


# --------------------------------------------------------------------------
# Per-entity CRUD + change-feed routes, generated from config — no
# per-entity route functions are hand-written anywhere in this file.
# --------------------------------------------------------------------------
def _register_entity_routes() -> None:
    global _routes_registered
    if _routes_registered:
        return

    for name, cfg in CONFIGS.items():
        table = TABLES[name]

        def make_list(name=name, table=table, cfg=cfg):
            def _list(limit: int = Query(default=20, le=500)):
                return entity_ops.list_rows(engine, cfg, table, limit)
            return _list

        def make_changes(name=name, table=table, cfg=cfg):
            def _changes(since: int = Query(default=0), limit: int = Query(default=100, le=500)):
                try:
                    with engine.connect() as conn:
                        return get_changes(conn, table, cfg, since=since, limit=limit)
                except CursorAheadError as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": {
                                "code": "cursor_ahead_of_source",
                                "message": (
                                    f"cursor {exc.since} is ahead of the latest version "
                                    f"({exc.latest}) for '{name}'. The dataset was probably reset; "
                                    f"restart from since=0."
                                ),
                            }
                        },
                    )
            return _changes

        def make_export(name=name, table=table, cfg=cfg):
            def _export(
                fmt: Literal["csv", "ndjson", "sql"] = Query(
                    default="csv", alias="format"),
                since: Optional[int] = Query(default=None, ge=0),
                include_deleted: bool = False,
                dialect: Literal["sqlite", "postgresql"] = "sqlite",
            ):
                """Snapshot of this entity, or (with `since`) the changes after that version."""
                try:
                    chunks, headers, media_type = open_entity_export(
                        engine, table, cfg, fmt=fmt, since=since,
                        include_deleted=include_deleted, dialect=dialect,
                    )
                except BatchError as exc:
                    raise batch_http_error(exc)
                return StreamingResponse(chunks, media_type=media_type, headers=headers)
            return _export

        def make_get(name=name, table=table, cfg=cfg):
            def _get(item_id: str):
                return entity_ops.get_row(engine, cfg, table, item_id, name)
            return _get

        def make_create(name=name, table=table, cfg=cfg):
            def _create(payload: Dict[str, Any] = Body(default={})):
                return entity_ops.create_row(engine, cfg, table, TABLES, CONFIGS, payload, name)
            return _create

        def make_update(name=name, table=table, cfg=cfg):
            def _update(item_id: str, payload: Dict[str, Any] = Body(default={})):
                return entity_ops.update_row(engine, cfg, table, TABLES, item_id, payload, name)
            return _update

        def make_delete(name=name, table=table, cfg=cfg):
            def _delete(item_id: str):
                return entity_ops.delete_row(engine, cfg, table, item_id, name)
            return _delete

        # Order matters: '/changes' must be registered before the
        # '/{item_id}' GET route, or Starlette will match "changes" as an
        # item_id path parameter first.
        app.add_api_route(f"/{name}", make_list(),
                          methods=["GET"], dependencies=SUPERUSER)
        app.add_api_route(f"/{name}/changes", make_changes(),
                          methods=["GET"], dependencies=SUPERUSER)
        app.add_api_route(f"/{name}/export", make_export(),
                          methods=["GET"], dependencies=SUPERUSER)
        app.add_api_route(f"/{name}/{{item_id}}", make_get(),
                          methods=["GET"], dependencies=SUPERUSER)
        app.add_api_route(f"/{name}", make_create(),
                          methods=["POST"], status_code=201, dependencies=SUPERUSER)
        app.add_api_route(f"/{name}/{{item_id}}", make_update(),
                          methods=["PUT"], dependencies=SUPERUSER)
        app.add_api_route(f"/{name}/{{item_id}}", make_delete(),
                          methods=["DELETE"], dependencies=SUPERUSER)

    _routes_registered = True


# --------------------------------------------------------------------------
# System-level routes (not per-entity)
# --------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/entities", dependencies=SUPERUSER)
def list_entities():
    return {
        name: {
            "fields": list(cfg.fields.keys()),
            "cadence": cfg.update_schedule.cadence,
            "row_count_seed": cfg.seed.initial_count,
        }
        for name, cfg in CONFIGS.items()
    }


@app.get("/metrics", dependencies=SUPERUSER)
def get_metrics_route():
    return metrics_module.get_metrics(engine, TABLES, CONFIGS)


@app.get("/scheduler/runs", dependencies=SUPERUSER)
def scheduler_runs_route(entity: str, limit: int = Query(default=20, le=200)):
    if entity not in CONFIGS:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "not_found",
                              "message": f"unknown entity '{entity}'"}},
        )
    return {"runs": metrics_module.get_scheduler_runs(engine, entity, limit)}


# --------------------------------------------------------------------------
# DDL export + batch generation / refresh
# --------------------------------------------------------------------------
@app.get("/ddl", response_class=PlainTextResponse, dependencies=SUPERUSER)
def ddl_route(dialect: Literal["sqlite", "postgresql"] = "sqlite", include_system: bool = False):
    """CREATE TABLE / CREATE INDEX statements for every entity, generated from the configs."""
    return generate_ddl(TABLES, dialect=dialect, include_system=include_system)


class BatchRequest(BaseModel):
    mode: Literal["append", "replace", "reset"] = "append"
    entities: Optional[List[str]] = None
    counts: Optional[Dict[str, Annotated[int, Field(
        ge=0, le=MAX_ROWS_PER_ENTITY)]]] = None
    count: Optional[int] = Field(default=None, ge=0, le=MAX_ROWS_PER_ENTITY)
    batch_size: int = Field(default=DEFAULT_BATCH_SIZE,
                            ge=1, le=MAX_BATCH_SIZE)
    seed: Optional[int] = None
    confirm: bool = False


@app.post("/admin/batch", dependencies=SUPERUSER)
def batch_route(req: BatchRequest):
    """Bulk-generate rows (append) or refresh the dataset (replace / reset).

    replace and reset delete existing data and require confirm=true.
    """
    try:
        return run_batch(
            engine, TABLES, CONFIGS,
            mode=req.mode, entities=req.entities, counts=req.counts, count=req.count,
            batch_size=req.batch_size, seed=req.seed, confirm=req.confirm,
        )
    except BatchError as exc:
        raise batch_http_error(exc)


class ChangesRequest(BaseModel):
    entities: Optional[List[str]] = None
    inserts: Optional[int] = Field(default=None, ge=0, le=MAX_ROWS_PER_ENTITY)
    updates: Optional[int] = Field(default=None, ge=0, le=MAX_ROWS_PER_ENTITY)
    deletes: Optional[int] = Field(default=None, ge=0, le=MAX_ROWS_PER_ENTITY)
    batch_size: int = Field(default=DEFAULT_BATCH_SIZE,
                            ge=1, le=MAX_BATCH_SIZE)
    seed: Optional[int] = None


@app.post("/admin/changes", dependencies=SUPERUSER)
def changes_route(req: ChangesRequest):
    """Apply a batch of inserts, updates and soft-deletes, all recorded in the change feed.

    Any count you omit defaults to one scheduler tick for that entity.
    """
    try:
        return run_changes(
            engine, TABLES, CONFIGS,
            entities=req.entities, inserts=req.inserts, updates=req.updates,
            deletes=req.deletes, batch_size=req.batch_size, seed=req.seed,
        )
    except BatchError as exc:
        raise batch_http_error(exc)


@app.get("/export", dependencies=SUPERUSER)
def export_route(
    fmt: Literal["csv", "ndjson", "sql"] = Query(
        default="csv", alias="format"),
    dialect: Literal["sqlite", "postgresql"] = "sqlite",
    entities: List[str] = Query(default=[]),
    since: List[str] = Query(default=[]),
    include_deleted: bool = False,
):
    """A zip with schema.sql, one data file per entity (parents first) and a manifest.json.

    `since=orders=120` swaps that entity's snapshot for its changes after version 120.
    """
    try:
        spool = build_zip(
            engine, TABLES, CONFIGS, fmt=fmt, dialect=dialect,
            entities=entities or None, include_deleted=include_deleted,
            since=parse_since(since),
        )
    except BatchError as exc:
        raise batch_http_error(exc)
    return StreamingResponse(
        iter_spool(spool), media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="synthetic-export.zip"'},
    )


# --------------------------------------------------------------------------
# Startup / shutdown
# --------------------------------------------------------------------------
def on_startup():
    global CONFIGS, TABLES, _scheduler
    check_configuration()
    get_limits()                         # fail fast on a malformed MAX_* quota variable
    CONFIGS = load_entity_configs(CONFIG_DIR)
    TABLES = models.build_tables(CONFIGS)
    # built-in entities + the provider registry
    models.metadata.create_all(engine)
    # register YAML examples; rebuild provider configs
    catalog.load(CONFIGS, TABLES)
    # ...and any provider table that doesn't exist yet
    models.metadata.create_all(engine)
    _seed_all()
    _register_entity_routes()
    _scheduler = start_scheduler(engine, TABLES, CONFIGS)
    # one background job per provider config
    catalog.start_jobs(_scheduler)


def on_shutdown():
    global _scheduler
    catalog.stop()
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
