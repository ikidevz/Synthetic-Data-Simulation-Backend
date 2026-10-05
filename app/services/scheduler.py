"""Per-entity background jobs.

Each entity gets its own APScheduler job, running at the cadence its
config specifies. Every run: inserts new synthetic rows, mutates a % of
existing rows (bumping 'updated_at' + 'version'), and soft-deletes a %
of the rest. Every run is logged to the shared `scheduler_runs` table —
that log is what /metrics and /scheduler/runs read from.
"""
from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import Table, select, update, insert, func
from sqlalchemy.engine import Engine, Connection

from ..config.entities import EntityConfig
from ..db import models
from .changefeed import log_change
from .generator import generate_row, generate_update

logger = logging.getLogger("scheduler")

_CADENCE_SECONDS = {
    "hourly": 60 * 60,
    "daily": 60 * 60 * 24,
    "weekly": 60 * 60 * 24 * 7,
}


# Serialises the scheduler's write cycles and batch generation (see batch.py),
# so two bulk writers never race to allocate the same version numbers.
WRITE_LOCK = threading.Lock()


def _next_version(conn: Connection, table: Table) -> int:
    """Next version for this entity = highest version ever used + 1.

    "Ever used" looks at both the table and the change_log: after a batch
    'replace' wipes the table, the change_log still remembers the old
    versions, so the counter keeps climbing instead of restarting at 1 and
    sending consumers' cursors backwards.

    A simpler stand-in for a dedicated sequence table — same guarantee
    (monotonic, per-entity) under serialised writers. Worth revisiting a
    real sequence if this ever needs concurrent writers on one entity.
    """
    in_table = conn.execute(select(func.max(table.c.version))).scalar() or 0
    in_log = (
        conn.execute(
            select(func.max(models.change_log.c.version)).where(
                models.change_log.c.entity == table.name
            )
        ).scalar()
        or 0
    )
    return max(in_table, in_log) + 1


def _existing_ids(
    conn: Connection,
    tables: Dict[str, Table],
    configs: Dict[str, EntityConfig],
    names: Optional[Iterable[str]] = None,
) -> Dict[str, List]:
    """{entity_name: [primary key values]} — used both to resolve 'ref' fields and
    to pick mutation/deletion targets. Every entity by default; pass `names` to load
    only the ones you need (an entity and the parents it references)."""
    ids: Dict[str, List] = {}
    wanted = None if names is None else set(names)
    for name, table in tables.items():
        if wanted is not None and name not in wanted:
            continue
        pk = configs[name].primary_key_field
        rows = conn.execute(select(table.c[pk])).all()
        ids[name] = [r[0] for r in rows]
    return ids


def _run_entity_job(
    engine: Engine,
    entity_name: str,
    tables: Dict[str, Table],
    configs: Dict[str, EntityConfig],
) -> None:
    """Runs one insert/mutate/soft-delete cycle for a single entity.
    Safe to call directly (e.g. from tests) without waiting on real time."""
    cfg = configs[entity_name]
    table = tables[entity_name]
    started_at = datetime.now(timezone.utc)
    inserted = mutated = deleted = 0
    status = "success"

    try:
        with engine.begin() as conn:
            needed = {entity_name} | {
                f.ref_entity for f in cfg.fields.values() if f.type == "ref"}
            all_ids = _existing_ids(conn, tables, configs, needed)
            # Rows this tick may edit or delete: the ones that are live BEFORE it inserts
            # anything. (Otherwise a tick can update a row it created a moment ago, or
            # touch rows that were already soft-deleted.)
            live_stmt = select(table.c[cfg.primary_key_field])
            if cfg.soft_delete_field:
                live_stmt = live_stmt.where(
                    table.c[cfg.soft_delete_field].is_(None))
            live_before = [r[0] for r in conn.execute(live_stmt)]
            pk_field = cfg.primary_key_field
            pk_col = table.c[pk_field]

            # 1. Insert new synthetic rows
            lo, hi = cfg.update_schedule.new_records
            n_new = random.randint(lo, hi)
            if cfg.max_rows is not None:  # provider quota: never grow past the cap
                total = conn.execute(
                    select(func.count()).select_from(table)).scalar() or 0
                n_new = max(0, min(n_new, cfg.max_rows - total))
            for _ in range(n_new):
                row = generate_row(cfg, all_ids)
                row["version"] = _next_version(conn, table)
                conn.execute(insert(table).values(**row))
                log_change(conn, entity_name,
                           row[pk_field], "insert", row["version"])
                inserted += 1
                all_ids.setdefault(entity_name, []).append(row[pk_field])

            existing_ids = live_before

            # 2. Mutate a % of existing rows (bump 'updated_at' fields + version)
            n_mutate = int(len(existing_ids) *
                           cfg.update_schedule.mutate_existing_pct / 100)
            mutate_targets = (
                random.sample(existing_ids, min(n_mutate, len(existing_ids)))
                if existing_ids
                else []
            )
            for target_id in mutate_targets:
                new_version = _next_version(conn, table)
                values = {"version": new_version}
                # a real edit changes data, not just a timestamp
                values.update(generate_update(cfg))
                for fname, field in cfg.fields.items():
                    if field.auto == "updated":
                        values[fname] = datetime.now(timezone.utc)
                conn.execute(update(table).where(
                    pk_col == target_id).values(**values))
                log_change(conn, entity_name, target_id, "update", new_version)
                mutated += 1

            # 3. Soft-delete a % of the remaining (non-mutated) rows
            deleted_field = cfg.soft_delete_field
            remaining_ids = [
                i for i in existing_ids if i not in mutate_targets]
            n_delete = int(len(remaining_ids) *
                           cfg.update_schedule.soft_delete_pct / 100)
            if deleted_field and n_delete:
                delete_targets = random.sample(
                    remaining_ids, min(n_delete, len(remaining_ids)))
                for target_id in delete_targets:
                    new_version = _next_version(conn, table)
                    conn.execute(
                        update(table)
                        .where(pk_col == target_id)
                        .values(**{deleted_field: datetime.now(timezone.utc), "version": new_version})
                    )
                    log_change(conn, entity_name, target_id,
                               "delete", new_version)
                    deleted += 1

    except Exception:
        status = "failed"
        logger.exception("Scheduler job failed for entity '%s'", entity_name)

    finished_at = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            insert(models.scheduler_runs).values(
                entity=entity_name,
                started_at=started_at,
                finished_at=finished_at,
                rows_inserted=inserted,
                rows_mutated=mutated,
                rows_deleted=deleted,
                status=status,
            )
        )
    logger.info(
        "scheduler run entity=%s inserted=%d mutated=%d deleted=%d status=%s",
        entity_name,
        inserted,
        mutated,
        deleted,
        status,
    )


def run_entity_job(
    engine: Engine,
    entity_name: str,
    tables: Dict[str, Table],
    configs: Dict[str, EntityConfig],
) -> None:
    """Runs one insert/mutate/soft-delete cycle for a single entity.
    Safe to call directly (e.g. from tests) without waiting on real time."""
    with WRITE_LOCK:
        _run_entity_job(engine, entity_name, tables, configs)


def schedule_entity(
    scheduler: BackgroundScheduler,
    engine: Engine,
    name: str,
    tables: Dict[str, Table],
    configs: Dict[str, EntityConfig],
) -> None:
    """(Re)register one entity's job on the entity's configured cadence."""
    cfg = configs[name]
    scheduler.add_job(
        run_entity_job,
        trigger=IntervalTrigger(
            seconds=_CADENCE_SECONDS[cfg.update_schedule.cadence],
            jitter=cfg.update_schedule.jitter_seconds or None,
        ),
        args=[engine, name, tables, configs],
        id=f"job_{name}",
        replace_existing=True,
    )


def unschedule_entity(scheduler: BackgroundScheduler, name: str) -> None:
    try:
        scheduler.remove_job(f"job_{name}")
    except Exception:
        pass


def start_scheduler(
    engine: Engine,
    tables: Dict[str, Table],
    configs: Dict[str, EntityConfig],
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    for name in configs:
        schedule_entity(scheduler, engine, name, tables, configs)
    scheduler.start()
    return scheduler
