"""Read-only system endpoints: row counts, last scheduler run per entity,
and recent scheduler run history. This is what answers "how would you
know if this pipeline was silently broken?" in an interview.
"""
from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy import select, func, desc
from sqlalchemy.engine import Engine

from ..config.entities import EntityConfig
from ..db import models


def get_metrics(engine: Engine, tables: Dict[str, Any], configs: Dict[str, EntityConfig]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    with engine.connect() as conn:
        for name, table in tables.items():
            row_count = conn.execute(
                select(func.count()).select_from(table)).scalar()
            last_run = (
                conn.execute(
                    select(models.scheduler_runs)
                    .where(models.scheduler_runs.c.entity == name)
                    .order_by(desc(models.scheduler_runs.c.run_id))
                    .limit(1)
                )
                .mappings()
                .first()
            )
            result[name] = {
                "row_count": row_count,
                "last_run": last_run["finished_at"].isoformat()
                if last_run and last_run["finished_at"]
                else None,
                "last_status": last_run["status"] if last_run else None,
            }
    return result


def get_scheduler_runs(engine: Engine, entity: str, limit: int = 20) -> List[Dict[str, Any]]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(models.scheduler_runs)
                .where(models.scheduler_runs.c.entity == entity)
                .order_by(desc(models.scheduler_runs.c.run_id))
                .limit(limit)
            )
            .mappings()
            .all()
        )
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for key in ("started_at", "finished_at"):
            if d.get(key):
                d[key] = d[key].isoformat()
        out.append(d)
    return out
