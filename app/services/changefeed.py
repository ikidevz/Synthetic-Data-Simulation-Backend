"""Incremental change-feed logic: GET /{entity}/changes?since=<version>.

Backed by an explicit append-only `change_log` table (see models.py)
rather than inferred from the entity row alone — a bare row snapshot
can't reliably tell you whether it was just inserted or just updated,
since `version` is a monotonic counter for the whole table, not a
per-row revision count.

Uses that per-entity monotonic 'version' integer as the cursor rather
than a timestamp, specifically to avoid clock-skew / same-millisecond
collision bugs that timestamp-based cursors are prone to (see the
blueprint's Gotchas section).

Known simplification: each change-feed entry is enriched with the row's
*current* field values (fetched fresh at read time), not a point-in-time
snapshot taken when that change happened. For a portfolio/demo project
this is a reasonable tradeoff — a fully correct implementation would
store a field snapshot per change_log row, at the cost of real storage
and write overhead.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy import select, insert, func, Table
from sqlalchemy.engine import Connection

from ..config.entities import EntityConfig
from ..db import models


class CursorAheadError(Exception):
    """The caller's cursor is beyond the newest version this source has.

    The usual cause is a batch 'reset', which restarts versions at 1 —
    a consumer holding an old cursor would otherwise poll forever and
    silently never see the regenerated data.
    """

    def __init__(self, since: int, latest: int):
        super().__init__(f"cursor {since} is ahead of latest version {latest}")
        self.since = since
        self.latest = latest


def latest_version(conn: Connection, entity: str) -> int:
    return (
        conn.execute(
            select(func.max(models.change_log.c.version)).where(
                models.change_log.c.entity == entity
            )
        ).scalar()
        or 0
    )


def log_change(conn: Connection, entity: str, pk_value: Any, op: str, version: int) -> None:
    conn.execute(
        insert(models.change_log).values(
            entity=entity,
            pk_value=str(pk_value),
            op=op,
            version=version,
            changed_at=datetime.now(timezone.utc),
        )
    )


def get_changes(
    conn: Connection,
    table: Table,
    cfg: EntityConfig,
    since: int,
    limit: int = 100,
) -> Dict[str, Any]:
    latest = latest_version(conn, cfg.entity)
    if since > latest:
        raise CursorAheadError(since, latest)

    log_stmt = (
        select(models.change_log)
        .where(models.change_log.c.entity == cfg.entity)
        .where(models.change_log.c.version > since)
        .order_by(models.change_log.c.version.asc())
        .limit(limit)
    )
    log_rows = conn.execute(log_stmt).mappings().all()

    pk_col = table.c[cfg.primary_key_field]
    changes: List[Dict[str, Any]] = []
    max_version = since

    for log_row in log_rows:
        max_version = max(max_version, log_row["version"])
        current = conn.execute(select(table).where(
            pk_col == log_row["pk_value"])).mappings().first()
        entry = _serialize(dict(current)) if current else {
            cfg.primary_key_field: log_row["pk_value"]}
        entry["op"] = log_row["op"]
        entry["version"] = log_row["version"]
        changes.append(entry)

    return {
        "changes": changes,
        "next_cursor": max_version,
        "latest_version": latest,
        "has_more": len(log_rows) == limit,
    }


def _serialize(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
