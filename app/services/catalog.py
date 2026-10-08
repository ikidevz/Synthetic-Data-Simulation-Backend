"""The live catalog: every project, the configs (tables) inside it, each config's
background job — and the rules for who may touch them.

The model
---------
  provider  ->  project (a schema)  ->  config (a table)

A *project* is a named group of configs that belong together, the way tables belong to a
schema. A config's `ref` fields can only point at configs of the same project, bulk
operations (`replace`, `reset`, which also refresh dependents) can only reach tables of
the same project, and a whole project exports as one schema (see /v1/projects/{id}/ddl
and /export).

Where things live
-----------------
  registry_projects (database)  each project, plus its `is_only_me`
  registry_configs (database)   the definition each config was published with
  Catalog.projects / .entries   the same in memory, with the runtime config and Table object
  one physical table per config `d_<10 hex>_<name>` for API configs, the plain entity
                                name for the built-in YAML examples

Namespacing is what lets the existing engine (change feed, scheduler, batch, export)
serve many projects unchanged: `change_log` and `scheduler_runs` already key on the
entity name, and a config's physical table name never collides with anyone else's. Each
project gets its own `(configs, tables)` *scope*.

The built-in YAML examples form one read-only project, `examples`, owned by the system
provider: its configs come from the files in `configs/`, so the API can't add to it,
rename it or delete it (it can only be made private).

Who may do what
---------------
  read   the owner, the superuser, and — unless the project is `is_only_me` — any other provider
  write  the owner and the superuser only
  A project marked `is_only_me` is invisible to everyone else, and so are its configs:
  404, not 403, so its existence isn't revealed. Visibility belongs to the project, never
  to a single config, so a shared config can't point at a private parent.

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
from ..config.provider import (
    Limits, get_limits, normalize, physical_name, to_runtime, validate, validate_project,
)
from ..core.errors import ApiError
from ..db import models
from ..db.engine import engine
from ..security.api_keys import Principal
from . import registry
from .batch import DEFAULT_BATCH_SIZE, _deps, _generate, _load_ids
from .scheduler import WRITE_LOCK, schedule_entity, unschedule_entity

logger = logging.getLogger("catalog")

# The built-in examples are the system project; its scope is the legacy CONFIGS / TABLES.
SYSTEM_PROJECT_ID = registry.SYSTEM_PROJECT_ID
PATCHABLE = frozenset(
    {"seed", "update_schedule", "failure_injection", "fields"})
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
class Project:
    id: str
    provider_id: str
    name: str
    description: Optional[str]
    is_only_me: bool
    source: str          # "api" | "yaml" (the built-in examples project)
    created_at: Any
    updated_at: Any


@dataclass
class Entry:
    id: str
    provider_id: str
    project_id: str
    name: str            # the provider's own name for it
    physical: str        # the table it lives in
    source: str          # "api" | "yaml"
    stored: EntityConfig  # as the provider wrote it (logical names)
    cfg: EntityConfig     # as the engine runs it (physical names)
    table: Any
    created_at: Any
    updated_at: Any


class Catalog:
    def __init__(self) -> None:
        # guards the structures below and config create/edit/delete
        self.lock = threading.RLock()
        self.projects: Dict[str, Project] = {}
        self.entries: Dict[str, Entry] = {}
        self.scopes: Dict[str, Scope] = {}
        self.scheduler: Any = None

    # ------------------------------------------------------------ loading ----
    def load(self, system_configs: Dict[str, EntityConfig], system_tables: Dict[str, Any]) -> None:
        """Rebuild the catalog at startup: sync the YAML examples into the registry, then
        load every project and its configs (building each Table object)."""
        with self.lock:
            self.projects.clear()
            self.entries.clear()
            self.scopes.clear()
            registry.ensure_system_provider()
            registry.ensure_system_project()
            self.scopes[SYSTEM_PROJECT_ID] = (system_configs, system_tables)

            now = _now()
            registry.sync_yaml_rows([
                {
                    "id": f"cfg_sys_{name}", "provider_id": registry.SYSTEM_PROVIDER_ID,
                    "project_id": SYSTEM_PROJECT_ID, "name": name, "physical_name": name,
                    "config_json": json.dumps(normalize(cfg)), "source": "yaml",
                    "created_at": now, "updated_at": now,
                }
                for name, cfg in system_configs.items()
            ])

            for prow in registry.list_project_rows():
                self.projects[prow["id"]] = Project(**prow)

            by_project: Dict[str, List[Dict[str, Any]]] = {}
            for row in registry.list_config_rows():
                if row["project_id"] not in self.projects:
                    logger.error("skipping config %s (%s): its project %s doesn't exist",
                                 row["id"], row["name"], row["project_id"])
                    continue
                if row["source"] == "yaml":
                    cfg = system_configs.get(row["name"])
                    if cfg is not None:
                        self.entries[row["id"]] = self._entry(
                            row, cfg, cfg, system_tables[row["name"]])
                else:
                    by_project.setdefault(row["project_id"], []).append(row)
            for project_id, rows in by_project.items():
                self._load_project(project_id, rows)

    @staticmethod
    def _entry(row: Dict[str, Any], stored: EntityConfig, cfg: EntityConfig, table: Any) -> Entry:
        return Entry(
            id=row["id"], provider_id=row["provider_id"], project_id=row["project_id"], name=row["name"],
            physical=row["physical_name"], source=row["source"], stored=stored, cfg=cfg, table=table,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def _load_project(self, project_id: str, rows: List[Dict[str, Any]]) -> None:
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
        configs, tables = self._scope_dicts(project_id)
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
        return entry.project_id

    def _scope_dicts(self, key: str) -> Scope:
        if key not in self.scopes:
            self.scopes[key] = ({}, {})
        return self.scopes[key]

    def scope(self, entry: Entry) -> Scope:
        return self._scope_dicts(self.scope_key(entry))

    def project_scope(self, project_id: str) -> Scope:
        """The (configs, tables) dicts for one project: all the engine is ever handed."""
        return self._scope_dicts(project_id)

    def project_entries(self, project_id: str) -> List[Entry]:
        """Every config in a project, in creation order."""
        return [e for e in list(self.entries.values()) if e.project_id == project_id]

    def scope_entries(self, entry: Entry) -> List[Entry]:
        return self.project_entries(entry.project_id)

    def project_of(self, entry: Entry) -> Project:
        return self.projects[entry.project_id]

    def labels(self, entry: Entry) -> Dict[str, str]:
        """{physical table name: the provider's name} for everything in this entry's project."""
        return {e.physical: e.name for e in self.scope_entries(entry)}

    def project_labels(self, project_id: str) -> Dict[str, str]:
        return {e.physical: e.name for e in self.project_entries(project_id)}

    def dependents(self, entry: Entry) -> List[Entry]:
        """Configs (same project) with a ref field pointing at this one."""
        return [
            e for e in self.scope_entries(entry)
            if e is not entry and any(f.type == "ref" and f.ref_entity == entry.name for f in e.stored.fields.values())
        ]

    # ------------------------------------------------------------- access ----
    @staticmethod
    def can_write(principal: Principal, thing: Any) -> bool:
        """`thing` is a Project or an Entry: both carry the owning provider's id."""
        return principal.is_superuser or principal.provider_id == thing.provider_id

    def is_private(self, thing: Any) -> bool:
        """Visibility is the project's: a config is private exactly when its project is."""
        project = thing if isinstance(
            thing, Project) else self.projects.get(thing.project_id)
        return True if project is None else project.is_only_me

    def can_read(self, principal: Principal, thing: Any) -> bool:
        return self.can_write(principal, thing) or not self.is_private(thing)

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

    def resolve_project(self, project_id: str, principal: Principal, need: str = "read") -> Project:
        """The project, if `principal` may use it for `need`. A private project is a 404."""
        project = self.projects.get(project_id)
        if project is None or not self.can_read(principal, project):
            raise ApiError(
                "not_found", f"project '{project_id}' not found", 404)
        if need == "write" and not self.can_write(principal, project):
            raise ApiError(
                "forbidden",
                f"project '{project_id}' belongs to {project.provider_id}: you can read it, "
                f"but only its owner can change it", 403)
        return project

    def _own(self, principal: Principal, thing: Any) -> bool:
        return thing.provider_id == (
            registry.SYSTEM_PROVIDER_ID if principal.is_superuser else principal.provider_id)

    def visible(self, principal: Principal, scope: str = "all", project_id: Optional[str] = None) -> List[Entry]:
        """Every config `principal` may read. `mine` = their own; `shared` = other providers'
        public ones. (For the superuser, "mine" means the built-in system-owned ones.)
        `project_id` narrows it to one project."""
        out = []
        for entry in list(self.entries.values()):
            if project_id is not None and entry.project_id != project_id:
                continue
            if not self.can_read(principal, entry):
                continue
            own = self._own(principal, entry)
            if (scope == "mine" and not own) or (scope == "shared" and own):
                continue
            out.append(entry)
        return sorted(out, key=lambda e: (e.name, e.id))

    def visible_projects(self, principal: Principal, scope: str = "all") -> List[Project]:
        out = []
        for project in list(self.projects.values()):
            if not self.can_read(principal, project):
                continue
            own = self._own(principal, project)
            if (scope == "mine" and not own) or (scope == "shared" and own):
                continue
            out.append(project)
        return sorted(out, key=lambda p: (p.name, p.id))

    def describe(
        self, entry: Entry, principal: Principal, owners: Optional[Dict[str, str]] = None, detail: bool = False
    ) -> Dict[str, Any]:
        owners = owners if owners is not None else registry.provider_names()
        project = self.projects.get(entry.project_id)
        out: Dict[str, Any] = {
            "id": entry.id,
            "name": entry.name,
            "project": {"id": entry.project_id, "name": project.name if project else None,
                        "is_only_me": bool(project and project.is_only_me)},
            "owner": {"id": entry.provider_id, "full_name": owners.get(entry.provider_id)},
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

    def describe_project(
        self, project: Project, principal: Principal, owners: Optional[Dict[str, str]] = None, detail: bool = False
    ) -> Dict[str, Any]:
        owners = owners if owners is not None else registry.provider_names()
        members = self.project_entries(project.id)
        out: Dict[str, Any] = {
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "owner": {"id": project.provider_id, "full_name": owners.get(project.provider_id)},
            "is_only_me": project.is_only_me,
            "source": project.source,
            "can_write": self.can_write(principal, project),
            "config_count": len(members),
            "created_at": project.created_at.isoformat() if project.created_at else None,
            "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        }
        if detail:
            out["configs"] = [{"id": e.id, "name": e.name, "cadence": e.stored.update_schedule.cadence}
                              for e in sorted(members, key=lambda e: (e.name, e.id))]
        return out

    # ----------------------------------------------------------- projects ----
    def create_project(self, owner_id: str, raw: Any, limits: Limits) -> Project:
        """Create an empty project (a schema with no tables yet) for `owner_id`."""
        with self.lock:
            if registry.get_provider(owner_id) is None:
                raise ApiError(
                    "not_found", f"provider '{owner_id}' not found", 404)
            values = validate_project(raw)
            mine = [p for p in self.projects.values()
                    if p.provider_id == owner_id and p.source == "api"]
            if len(mine) >= limits.max_projects:
                raise ApiError(
                    "quota_exceeded",
                    f"a provider can hold at most {limits.max_projects} projects; delete one first", 409)
            if values["name"] in {p.name for p in self.projects.values() if p.provider_id == owner_id}:
                raise ApiError("already_exists",
                               f"you already have a project named '{values['name']}'", 409)
            now = _now()
            row = {"id": registry.new_id("prj"), "provider_id": owner_id, "name": values["name"],
                   "description": values.get("description"),
                   "is_only_me": bool(values.get("is_only_me", False)), "source": "api",
                   "created_at": now, "updated_at": now}
            registry.insert_project_row(row)
            project = Project(**row)
            self.projects[project.id] = project
            self._scope_dicts(project.id)
            return project

    def update_project(self, project: Project, patch: Any) -> Project:
        """Rename a project, change its description, or share / unshare it."""
        with self.lock:
            values = validate_project(patch, patch=True)
            if project.source == "yaml" and set(values) - {"is_only_me"}:
                raise ApiError(
                    "managed_by_yaml",
                    "the built-in examples project is defined by the files in configs/ "
                    "(only is_only_me can be changed here)", 409)
            if "name" in values and values["name"] != project.name:
                clash = [p for p in self.projects.values()
                         if p.provider_id == project.provider_id and p.id != project.id
                         and p.name == values["name"]]
                if clash:
                    raise ApiError("already_exists",
                                   f"you already have a project named '{values['name']}'", 409)
            registry.update_project_row(project.id, **values)
            for key, value in values.items():
                setattr(project, key, value)
            project.updated_at = _now()
            return project

    def delete_project(self, project: Project) -> int:
        """Delete a project, every config in it and ALL their data. Returns how many configs went."""
        with self.lock:
            if project.source == "yaml":
                raise ApiError(
                    "managed_by_yaml",
                    "the built-in examples project comes from the files in configs/ and can't be deleted "
                    "through the API", 409)
            removed = 0
            # children before parents, so no config is ever deleted while another still refs it
            while True:
                remaining = self.project_entries(project.id)
                if not remaining:
                    break
                leaf = next(
                    (e for e in remaining if not self.dependents(e)), None)
                # unreachable (refs form a DAG); never loop forever
                if leaf is None:
                    raise ApiError(
                        "conflict", "can't order this project's configs for deletion", 409)
                self.delete(leaf)
                removed += 1
            registry.delete_project_row(project.id)
            self.projects.pop(project.id, None)
            self.scopes.pop(project.id, None)
            return removed

    # ------------------------------------------------------------- create ----
    def create(self, project: Project, raw: Any, limits: Limits) -> Tuple[Entry, int]:
        """Validate a config, create its table inside `project`, seed it and schedule its job.
        Returns (entry, rows seeded). Everything is undone if any step fails."""
        with self.lock:
            if project.source == "yaml":
                raise ApiError(
                    "managed_by_yaml",
                    "the built-in examples project is defined by the files in configs/: "
                    "create your own project to publish configs", 409)
            if project.id not in self.projects:
                raise ApiError(
                    "not_found", f"project '{project.id}' not found", 404)
            owner_id = project.provider_id
            members = self.project_entries(project.id)
            if len(members) >= limits.max_configs_per_project:
                raise ApiError(
                    "quota_exceeded",
                    f"a project can hold at most {limits.max_configs_per_project} configs; delete one first", 409)
            api_owned = [e for e in list(self.entries.values())
                         if e.provider_id == owner_id and e.source == "api"]
            if len(api_owned) >= limits.max_configs:
                raise ApiError(
                    "quota_exceeded",
                    f"a provider can hold at most {limits.max_configs} configs across all its projects; "
                    f"delete one first", 409,
                )
            stored = validate(raw, {e.name: e.stored for e in members}, limits)
            if stored.entity in {e.name for e in members}:
                raise ApiError(
                    "already_exists",
                    f"project '{project.name}' already has a config named '{stored.entity}'", 409)

            config_id, physical = self._new_ids(stored.entity)
            configs, tables = self._scope_dicts(project.id)
            name_map = {e.name: e.physical for e in members}
            name_map[stored.entity] = physical
            labels = {v: k for k, v in name_map.items()}
            runtime = to_runtime(stored, physical, name_map, limits.max_rows)

            now = _now()
            registry.insert_config_row({
                "id": config_id, "provider_id": owner_id, "project_id": project.id,
                "name": stored.entity, "physical_name": physical,
                "config_json": json.dumps(normalize(stored)), "source": "api",
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
                id=config_id, provider_id=owner_id, project_id=project.id, name=stored.entity,
                physical=physical, source="api", stored=stored, cfg=runtime, table=table,
                created_at=now, updated_at=now,
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
                hint = (" is_only_me belongs to the project: PATCH /v1/projects/{project_id}."
                        if "is_only_me" in unknown else "")
                raise ApiError(
                    "invalid_request",
                    f"can't change {unknown}. Editable: {sorted(PATCHABLE)} (a name can't be changed).{hint}", 422)
            if entry.source == "yaml":
                raise ApiError(
                    "managed_by_yaml",
                    "this built-in config is defined by a YAML file: edit the file and restart",
                    409,
                )

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
            stored = validate(candidate, others, limits,
                              forced_name=entry.name)

            configs, tables = self.scope(entry)
            name_map = {e.name: e.physical for e in self.scope_entries(entry)}
            runtime = to_runtime(stored, entry.physical,
                                 name_map, limits.max_rows)

            if normalize(stored)["fields"] != normalize(entry.stored)["fields"]:
                seeded = self._replace_fields(
                    entry, stored, runtime, confirm, configs, tables)
                return entry, seeded

            registry.update_config_row(entry.id, config_json=json.dumps(
                normalize(stored)))
            entry.stored, entry.cfg, entry.updated_at = stored, runtime, _now()
            configs[entry.physical] = runtime
            if self.scheduler is not None:
                schedule_entity(self.scheduler, engine,
                                entry.physical, tables, configs)
            return entry, None

    def _replace_fields(self, entry: Entry, stored: EntityConfig, runtime: EntityConfig,
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
            normalize(stored)))
        entry.stored, entry.cfg, entry.table, entry.updated_at = stored, runtime, new_table, _now()
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
