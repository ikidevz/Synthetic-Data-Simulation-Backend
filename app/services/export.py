"""Data export: snapshots and change batches as CSV / NDJSON / SQL.

Two kinds of file, both streamed in chunks (keyset-paginated, so memory stays
flat however big the table is):

  snapshot  the entity's current rows (soft-deleted rows excluded unless
            include_deleted). Formats: csv, ndjson, sql (INSERT statements).
  changes   every insert / update / delete after a cursor, one row per change,
            with an `op` column and the change's version. Formats: csv, ndjson.
            Together with a snapshot this is the classic load-then-catch-up
            handoff: load the snapshot, then apply changes from its cursor.

The cursor is captured BEFORE any rows are read, so a snapshot always contains
every change up to its cursor (it may also contain a few newer ones — applying
changes from the cursor is idempotent, so that is safe).

A "bundle" is schema.sql + one file per entity (parents first) + manifest.json
with row counts and cursors: everything needed to load the dataset elsewhere.
"""
from __future__ import annotations

import csv
import io
import json
import math
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from sqlalchemy import Table, select
from sqlalchemy.engine import Connection, Engine

from ..config.entities import EntityConfig
from ..db import models
from .batch import BatchError, dependency_order
from .changefeed import latest_version
from .ddl import DIALECTS, generate_ddl, generate_ddl_labeled

FORMATS = ("csv", "ndjson", "sql")
CHANGE_FORMATS = ("csv", "ndjson")
EXTENSIONS = {"csv": "csv", "ndjson": "ndjson", "sql": "sql"}
MEDIA_TYPES = {"csv": "text/csv",
               "ndjson": "application/x-ndjson", "sql": "text/plain"}
CHUNK = 5000
INSERT_BATCH = 500  # rows per INSERT statement in sql exports


# ------------------------------------------------------------ reading ----
def iter_snapshot_rows(
    conn: Connection, table: Table, cfg: EntityConfig, include_deleted: bool, chunk: int = CHUNK
) -> Iterator[Dict[str, Any]]:
    pk_field = cfg.primary_key_field
    pk_col = table.c[pk_field]
    deleted = cfg.soft_delete_field
    last = None
    while True:
        stmt = select(table).order_by(pk_col).limit(chunk)
        if last is not None:
            stmt = stmt.where(pk_col > last)
        if deleted and not include_deleted:
            stmt = stmt.where(table.c[deleted].is_(None))
        rows = conn.execute(stmt).mappings().all()
        if not rows:
            return
        for row in rows:
            yield dict(row)
        last = rows[-1][pk_field]


def iter_change_rows(
    conn: Connection, table: Table, cfg: EntityConfig, since: int, upto: int, chunk: int = CHUNK
) -> Iterator[Dict[str, Any]]:
    """Changes with since < version <= upto, oldest first. Each row carries the
    entity's CURRENT values (as the /changes feed does) plus `op` and the change's
    own version. A wiped row has no current values, so only its key is filled."""
    log = models.change_log
    pk_field = cfg.primary_key_field
    pk_col = table.c[pk_field]
    cursor = since
    while True:
        logs = (
            conn.execute(
                select(log)
                .where(log.c.entity == cfg.entity, log.c.version > cursor, log.c.version <= upto)
                .order_by(log.c.version)
                .limit(chunk)
            )
            .mappings()
            .all()
        )
        if not logs:
            return
        current: Dict[str, Dict[str, Any]] = {}
        keys = list({entry["pk_value"] for entry in logs})
        for i in range(0, len(keys), 500):
            for row in conn.execute(select(table).where(pk_col.in_(keys[i: i + 500]))).mappings():
                current[str(row[pk_field])] = dict(row)
        for entry in logs:
            row = dict(current.get(entry["pk_value"]) or {
                       pk_field: entry["pk_value"]})
            row["version"] = entry["version"]
            yield {"op": entry["op"], **row}
        cursor = logs[-1]["version"]


# ---------------------------------------------------------- rendering ----
def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""  # CSV can't tell NULL from an empty string; empty means NULL here
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def sql_literal(value: Any, dialect: str) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE") if dialect == "postgresql" else ("1" if value else "0")
    if isinstance(value, (int, float)):
        return repr(value) if not isinstance(value, float) or math.isfinite(value) else "NULL"
    if isinstance(value, datetime):
        return "'" + value.isoformat(sep=" ") + "'"
    # standard SQL: double the quote
    return "'" + str(value).replace("'", "''") + "'"


def _render_csv(rows: Iterable[Dict[str, Any]], columns: List[str], flush_every: int) -> Iterator[str]:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for n, row in enumerate(rows, 1):
        writer.writerow([_csv_cell(row.get(c)) for c in columns])
        if n % flush_every == 0:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
    yield buf.getvalue()


def _render_ndjson(rows: Iterable[Dict[str, Any]], columns: List[str], flush_every: int) -> Iterator[str]:
    lines: List[str] = []
    for row in rows:
        record = {c: (row.get(c).isoformat() if isinstance(
            row.get(c), datetime) else row.get(c)) for c in columns}
        lines.append(json.dumps(record, default=str))
        if len(lines) >= flush_every:
            yield "\n".join(lines) + "\n"
            lines = []
    if lines:
        yield "\n".join(lines) + "\n"


def _render_sql(rows: Iterable[Dict[str, Any]], columns: List[str], table_name: str, dialect: str) -> Iterator[str]:
    quote = DIALECTS[dialect]().identifier_preparer.quote
    head = f"INSERT INTO {quote(table_name)} ({', '.join(quote(c) for c in columns)}) VALUES\n"
    yield f"-- {table_name}: INSERT statements ({dialect})\n"
    group: List[str] = []
    for row in rows:
        group.append("(" + ", ".join(sql_literal(row.get(c), dialect)
                     for c in columns) + ")")
        if len(group) >= INSERT_BATCH:
            yield head + ",\n".join(group) + ";\n"
            group = []
    if group:
        yield head + ",\n".join(group) + ";\n"


def render(
    rows: Iterable[Dict[str, Any]], columns: List[str], fmt: str, *,
    table_name: str, dialect: str = "sqlite", flush_every: int = 1000,
) -> Iterator[str]:
    if fmt == "csv":
        return _render_csv(rows, columns, flush_every)
    if fmt == "ndjson":
        return _render_ndjson(rows, columns, flush_every)
    if fmt == "sql":
        return _render_sql(rows, columns, table_name, dialect)
    raise BatchError("invalid_format",
                     f"format must be one of {list(FORMATS)}", 422)


# ------------------------------------------------------ single entity ----
def _check(fmt: str, dialect: str, changes: bool) -> None:
    if fmt not in FORMATS:
        raise BatchError("invalid_format",
                         f"format must be one of {list(FORMATS)}", 422)
    if dialect not in DIALECTS:
        raise BatchError("invalid_dialect",
                         f"dialect must be one of {sorted(DIALECTS)}", 422)
    if changes and fmt not in CHANGE_FORMATS:
        raise BatchError("unsupported_format",
                         "change exports support csv and ndjson only", 422)


def _stale(name: str, since: int, upto: int) -> BatchError:
    return BatchError(
        "cursor_ahead_of_source",
        f"cursor {since} is ahead of the latest version ({upto}) for '{name}'. "
        f"The dataset was probably reset; restart from since=0.",
        409,
    )


def open_entity_export(
    engine: Engine, table: Table, cfg: EntityConfig, *,
    fmt: str = "csv", since: Optional[int] = None, include_deleted: bool = False, dialect: str = "sqlite",
    label: Optional[str] = None,
) -> Tuple[Iterator[str], Dict[str, str], str]:
    """Returns (chunks, headers, media_type). Validation errors raise BatchError
    before anything is streamed. `label` is the name shown in the file name, the SQL
    table name and error messages (default: the entity name) — a provider's config
    is stored under a namespaced table but exported under the name they gave it."""
    shown = label or cfg.entity
    changes = since is not None
    _check(fmt, dialect, changes)
    with engine.connect() as conn:
        upto = latest_version(conn, cfg.entity)  # captured BEFORE reading rows
    if changes and since > upto:
        raise _stale(shown, since, upto)

    columns = (["op"] if changes else []) + list(cfg.fields)

    def chunks() -> Iterator[str]:
        with engine.connect() as conn:
            rows = (
                iter_change_rows(conn, table, cfg, since, upto)
                if changes
                else iter_snapshot_rows(conn, table, cfg, include_deleted)
            )
            yield from render(rows, columns, fmt, table_name=shown, dialect=dialect)

    name = f"{shown}{'.changes' if changes else ''}.{EXTENSIONS[fmt]}"
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    if changes:
        headers.update({"X-From-Cursor": str(since), "X-To-Cursor": str(upto)})
    else:
        headers["X-Snapshot-Cursor"] = str(upto)
    return chunks(), headers, MEDIA_TYPES[fmt]


# ---------------------------------------------------------------- bundle ----
def parse_since(pairs: Optional[Iterable[str]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for pair in pairs or []:
        name, sep, value = pair.partition("=")
        if not sep or not name or not value.isdigit():
            raise BatchError(
                "invalid_since", f"since entries must look like entity=N, got '{pair}'", 422)
        out[name] = int(value)
    return out


def _counted(rows: Iterable[Dict[str, Any]], counter: Dict[str, int]) -> Iterator[Dict[str, Any]]:
    for row in rows:
        counter["n"] += 1
        yield row


def write_bundle(
    engine: Engine,
    tables: Dict[str, Table],
    configs: Dict[str, EntityConfig],
    add_file: Callable[[str, Iterable[str]], None],
    *,
    fmt: str = "csv",
    dialect: str = "sqlite",
    entities: Optional[List[str]] = None,
    include_deleted: bool = False,
    since: Optional[Dict[str, int]] = None,
    labels: Optional[Dict[str, str]] = None,
    handoff: Optional[str] = None,
    file_extra: Optional[Dict[str, Dict[str, Any]]] = None,
    schema: Optional[str] = None,
    manifest_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Stream schema.sql + one file per entity + manifest.json through `add_file`
    (which is handed a file name and an iterable of text chunks and must consume it).
    Entities named in `since` get a change file instead of a snapshot.

    `labels` maps an entity's key to the name shown in file names, SQL and the
    manifest (a provider's configs are keyed by namespaced table names but exported
    under their own). `handoff` replaces the manifest's catch-up hint, and
    `file_extra` merges extra keys into an entity's manifest entry. `schema` is the project
    name (see `generate_ddl_labeled`) and `manifest_extra` merges extra top-level keys into
    the manifest."""
    since = since or {}
    labels = labels or {}

    def shown(n: str) -> str:
        return labels.get(n, n)

    _check(fmt, dialect, changes=bool(since))
    for name in list(entities or []) + list(since):
        if name not in configs:
            raise BatchError(
                "unknown_entity", f"unknown entity '{name}'. Known: {sorted(configs)}", 422)
    targets = dependency_order(configs, dict.fromkeys(
        entities) if entities else configs)
    stray = set(since) - set(targets)
    if stray:
        raise BatchError(
            "since_not_targeted", f"since given for entities not being exported: {sorted(stray)}", 422)

    snapshots = [n for n in targets if n not in since]
    schema_name = None
    if snapshots:
        schema_name = "schema.sql"
        if labels:
            ddl_text = generate_ddl_labeled(
                configs, labels, snapshots, dialect=dialect, schema=schema)
        else:
            ddl_text = generate_ddl(
                {n: tables[n] for n in snapshots}, dialect=dialect)
        add_file(schema_name, [ddl_text])

    files = []
    for name in targets:
        cfg, table = configs[name], tables[name]
        is_changes = name in since
        with engine.connect() as conn:
            upto = latest_version(conn, name)  # captured BEFORE reading rows
            if is_changes and since[name] > upto:
                raise _stale(shown(name), since[name], upto)
            counter = {"n": 0}
            rows = (
                iter_change_rows(conn, table, cfg, since[name], upto)
                if is_changes
                else iter_snapshot_rows(conn, table, cfg, include_deleted)
            )
            columns = (["op"] if is_changes else []) + list(cfg.fields)
            file_name = f"{shown(name)}{'.changes' if is_changes else ''}.{EXTENSIONS[fmt]}"
            add_file(file_name, render(_counted(rows, counter),
                     columns, fmt, table_name=shown(name), dialect=dialect))
        entry: Dict[str, Any] = {"entity": shown(
            name), "file": file_name, "rows": counter["n"]}
        entry.update((file_extra or {}).get(name, {}))
        if is_changes:
            entry.update(kind="changes",
                         from_cursor=since[name], to_cursor=upto)
        else:
            entry.update(kind="snapshot", snapshot_cursor=upto)
        files.append(entry)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "format": fmt,
        "dialect": dialect,
        "schema": schema_name,
        "load_order": [shown(n) for n in targets],
        "files": files,
        "handoff": handoff or (
            "Load each snapshot in load_order, then catch up with "
            "/<entity>/export?since=<snapshot_cursor> or /<entity>/changes?since=<snapshot_cursor>."
        ),
    }
    manifest.update(manifest_extra or {})
    add_file("manifest.json", [json.dumps(manifest, indent=2) + "\n"])
    return manifest


def build_zip(
    engine: Engine, tables: Dict[str, Table], configs: Dict[str, EntityConfig], **bundle_kwargs: Any
) -> Any:
    """Write a bundle into a spooled temp file (memory up to 64 MB, then disk) and return it
    rewound. Built BEFORE anything is sent, so a bad request fails as a clean error rather
    than a half-streamed zip. Pass the result to `iter_spool`."""
    spool = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
            def add_file(name: str, chunks: Iterable[str]) -> None:
                with zf.open(name, "w", force_zip64=True) as fh:
                    for chunk in chunks:
                        fh.write(chunk.encode("utf-8"))

            write_bundle(engine, tables, configs, add_file, **bundle_kwargs)
    except BaseException:
        spool.close()
        raise
    spool.seek(0)
    return spool


def iter_spool(spool: Any, size: int = 65536) -> Iterator[bytes]:
    try:
        while True:
            data = spool.read(size)
            if not data:
                break
            yield data
    finally:
        spool.close()
