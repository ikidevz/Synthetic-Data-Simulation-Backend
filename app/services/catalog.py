"""The live catalog: every published config, the table that holds its data, its
background job — and the rules for who may touch them.

Where things live
-----------------
  registry_configs (database)   the definition each provider published, plus `is_only_me`
  Catalog.entries (memory)      the same, with the runtime config, Table object and owner
  one physical table per config `d_<10 hex>_<name>` for provider configs, the plain entity
                                name for the built-in YAML examples

Namespacing is what lets the existing engine (change feed, scheduler, batch, export)
serve many providers unchanged: `change_log` and `scheduler_runs` already key on the
entity name, and a provider's physical table names never collide with anyone else's. Each
provider's configs also get their own `(configs, tables)` *scope*, so a `ref` field can
only ever resolve to that provider's own rows, and bulk operations (`replace`, `reset`,
which also refresh dependents) can only ever reach that provider's own tables.

Who may do what
---------------
  read   the owner, the superuser, and — unless `is_only_me` is set — any other provider
  write  the owner and the superuser only
  A config marked `is_only_me` is invisible to everyone else: 404, not 403, so its
  existence isn't revealed.

The catalog lives in one process's memory, like the scheduler and the write lock. Run
a single instance (see the README's limitations).
"""
from __future__ import annotations

import json
import logging
import random
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, select

from ..config.entities import EntityConfig
from ..config.provider import Limits, get_limits, normalize, physical_name, to_runtime, validate
from ..core.errors import ApiError
from ..db import models
from ..db.engine import engine
from ..security.api_keys import Principal
from . import registry
from .batch import DEFAULT_BATCH_SIZE, _deps, _generate, _load_ids
from .scheduler import WRITE_LOCK, schedule_entity, unschedule_entity

logger = logging.getLogger("catalog")

YAML_SCOPE = "yaml"  # the built-in examples share one scope: the legacy CONFIGS / TABLES
PATCHABLE = frozenset(
    {"is_only_me", "seed", "update_schedule", "failure_injection", "fields"})
LOCK_TIMEOUT = 30.0

Scope = Tuple[Dict[str, EntityConfig], Dict[str, Any]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def scrub(text: str, labels: Dict[str, str]) -> str:
    """Replace namespaced table names in a message with the names the provider chose."""
    for physical in sorted(labels, key=len, reverse=True):
        text = text.replace(physical, labels[physical])
    return text


@dataclass
class Entry:
    id: str
    provider_id: str
    name: str            # the provider's own name for it
    physical: str        # the table it lives in
    source: str          # "api" | "yaml"
    is_only_me: bool
    stored: EntityConfig  # as the provider wrote it (logical names)
    cfg: EntityConfig     # as the engine runs it (physical names)
    table: Any
    created_at: Any
    updated_at: Any


class Catalog:
    def __init__(self) -> None:
        # guards the structures below and config create/edit/delete
        self.lock = threading.RLock()
        self.entries: Dict[str, Entry] = {}
        self.scopes: Dict[str, Scope] = {}
        self.scheduler: Any = None

    # ------------------------------------------------------------ loading ----
    def load(self, system_configs: Dict[str, EntityConfig], system_tables: Dict[str, Any]) -> None:
        """Rebuild the catalog at startup: sync the YAML examples into the registry, then
        load every provider config (building its Table object)."""
        with self.lock:
            self.entries.clear()
            self.scopes.clear()
            registry.ensure_system_provider()
            self.scopes[YAML_SCOPE] = (system_configs, system_tables)

            now = _now()
            registry.sync_yaml_rows([
                {
                    "id": f"cfg_sys_{name}", "provider_id": registry.SYSTEM_PROVIDER_ID, "name": name,
                    "physical_name": name, "config_json": json.dumps(normalize(cfg)), "is_only_me": False,
                    "source": "yaml", "created_at": now, "updated_at": now,
                }
                for name, cfg in system_configs.items()
            ])

            by_provider: Dict[str, List[Dict[str, Any]]] = {}
            for row in registry.list_config_rows():
                if row["source"] == "yaml":
                    cfg = system_configs.get(row["name"])
                    if cfg is not None:
                        self.entries[row["id"]] = self._entry(
                            row, cfg, cfg, system_tables[row["name"]])
                else:
                    by_provider.setdefault(row["provider_id"], []).append(row)
            for provider_id, rows in by_provider.items():
                self._load_provider(provider_id, rows)

    @staticmethod
    def _entry(row: Dict[str, Any], stored: EntityConfig, cfg: EntityConfig, table: Any) -> Entry:
        return Entry(
            id=row["id"], provider_id=row["provider_id"], name=row["name"], physical=row["physical_name"],
            source=row["source"], is_only_me=bool(row["is_only_me"]), stored=stored, cfg=cfg, table=table,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def _load_provider(self, provider_id: str, rows: List[Dict[str, Any]]) -> None:
        limits = get_limits()
        stored: Dict[str, EntityConfig] = {}
        for row in rows:
            try:
                stored[row["id"]] = EntityConfig(
                    **json.loads(row["config_json"]))
            except Exception:  # a damaged row must not stop the whole app from starting
                logger.exception(
                    "skipping unreadable config %s (%s)", row["id"], row["name"])
        name_map = {r["name"]: r["physical_name"]
                    for r in rows if r["id"] in stored}
        configs, tables = self._scope_dicts(provider_id)
        runtime: Dict[str, EntityConfig] = {}
        for row in rows:
            if row["id"] not in stored:
                continue
            try:
                runtime[row["id"]] = to_runtime(
                    stored[row["id"]], row["physical_name"], name_map, limits.max_rows)
            except KeyError:
                logger.error(
                    "skipping config %s (%s): it references a config that no longer exists", row["id"], row["name"])
                continue
            configs[row["physical_name"]] = runtime[row["id"]]
        for row in rows:
            if row["id"] not in runtime:
                continue
            try:
                table = models.build_table(runtime[row["id"]], configs)
            except Exception:
                logger.exception(
                    "skipping config %s (%s): its table can't be built", row["id"], row["name"])
                configs.pop(row["physical_name"], None)
                continue
            tables[row["physical_name"]] = table
            self.entries[row["id"]] = self._entry(
                row, stored[row["id"]], runtime[row["id"]], table)

    def start_jobs(self, scheduler: Any) -> None:
        """Schedule a background job for every provider config (the YAML examples are
        scheduled by main.py's own `start_scheduler`)."""
        with self.lock:
            self.scheduler = scheduler
            for entry in list(self.entries.values()):
                if entry.source == "api":
                    configs, tables = self.scope(entry)
                    schedule_entity(scheduler, engine,
                                    entry.physical, tables, configs)

    def stop(self) -> None:
        with self.lock:
            self.scheduler = None

    # ------------------------------------------------------------- scopes ----
    @staticmethod
    def scope_key(entry: Entry) -> str:
        return entry.provider_id if entry.source == "api" else YAML_SCOPE

    def _scope_dicts(self, key: str) -> Scope:
        if key not in self.scopes:
            self.scopes[key] = ({}, {})
        return self.scopes[key]

    def scope(self, entry: Entry) -> Scope:
        return self._scope_dicts(self.scope_key(entry))

    def scope_entries(self, entry: Entry) -> List[Entry]:
        key = self.scope_key(entry)
        return [e for e in list(self.entries.values()) if self.scope_key(e) == key]

    def labels(self, entry: Entry) -> Dict[str, str]:
        """{physical table name: the provider's name} for everything in this entry's scope."""
        return {e.physical: e.name for e in self.scope_entries(entry)}

    def dependents(self, entry: Entry) -> List[Entry]:
        """Configs (same owner) with a ref field pointing at this one."""
        return [
            e for e in self.scope_entries(entry)
            if e is not entry and any(f.type == "ref" and f.ref_entity == entry.name for f in e.stored.fields.values())
        ]

    # ------------------------------------------------------------- access ----
    @staticmethod
    def can_write(principal: Principal, entry: Entry) -> bool:
        return principal.is_superuser or principal.provider_id == entry.provider_id

    def can_read(self, principal: Principal, entry: Entry) -> bool:
        return self.can_write(principal, entry) or not entry.is_only_me

    def resolve(self, config_id: str, principal: Principal, need: str = "read") -> Entry:
        """The entry, if `principal` may use it for `need` ("read" | "write")."""
        entry = self.entries.get(config_id)
        if entry is None or not self.can_read(principal, entry):
            raise ApiError("not_found", f"config '{config_id}' not found", 404)
        if need == "write" and not self.can_write(principal, entry):
            raise ApiError(
                "forbidden",
                f"config '{config_id}' belongs to {entry.provider_id}: you can read it, but only its owner can change it",
                403,
            )
        return entry

    def visible(self, principal: Principal, scope: str = "all") -> List[Entry]:
        """Everything `principal` may read. `mine` = their own; `shared` = other providers'
        public configs. (For the superuser, "mine" means the built-in system-owned ones.)"""
        out = []
        for entry in list(self.entries.values()):
            if not self.can_read(principal, entry):
                continue
            own = entry.provider_id == (
                registry.SYSTEM_PROVIDER_ID if principal.is_superuser else principal.provider_id)
            if (scope == "mine" and not own) or (scope == "shared" and own):
                continue
            out.append(entry)
        return sorted(out, key=lambda e: (e.name, e.id))

    def describe(
        self, entry: Entry, principal: Principal, owners: Optional[Dict[str, str]] = None, detail: bool = False
    ) -> Dict[str, Any]:
        owners = owners if owners is not None else registry.provider_names()
        out: Dict[str, Any] = {
            "id": entry.id,
            "name": entry.name,
            "owner": {"id": entry.provider_id, "full_name": owners.get(entry.provider_id)},
            "is_only_me": entry.is_only_me,
            "source": entry.source,
            "can_write": self.can_write(principal, entry),
            "cadence": entry.stored.update_schedule.cadence,
            "fields": list(entry.stored.fields),
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        }
        if detail:
            out["config"] = normalize(entry.stored)
        return out

    # ------------------------------------------------------------- create ----
    def create(self, owner_id: str, raw: Any, limits: Limits) -> Tuple[Entry, int]:
        """Validate a provider's config, create its table, seed it and schedule its job.
        Returns (entry, rows seeded). Everything is undone if any step fails."""
        with self.lock:
            if registry.get_provider(owner_id) is None:
                raise ApiError(
                    "not_found", f"provider '{owner_id}' not found", 404)
            owned = [e for e in list(self.entries.values())
                     if e.provider_id == owner_id]
            api_owned = [e for e in owned if e.source == "api"]
            if len(api_owned) >= limits.max_configs:
                raise ApiError(
                    "quota_exceeded",
                    f"a provider can hold at most {limits.max_configs} configs; delete one first", 409,
                )
            stored, flag = validate(
                raw, {e.name: e.stored for e in api_owned}, limits)
            if stored.entity in {e.name for e in owned}:
                raise ApiError(
                    "already_exists", f"you already have a config named '{stored.entity}'", 409)

            # shared for reading unless the provider says otherwise
            only_me = bool(flag)
            config_id, physical = self._new_ids(stored.entity)
            configs, tables = self._scope_dicts(owner_id)
            name_map = {e.name: e.physical for e in api_owned}
            name_map[stored.entity] = physical
            labels = {v: k for k, v in name_map.items()}
            runtime = to_runtime(stored, physical, name_map, limits.max_rows)

            now = _now()
            registry.insert_config_row({
                "id": config_id, "provider_id": owner_id, "name": stored.entity, "physical_name": physical,
                "config_json": json.dumps(normalize(stored)), "is_only_me": only_me, "source": "api",
                "created_at": now, "updated_at": now,
            })
            table = None
            try:
                configs[physical] = runtime
                table = models.build_table(runtime, configs)
                tables[physical] = table
                table.create(engine)
                seeded = self._seed(runtime, table, configs, tables)
            except ApiError as exc:
                self._discard(physical, configs, tables, config_id)
                raise ApiError(exc.code, scrub(
                    exc.message, labels), exc.status)
            except BaseException:
                self._discard(physical, configs, tables, config_id)
                raise

            entry = Entry(
                id=config_id, provider_id=owner_id, name=stored.entity, physical=physical, source="api",
                is_only_me=only_me, stored=stored, cfg=runtime, table=table, created_at=now, updated_at=now,
            )
            self.entries[config_id] = entry
            if self.scheduler is not None:
                schedule_entity(self.scheduler, engine,
                                physical, tables, configs)
            return entry, seeded

    def _new_ids(self, name: str) -> Tuple[str, str]:
        taken = {e.physical for e in list(self.entries.values())}
        while True:
            config_id = "cfg_" + secrets.token_hex(8)
            physical = physical_name(config_id, name)
            if physical not in taken:
                return config_id, physical

    def _seed(self, cfg: EntityConfig, table: Any, configs: Dict[str, EntityConfig],
              tables: Dict[str, Any], locked: bool = False) -> int:
        count = cfg.seed.initial_count
        if count <= 0:
            return 0
        if not locked and not WRITE_LOCK.acquire(timeout=LOCK_TIMEOUT):
            raise ApiError(
                "busy", "another bulk write is in progress; try again shortly", 409)
        try:
            with engine.begin() as conn:
                ids = _load_ids(conn, tables, configs, _deps(cfg))
                ids.setdefault(cfg.entity, [])
                _generate(conn, table, cfg, count,
                          DEFAULT_BATCH_SIZE, random.Random(), ids)
        finally:
            if not locked:
                WRITE_LOCK.release()
        return count

    def _discard(self, physical: str, configs: Dict[str, EntityConfig], tables: Dict[str, Any], config_id: str) -> None:
        """Undo a half-finished create."""
        configs.pop(physical, None)
        tables.pop(physical, None)
        table = models.metadata.tables.get(physical)
        if table is not None:
            try:
                table.drop(engine, checkfirst=True)
            except Exception:
                logger.exception(
                    "could not drop table %s while undoing a failed create", physical)
            models.metadata.remove(table)
        registry.delete_config_row(config_id)

    # ------------------------------------------------------------- update ----
    def update(self, entry: Entry, patch: Any, confirm: bool, limits: Limits) -> Tuple[Entry, Optional[int]]:
        """Apply a PATCH. Returns (entry, rows re-seeded or None). Changing `fields` rebuilds
        the table, which deletes its data, so it needs `confirm`."""
        with self.lock:
            if not isinstance(patch, dict) or not patch:
                raise ApiError(
                    "invalid_request", f"send at least one of: {sorted(PATCHABLE)}", 422)
            unknown = sorted(set(patch) - PATCHABLE)
            if unknown:
                raise ApiError(
                    "invalid_request", f"can't change {unknown}. Editable: {sorted(PATCHABLE)} (a name can't be changed)", 422)
            if "is_only_me" in patch and not isinstance(patch["is_only_me"], bool):
                raise ApiError("invalid_request",
                               "is_only_me must be true or false", 422)
            only_me = patch.get("is_only_me", entry.is_only_me)

            definition = set(patch) - {"is_only_me"}
            if entry.source == "yaml" and definition:
                raise ApiError(
                    "managed_by_yaml",
                    "this built-in config is defined by a YAML file: edit the file and restart (only is_only_me can be changed here)",
                    409,
                )
            if not definition:
                registry.update_config_row(entry.id, is_only_me=only_me)
                entry.is_only_me, entry.updated_at = only_me, _now()
                return entry, None

            candidate = normalize(entry.stored)
            for key in ("seed", "update_schedule", "failure_injection"):
                if key in patch:
                    if not isinstance(patch[key], dict):
                        raise ApiError("invalid_request",
                                       f"{key} must be an object", 422)
                    candidate[key] = {**candidate.get(key, {}), **patch[key]}
            if "fields" in patch:
                candidate["fields"] = patch["fields"]
            others = {e.name: e.stored for e in self.scope_entries(
                entry) if e is not entry}
            stored, _ = validate(candidate, others, limits,
                                 forced_name=entry.name)

            configs, tables = self.scope(entry)
            name_map = {e.name: e.physical for e in self.scope_entries(entry)}
            runtime = to_runtime(stored, entry.physical,
                                 name_map, limits.max_rows)

            if normalize(stored)["fields"] != normalize(entry.stored)["fields"]:
                seeded = self._replace_fields(
                    entry, stored, runtime, only_me, confirm, configs, tables)
                return entry, seeded

            registry.update_config_row(entry.id, config_json=json.dumps(
                normalize(stored)), is_only_me=only_me)
            entry.stored, entry.cfg, entry.is_only_me, entry.updated_at = stored, runtime, only_me, _now()
            configs[entry.physical] = runtime
            if self.scheduler is not None:
                schedule_entity(self.scheduler, engine,
                                entry.physical, tables, configs)
            return entry, None

    def _replace_fields(self, entry: Entry, stored: EntityConfig, runtime: EntityConfig, only_me: bool,
                        confirm: bool, configs: Dict[str, EntityConfig], tables: Dict[str, Any]) -> int:
        if not confirm:
            raise ApiError(
                "confirmation_required",
                "changing fields deletes this config's data and recreates it; confirm it (confirm=true)", 400,
            )
        deps = self.dependents(entry)
        if deps:
            raise ApiError(
                "has_dependents", f"can't change the fields of '{entry.name}': referenced by {sorted(e.name for e in deps)}", 409)
        if stored.seed.initial_count > 0:  # check before anything is destroyed
            for f in runtime.fields.values():
                if f.type == "ref":
                    parent = next(e for e in self.scope_entries(
                        entry) if e.physical == f.ref_entity)
                    with engine.connect() as conn:
                        if conn.execute(select(parent.table).limit(1)).first() is None:
                            raise ApiError(
                                "missing_parent_rows", f"'{parent.name}' has no rows yet, so '{entry.name}' can't be re-seeded from it", 409)

        old_cfg, old_table = entry.cfg, entry.table
        if not WRITE_LOCK.acquire(timeout=LOCK_TIMEOUT):
            raise ApiError(
                "busy", "another bulk write is in progress; try again shortly", 409)
        try:
            if self.scheduler is not None:
                unschedule_entity(self.scheduler, entry.physical)
            try:
                self._wipe(entry.physical, old_table)
                configs[entry.physical] = runtime
                new_table = models.build_table(runtime, configs)
                tables[entry.physical] = new_table
                new_table.create(engine)
                seeded = self._seed(runtime, new_table,
                                    configs, tables, locked=True)
            except BaseException:
                # Best effort: put the old definition back (empty) so the config still works.
                try:
                    configs[entry.physical] = old_cfg
                    tables[entry.physical] = models.build_table(
                        old_cfg, configs)
                    tables[entry.physical].create(engine, checkfirst=True)
                    entry.table = tables[entry.physical]
                except Exception:
                    logger.exception(
                        "could not restore %s after a failed rebuild", entry.physical)
                if self.scheduler is not None:
                    schedule_entity(self.scheduler, engine,
                                    entry.physical, tables, configs)
                raise
        finally:
            WRITE_LOCK.release()

        registry.update_config_row(entry.id, config_json=json.dumps(
            normalize(stored)), is_only_me=only_me)
        entry.stored, entry.cfg, entry.table, entry.is_only_me, entry.updated_at = stored, runtime, new_table, only_me, _now()
        if self.scheduler is not None:
            schedule_entity(self.scheduler, engine,
                            entry.physical, tables, configs)
        return seeded

    @staticmethod
    def _wipe(physical: str, table: Any) -> None:
        """Drop a config's table and forget its history (change feed + scheduler log)."""
        table.drop(engine, checkfirst=True)
        with engine.begin() as conn:
            conn.execute(delete(models.change_log).where(
                models.change_log.c.entity == physical))
            conn.execute(delete(models.scheduler_runs).where(
                models.scheduler_runs.c.entity == physical))

    # ------------------------------------------------------------- delete ----
    def delete(self, entry: Entry) -> None:
        with self.lock:
            if entry.source != "api":
                raise ApiError(
                    "managed_by_yaml", "built-in configs come from YAML files and can't be deleted through the API", 409)
            deps = self.dependents(entry)
            if deps:
                raise ApiError(
                    "has_dependents",
                    f"'{entry.name}' is referenced by {sorted(e.name for e in deps)}; delete those first", 409,
                )
            if not WRITE_LOCK.acquire(timeout=LOCK_TIMEOUT):
                raise ApiError(
                    "busy", "another bulk write is in progress; try again shortly", 409)
            try:
                if self.scheduler is not None:
                    unschedule_entity(self.scheduler, entry.physical)
                self._wipe(entry.physical, entry.table)
                registry.delete_config_row(entry.id)
            finally:
                WRITE_LOCK.release()
            configs, tables = self.scope(entry)
            configs.pop(entry.physical, None)
            tables.pop(entry.physical, None)
            if entry.physical in models.metadata.tables:
                models.metadata.remove(models.metadata.tables[entry.physical])
            self.entries.pop(entry.id, None)


catalog = Catalog()
