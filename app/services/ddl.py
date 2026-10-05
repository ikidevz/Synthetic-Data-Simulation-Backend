"""DDL export: CREATE TABLE / CREATE INDEX statements generated straight
from the entity configs (via the same dynamic tables the app runs on), so
the SQL you export can never drift from the schema the API actually uses.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional

from sqlalchemy import MetaData, Table
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from ..config.entities import EntityConfig
from ..db import models

DIALECTS = {"sqlite": sqlite.dialect, "postgresql": postgresql.dialect}


def generate_ddl(
    tables: Dict[str, Table],
    dialect: str = "sqlite",
    include_system: bool = False,
    meta: Optional[MetaData] = None,
) -> str:
    """Return runnable DDL for every entity table, parents before children.

    include_system=True also emits the change_log and scheduler_runs tables
    the app uses internally. Statements use IF NOT EXISTS so the script can
    be re-run safely.
    """
    if dialect not in DIALECTS:
        raise ValueError(
            f"Unsupported dialect '{dialect}'. Supported: {sorted(DIALECTS)}")
    dialect_obj = DIALECTS[dialect]()

    wanted = {t.name for t in tables.values()}
    if include_system:
        wanted |= {models.change_log.name, models.scheduler_runs.name}

    statements = []
    # sorted_tables orders by foreign-key dependency: parents first.
    for table in (models.metadata if meta is None else meta).sorted_tables:
        if table.name not in wanted:
            continue
        statements.append(
            str(CreateTable(table, if_not_exists=True).compile(
                dialect=dialect_obj)).strip() + ";"
        )
        for index in sorted(table.indexes, key=lambda i: i.name):
            statements.append(
                str(CreateIndex(index, if_not_exists=True).compile(
                    dialect=dialect_obj)).strip() + ";"
            )

    header = f"-- Generated from entity configs (dialect: {dialect})\n\n"
    return header + "\n\n".join(statements) + "\n"


def generate_ddl_labeled(
    configs: Dict[str, EntityConfig],
    labels: Dict[str, str],
    selected: Iterable[str],
    dialect: str = "sqlite",
    include_parents: bool = False,
) -> str:
    """DDL under the provider's own table names instead of the namespaced physical ones.

    `configs` is one provider's {physical name: config}; `labels` maps each physical
    name to its logical name. The tables are rebuilt on a throwaway MetaData under the
    logical names (foreign keys included), so the script reads as the provider wrote
    it. `include_parents` adds every table the selection references, directly or not,
    so the script runs on its own.
    """
    meta = MetaData()
    logical: Dict[str, EntityConfig] = {}
    for physical, cfg in configs.items():
        copy = cfg.model_copy(deep=True)
        copy.entity = labels[physical]
        for f in copy.fields.values():
            if f.type == "ref":
                f.ref_entity = labels[f.ref_entity]
        logical[copy.entity] = copy
    tables = models.build_tables(logical, meta=meta)

    wanted = {labels[p] for p in selected}
    if include_parents:
        stack = list(wanted)
        while stack:
            for f in logical[stack.pop()].fields.values():
                if f.type == "ref" and f.ref_entity not in wanted:
                    wanted.add(f.ref_entity)
                    stack.append(f.ref_entity)
    return generate_ddl({n: tables[n] for n in wanted}, dialect=dialect, meta=meta)
