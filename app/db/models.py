"""Builds SQLAlchemy Core Table objects dynamically from EntityConfig.

Deliberately uses SQLAlchemy Core (not the declarative ORM) — schema here
is *data* (parsed from YAML at runtime), and Core's Table/Column objects
can be constructed dynamically without fighting the ORM's class-based
metaclass machinery.
"""
from __future__ import annotations

from typing import Dict, Optional

from sqlalchemy import (
    MetaData,
    Table,
    Column,
    Index,
    String,
    Integer,
    Float,
    Boolean,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
)

from ..config.entities import EntityConfig

metadata = MetaData()

_TYPE_MAP = {
    "uuid": String,
    "int": Integer,
    "float": Float,
    "string": String,
    "enum": String,
    "bool": Boolean,
    "timestamp": DateTime,
}


def build_table(cfg: EntityConfig, configs: Dict[str, EntityConfig], meta: Optional[MetaData] = None) -> Table:
    """Build (and register on `meta`) the Table for one entity. `configs` must contain
    every entity this one references, so the foreign keys can name the right column."""
    meta = metadata if meta is None else meta
    name = cfg.entity

    # Drop any earlier definition first, so rebuilding (tests, reloads)
    # never stacks duplicate columns or indexes on the shared MetaData.
    if name in meta.tables:
        meta.remove(meta.tables[name])

    columns = []
    for field_name, field in cfg.fields.items():
        if field.type == "ref":
            ref_cfg = configs[field.ref_entity]
            ref_pk = ref_cfg.primary_key_field
            col = Column(
                field_name,
                String,
                ForeignKey(f"{field.ref_entity}.{ref_pk}"),
                nullable=field.nullable,
            )
        else:
            sql_type = _TYPE_MAP[field.type]
            col = Column(
                field_name,
                sql_type,
                primary_key=field.primary_key,
                nullable=field.nullable,
            )
        columns.append(col)

    # The change feed and version allocation both filter/sort on version.
    extras = [Index(f"idx_{name}_version", "version")
              ] if "version" in cfg.fields else []
    return Table(name, meta, *columns, *extras)


def build_tables(configs: Dict[str, EntityConfig], meta: Optional[MetaData] = None) -> Dict[str, Table]:
    """Return {entity_name: sqlalchemy.Table}, all registered on one shared
    MetaData so foreign keys between entities resolve correctly."""
    return {name: build_table(cfg, configs, meta) for name, cfg in configs.items()}


# Shared append-only change log. Every insert/update/soft-delete on any
# entity writes one row here — this is what the change feed actually
# reads from, rather than trying to infer "was this an insert or an
# update?" from a bare snapshot of the row (version alone can't tell you
# that: it's a monotonic counter for the whole table, not a per-row
# revision count).
change_log = Table(
    "change_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("entity", String, nullable=False),
    Column("pk_value", String, nullable=False),
    Column("op", String, nullable=False),  # insert | update | delete
    Column("version", Integer, nullable=False),
    Column("changed_at", DateTime, nullable=False),
    Index("idx_change_log_entity_version", "entity", "version"),
)


# Shared table that logs every scheduler run, across all entities.
scheduler_runs = Table(
    "scheduler_runs",
    metadata,
    Column("run_id", Integer, primary_key=True, autoincrement=True),
    Column("entity", String, nullable=False),
    Column("started_at", DateTime, nullable=False),
    Column("finished_at", DateTime),
    Column("rows_inserted", Integer, default=0),
    Column("rows_mutated", Integer, default=0),
    Column("rows_deleted", Integer, default=0),
    Column("status", String, nullable=False),
)


# ---------------------------------------------------------------------------
# Provider registry: who may publish configs, how they authenticate, and the
# configs they published. Prefixed `registry_` so they can never collide with
# an entity table named in a YAML file.
# ---------------------------------------------------------------------------
registry_providers = Table(
    "registry_providers",
    metadata,
    Column("id", String, primary_key=True),
    Column("full_name", String, nullable=False),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("is_system", Boolean, nullable=False, default=False),
    Column("created_at", DateTime, nullable=False),
)

# Only a SHA-256 of each key is stored; the key itself is shown once, at creation.
registry_api_keys = Table(
    "registry_api_keys",
    metadata,
    Column("id", String, primary_key=True),
    Column("provider_id", String, ForeignKey(
        "registry_providers.id"), nullable=False, index=True),
    Column("key_hash", String, nullable=False, unique=True),
    Column("key_prefix", String, nullable=False),
    Column("label", String),
    Column("created_at", DateTime, nullable=False),
    Column("revoked_at", DateTime),
    Column("last_used_at", DateTime),
)


registry_configs = Table(
    "registry_configs",
    metadata,
    Column("id", String, primary_key=True),
    Column("provider_id", String, ForeignKey(
        "registry_providers.id"), nullable=False, index=True),
    Column("name", String, nullable=False),
    Column("physical_name", String, nullable=False, unique=True),
    Column("config_json", Text, nullable=False),
    Column("is_only_me", Boolean, nullable=False, default=False),
    Column("source", String, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint("provider_id", "name",
                     name="uq_registry_configs_provider_name"),
)

REGISTRY_TABLES = [registry_providers, registry_api_keys, registry_configs]
