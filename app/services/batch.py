"""Batch synthetic generation: bulk-create rows, or refresh the whole dataset.

Three modes:

  append   add N more rows per entity. Existing rows and every consumer's
           change-feed cursor stay valid.
  replace  wipe the entities and regenerate them. Wiped rows are written to
           the change log as 'delete' events and versions keep climbing, so a
           consumer polling /changes sees the refresh as ordinary deletes +
           inserts — no re-baselining needed.
  reset    truncate everything (rows, change log, scheduler history) and start
           from version 1. A clean slate for demos; consumers must re-baseline
           from since=0 (a stale cursor gets a 409 from /changes).

Replace and reset also refresh every entity that references the ones you
name (their rows would otherwise point at wiped parents). The response lists
these under `auto_included`.

The whole operation is ONE transaction: it either fully applies or leaves the
database untouched. Rows are inserted in chunks of `batch_size` to keep
memory bounded. Pass a `seed` and the same request regenerates the same ids,
values and relationships (timestamps and version numbers excepted).
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy.exc import IntegrityError
from sqlalchemy import Table, bindparam, delete, func, insert, select, update
from sqlalchemy.engine import Connection, Engine

from ..config.entities import EntityConfig
from ..core.errors import ApiError
from ..db import models
from .generator import MissingReferenceError, generate_row, generate_update, mutable_fields
from .scheduler import WRITE_LOCK, _next_version

logger = logging.getLogger("batch")

VALID_MODES = ("append", "replace", "reset")
MAX_ROWS_PER_ENTITY = 1_000_000
MAX_BATCH_SIZE = 10_000
DEFAULT_BATCH_SIZE = 1_000


class BatchError(ApiError):
    """A request problem, with an error code and HTTP-style status."""


def _deps(cfg: EntityConfig) -> Set[str]:
    return {f.ref_entity for f in cfg.fields.values() if f.type == "ref"}


def dependency_order(configs: Dict[str, EntityConfig], names: Iterable[str]) -> List[str]:
    """`names` sorted parents-before-children (deterministic)."""
    wanted = set(names)
    ordered: List[str] = []
    placed: Set[str] = set()
    while len(ordered) < len(wanted):
        progressed = False
        for name in sorted(wanted - placed):
            if (_deps(configs[name]) & wanted) <= placed:
                ordered.append(name)
                placed.add(name)
                progressed = True
        if not progressed:  # config validation already rejects cycles
            raise BatchError("circular_reference",
                             "circular 'ref' dependency", 500)
    return ordered


def with_dependents(configs: Dict[str, EntityConfig], names: Iterable[str]) -> Set[str]:
    """`names` plus every entity that (transitively) references any of them."""
    result = set(names)
    changed = True
    while changed:
        changed = False
        for name, cfg in configs.items():
            if name not in result and _deps(cfg) & result:
                result.add(name)
                changed = True
    return result


def _validate(
    configs: Dict[str, EntityConfig],
    mode: str,
    entities: Optional[List[str]],
    counts: Optional[Dict[str, int]],
    count: Optional[int],
    batch_size: int,
) -> None:
    if mode not in VALID_MODES:
        raise BatchError(
            "invalid_mode", f"mode must be one of {list(VALID_MODES)}", 422)
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise BatchError("invalid_batch_size",
                         f"batch_size must be 1-{MAX_BATCH_SIZE}", 422)
    for name in list(entities or []) + list((counts or {}).keys()):
        if name not in configs:
            raise BatchError(
                "unknown_entity", f"unknown entity '{name}'. Known: {sorted(configs)}", 422
            )
    values = list((counts or {}).values()) + \
        ([count] if count is not None else [])
    if any(not 0 <= v <= MAX_ROWS_PER_ENTITY for v in values):
        raise BatchError(
            "invalid_count", f"row counts must be 0-{MAX_ROWS_PER_ENTITY}", 422)


def _load_ids(
    conn: Connection, tables: Dict[str, Table], configs: Dict[str, EntityConfig], names: Iterable[str]
) -> Dict[str, List[Any]]:
    # Ordered by primary key so a seeded run picks the same parents every time.
    return {
        n: [
            r[0]
            for r in conn.execute(
                select(tables[n].c[configs[n].primary_key_field]).order_by(
                    tables[n].c[configs[n].primary_key_field]
                )
            )
        ]
        for n in names
    }


def _wipe(conn: Connection, table: Table, cfg: EntityConfig, mode: str, batch_size: int) -> int:
    """Delete every row of an entity. Returns how many rows were removed."""
    pk_col = table.c[cfg.primary_key_field]
    ids = [r[0] for r in conn.execute(select(pk_col))]

    if mode == "replace" and ids:
        # Tombstones: consumers must be able to see these rows disappear.
        version = _next_version(conn, table) - 1
        now = datetime.now(timezone.utc)
        for start in range(0, len(ids), batch_size):
            logs = []
            for pk in ids[start: start + batch_size]:
                version += 1
                logs.append(
                    {"entity": cfg.entity, "pk_value": str(pk), "op": "delete",
                     "version": version, "changed_at": now}
                )
            conn.execute(insert(models.change_log), logs)

    conn.execute(delete(table))

    if mode == "reset":
        conn.execute(delete(models.change_log).where(
            models.change_log.c.entity == cfg.entity))
        conn.execute(delete(models.scheduler_runs).where(
            models.scheduler_runs.c.entity == cfg.entity))
    return len(ids)


def _generate(
    conn: Connection,
    table: Table,
    cfg: EntityConfig,
    n: int,
    batch_size: int,
    rng: random.Random,
    all_ids: Dict[str, List[Any]],
) -> Dict[str, Any]:
    """Insert `n` synthetic rows in chunks, logging each one to the change log."""
    pk_field = cfg.primary_key_field
    version = _next_version(conn, table) - 1  # last version already used
    first_version = version + 1
    inserted = batches = 0

    while inserted < n:
        size = min(batch_size, n - inserted)
        rows: List[Dict[str, Any]] = []
        logs: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for _ in range(size):
            try:
                row = generate_row(cfg, all_ids, rng)
            except MissingReferenceError as exc:
                raise BatchError("missing_parent_rows", str(exc), 409) from exc
            version += 1
            row["version"] = version
            rows.append(row)
            logs.append(
                {"entity": cfg.entity, "pk_value": str(row[pk_field]), "op": "insert",
                 "version": version, "changed_at": now}
            )
            all_ids.setdefault(cfg.entity, []).append(row[pk_field])
        try:
            conn.execute(insert(table), rows)
        except IntegrityError as exc:
            # Likely cause: the same literal seed was reused for a previous insert into
            # this still-live table, and the two runs' random sequences happened to
            # realign after enough rows — a real, if uncommon, property of seeded PRNGs,
            # not a hash collision. A fresh seed sidesteps it entirely.
            raise BatchError(
                "duplicate_primary_key",
                f"generated a row for '{cfg.entity}' whose key already exists. This usually "
                f"means the seed used for this request was already used for an earlier insert "
                f"into this table that is still live; retry with a different seed.",
                409,
            ) from exc
        conn.execute(insert(models.change_log), logs)
        inserted += size
        batches += 1

    return {
        "inserted": inserted,
        "batches": batches,
        "first_version": first_version if inserted else None,
        "last_version": version if inserted else None,
    }


def run_batch(
    engine: Engine,
    tables: Dict[str, Table],
    configs: Dict[str, EntityConfig],
    *,
    mode: str = "append",
    entities: Optional[List[str]] = None,
    counts: Optional[Dict[str, int]] = None,
    count: Optional[int] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    seed: Optional[int] = None,
    confirm: bool = False,
    lock_timeout: float = 30.0,
) -> Dict[str, Any]:
    """Generate (and optionally wipe first) synthetic rows. See module docstring.

    Row counts per entity: `counts[entity]`, else `count`, else the entity's
    `seed.initial_count` from its config. With no `entities` given, targets are
    the keys of `counts` if any, otherwise every entity.
    """
    _validate(configs, mode, entities, counts, count, batch_size)

    if entities:
        requested = list(dict.fromkeys(entities))
    elif counts:
        requested = list(counts)
    else:
        requested = list(configs)

    if mode == "append":
        targets = dependency_order(configs, requested)
        auto_included: List[str] = []
    else:
        targets = dependency_order(
            configs, with_dependents(configs, requested))
        auto_included = [n for n in targets if n not in requested]

    stray = set(counts or {}) - set(targets)
    if stray:
        raise BatchError(
            "counts_not_targeted",
            f"counts given for entities that aren't being generated: {sorted(stray)}",
            422,
        )
    if mode != "append" and not confirm:
        raise BatchError(
            "confirmation_required",
            f"mode '{mode}' deletes existing data; confirm it (confirm=true in the API, --yes on the CLI)",
            400,
        )

    plan = {
        n: (counts or {}).get(
            n, count if count is not None else configs[n].seed.initial_count)
        for n in targets
    }
    rng = random.Random(seed)
    started = time.perf_counter()

    if not WRITE_LOCK.acquire(timeout=lock_timeout):
        raise BatchError(
            "busy", "another bulk write is in progress; try again shortly", 409)
    try:
        with engine.begin() as conn:  # one transaction: all-or-nothing
            wiped: Dict[str, int] = {}
            if mode != "append":
                for name in reversed(targets):  # children before parents
                    wiped[name] = _wipe(conn, tables[name],
                                        configs[name], mode, batch_size)

            parents = {ref for n in targets for ref in _deps(configs[n])}
            all_ids = _load_ids(conn, tables, configs, set(targets) | parents)

            results = []
            for name in targets:
                stats = _generate(
                    conn, tables[name], configs[name], plan[name], batch_size, rng, all_ids)
                total = conn.execute(
                    select(func.count()).select_from(tables[name])).scalar()
                results.append({"entity": name, "deleted": wiped.get(
                    name, 0), **stats, "total_rows": total})
    finally:
        WRITE_LOCK.release()

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    logger.info("batch mode=%s targets=%s duration_ms=%s",
                mode, targets, duration_ms)
    return {
        "mode": mode,
        "seed": seed,
        "batch_size": batch_size,
        "entities": results,
        "auto_included": auto_included,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# Change batches: inserts + updates + soft-deletes, on demand
# ---------------------------------------------------------------------------
def _load_live_ids(
    conn: Connection, tables: Dict[str, Table], configs: Dict[str, EntityConfig], names: Iterable[str]
) -> Dict[str, List[Any]]:
    """Primary keys of rows that aren't soft-deleted, in a stable order."""
    live: Dict[str, List[Any]] = {}
    for n in names:
        cfg, table = configs[n], tables[n]
        pk = table.c[cfg.primary_key_field]
        stmt = select(pk).order_by(pk)
        if cfg.soft_delete_field:
            stmt = stmt.where(table.c[cfg.soft_delete_field].is_(None))
        live[n] = [r[0] for r in conn.execute(stmt)]
    return live


def _apply_changes(
    conn: Connection,
    table: Table,
    cfg: EntityConfig,
    inserts: Optional[int],
    updates: Optional[int],
    deletes: Optional[int],
    batch_size: int,
    rng: random.Random,
    live: Dict[str, List[Any]],
    notes: List[str],
) -> Dict[str, Any]:
    name = cfg.entity
    pk_col = table.c[cfg.primary_key_field]
    deleted_field = cfg.soft_delete_field
    # the rows that existed before this request
    before = list(live.get(name, []))

    if deletes and not deleted_field:
        raise BatchError(
            "not_supported", f"'{name}' has no soft-delete field, so it can't take deletes", 422)

    # Anything the caller leaves out defaults to one scheduler tick's worth.
    n_ins = inserts if inserts is not None else rng.randint(
        *cfg.update_schedule.new_records)
    n_upd = updates if updates is not None else int(
        len(before) * cfg.update_schedule.mutate_existing_pct / 100)
    n_upd_applied = min(n_upd, len(before))
    if n_upd_applied < n_upd:
        notes.append(
            f"{name}: asked to update {n_upd} rows but only {len(before)} are live; updated {n_upd_applied}")
    remaining = len(before) - n_upd_applied
    if deletes is not None:
        n_del = deletes
    else:
        n_del = int(remaining * cfg.update_schedule.soft_delete_pct /
                    100) if deleted_field else 0
    n_del_applied = min(n_del, remaining)
    if n_del_applied < n_del:
        notes.append(
            f"{name}: asked to delete {n_del} rows but only {remaining} were left to choose from; deleted {n_del_applied}")

    chosen = rng.sample(before, n_upd_applied + n_del_applied)
    upd_ids, del_ids = chosen[:n_upd_applied], chosen[n_upd_applied:]

    first = _next_version(conn, table)
    ins = _generate(conn, table, cfg, n_ins, batch_size,
                    rng, live)  # new rows join `live`
    version = _next_version(conn, table) - 1

    if upd_ids:
        auto_updated = [n for n, f in cfg.fields.items()
                        if f.auto == "updated"]
        set_cols = [n for n, _ in mutable_fields(cfg)] + auto_updated
        stmt = (
            update(table)
            .where(pk_col == bindparam("__pk"))
            .values({**{c: bindparam(f"__{c}") for c in set_cols}, "version": bindparam("__version")})
        )
        for start in range(0, len(upd_ids), batch_size):
            params, logs = [], []
            now = datetime.now(timezone.utc)
            for pk in upd_ids[start: start + batch_size]:
                version += 1
                row = {"__pk": pk, "__version": version}
                row.update(
                    {f"__{c}": v for c, v in generate_update(cfg, rng).items()})
                row.update({f"__{c}": now for c in auto_updated})
                params.append(row)
                logs.append({"entity": name, "pk_value": str(
                    pk), "op": "update", "version": version, "changed_at": now})
            conn.execute(stmt, params)
            conn.execute(insert(models.change_log), logs)

    if del_ids:
        stmt = (
            update(table)
            .where(pk_col == bindparam("__pk"))
            .values({deleted_field: bindparam("__deleted"), "version": bindparam("__version")})
        )
        for start in range(0, len(del_ids), batch_size):
            params, logs = [], []
            now = datetime.now(timezone.utc)
            for pk in del_ids[start: start + batch_size]:
                version += 1
                params.append(
                    {"__pk": pk, "__deleted": now, "__version": version})
                logs.append({"entity": name, "pk_value": str(
                    pk), "op": "delete", "version": version, "changed_at": now})
            conn.execute(stmt, params)
            conn.execute(insert(models.change_log), logs)
        gone = set(del_ids)
        # children shouldn't pick deleted parents
        live[name] = [i for i in live[name] if i not in gone]

    changed = ins["inserted"] + len(upd_ids) + len(del_ids)
    return {
        "entity": name,
        "inserted": ins["inserted"],
        "updated": len(upd_ids),
        "deleted": len(del_ids),
        "first_version": first if changed else None,
        "last_version": version if changed else None,
    }


def run_changes(
    engine: Engine,
    tables: Dict[str, Table],
    configs: Dict[str, EntityConfig],
    *,
    entities: Optional[List[str]] = None,
    inserts: Optional[int] = None,
    updates: Optional[int] = None,
    deletes: Optional[int] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    seed: Optional[int] = None,
    lock_timeout: float = 30.0,
) -> Dict[str, Any]:
    """Apply one batch of changes — inserts, updates and soft-deletes — to each
    targeted entity, and write every one to the change log.

    This is "one scheduler tick, on demand and with sizes you choose": use it
    to give a pipeline something to pick up from /changes or a delta export.
    Updates rewrite every non-key, non-relationship field of the chosen rows.
    Counts you leave out default to one scheduler tick (from the entity's
    update_schedule); counts larger than the live rows available are clamped
    and reported under `notes`. Like every bulk write it is one transaction.
    """
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise BatchError("invalid_batch_size",
                         f"batch_size must be 1-{MAX_BATCH_SIZE}", 422)
    for name in entities or []:
        if name not in configs:
            raise BatchError(
                "unknown_entity", f"unknown entity '{name}'. Known: {sorted(configs)}", 422)
    for label, value in (("inserts", inserts), ("updates", updates), ("deletes", deletes)):
        if value is not None and not 0 <= value <= MAX_ROWS_PER_ENTITY:
            raise BatchError(
                "invalid_count", f"{label} must be 0-{MAX_ROWS_PER_ENTITY}", 422)

    targets = dependency_order(configs, dict.fromkeys(
        entities) if entities else configs)
    rng = random.Random(seed)
    started = time.perf_counter()
    notes: List[str] = []

    if not WRITE_LOCK.acquire(timeout=lock_timeout):
        raise BatchError(
            "busy", "another bulk write is in progress; try again shortly", 409)
    try:
        with engine.begin() as conn:
            parents = {ref for n in targets for ref in _deps(configs[n])}
            live = _load_live_ids(conn, tables, configs,
                                  set(targets) | parents)
            results = []
            for name in targets:
                stats = _apply_changes(
                    conn, tables[name], configs[name], inserts, updates, deletes,
                    batch_size, rng, live, notes,
                )
                stats["live_rows"] = len(live[name])
                stats["total_rows"] = conn.execute(
                    select(func.count()).select_from(tables[name])).scalar()
                results.append(stats)
    finally:
        WRITE_LOCK.release()

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    logger.info("changes targets=%s duration_ms=%s", targets, duration_ms)
    return {"seed": seed, "batch_size": batch_size, "entities": results, "notes": notes, "duration_ms": duration_ms}
