"""Validation for configs that providers submit through the API.

The YAML files in `configs/` are written by the operator, so the loader in
`entities.py` only checks them for shape. A config arriving over HTTP comes from
someone else, and it turns into a real database table and a recurring background
job — so it gets the strict treatment here:

  * names are constrained (they become table, column and file names)
  * unknown keys are rejected (a typo'd `update_schedul` must not be silently ignored)
  * limits apply (fields, rows, latency, schedule size) — see `Limits`
  * the config is trial-generated once, so a bad `key_label` / `ik_options` fails now,
    with a clear message, instead of halfway through seeding
  * the bookkeeping the engine depends on is enforced (a `version` column, a uuid key)

`to_runtime` then maps a validated config onto its namespaced physical table.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple

import yaml
from pydantic import ValidationError

from .entities import (
    EntityConfig,
    FailureInjectionConfig,
    FieldConfig,
    SeedConfig,
    UpdateScheduleConfig,
)
from ..core.errors import ApiError
from ..services.generator import generate_row, generate_update

# Table/column/file names. 32 keeps `idx_d_<10 hex>_<name>_version` under PostgreSQL's
# 63-character identifier limit.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# `schema` / `manifest` are file names inside an export bundle (schema.sql, manifest.json).
RESERVED_NAMES = frozenset({"schema", "manifest"})
TOP_LEVEL_KEYS = frozenset({"entity", "name", "fields", "seed",
                           "update_schedule", "failure_injection", "is_only_me"})
MAX_ENUM_VALUES = 50
MAX_ENUM_VALUE_LENGTH = 64
MAX_KEY_LABEL_LENGTH = 64
MAX_IK_OPTIONS_BYTES = 2000
MAX_NEW_RECORDS = 1000
MAX_JITTER_SECONDS = 3600


@dataclass(frozen=True)
class Limits:
    """Quotas for provider-created configs. Each is an environment variable, so a
    deployment can tighten (a 0.5 GB free database) or loosen them."""

    max_configs: int = 10           # MAX_CONFIGS_PER_PROVIDER
    max_fields: int = 40            # MAX_FIELDS_PER_CONFIG
    max_initial_count: int = 10_000  # MAX_INITIAL_COUNT
    # MAX_ROWS_PER_CONFIG  (total rows, soft-deleted included)
    max_rows: int = 50_000
    max_latency_ms: int = 5_000     # MAX_LATENCY_MS
    max_body_bytes: int = 65_536    # MAX_CONFIG_BYTES

    def as_dict(self) -> Dict[str, int]:
        return asdict(self)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw not in (None, "") else default
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}")
    return value


def get_limits() -> Limits:
    """Read at call time, so changing the environment (or a test) takes effect at once."""
    return Limits(
        max_configs=_env_int("MAX_CONFIGS_PER_PROVIDER", 10),
        max_fields=_env_int("MAX_FIELDS_PER_CONFIG", 40),
        max_initial_count=_env_int("MAX_INITIAL_COUNT", 10_000),
        max_rows=_env_int("MAX_ROWS_PER_CONFIG", 50_000),
        max_latency_ms=_env_int("MAX_LATENCY_MS", 5_000),
        max_body_bytes=_env_int("MAX_CONFIG_BYTES", 65_536),
    )


def config_error(message: str) -> ApiError:
    return ApiError("invalid_config", message, 422)


# --------------------------------------------------------------- parsing ----
def parse_body(raw: bytes, content_type: Optional[str], limits: Limits) -> Dict[str, Any]:
    """Request body -> dict. JSON for `application/json`; anything else is read as
    YAML (which is a superset of JSON), so an existing configs/*.yaml posts as-is."""
    if len(raw) > limits.max_body_bytes:
        raise ApiError("payload_too_large",
                       f"config bodies are limited to {limits.max_body_bytes} bytes", 413)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise config_error("body must be UTF-8 text")
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind == "application/json" or kind.endswith("+json"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise config_error(f"invalid JSON: {exc}")
    else:
        try:
            # Reject anchors/aliases up front: safe_load would happily expand a tiny
            # "billion laughs" document into gigabytes.
            for event in yaml.parse(text, Loader=yaml.SafeLoader):
                if isinstance(event, yaml.AliasEvent):
                    raise config_error(
                        "YAML anchors and aliases are not allowed")
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise config_error(f"invalid YAML: {exc}")
    if not isinstance(data, dict):
        raise config_error(
            "the config must be an object (a mapping of keys to values)")
    return data


def _format_pydantic(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"])
        parts.append(f"{where}: {err['msg']}" if where else err["msg"])
    return "; ".join(parts)


def _reject_unknown_keys(raw: Dict[str, Any]) -> None:
    def check(obj: Any, model: Any, path: str) -> None:
        if isinstance(obj, dict):
            unknown = sorted(set(obj) - set(model.model_fields))
            if unknown:
                raise config_error(
                    f"unknown key(s) in {path}: {unknown}. Allowed: {sorted(model.model_fields)}")

    fields = raw.get("fields")
    if isinstance(fields, dict):
        for fname, fdef in fields.items():
            check(fdef, FieldConfig, f"fields.{fname}")
    check(raw.get("seed"), SeedConfig, "seed")
    check(raw.get("update_schedule"), UpdateScheduleConfig, "update_schedule")
    check(raw.get("failure_injection"),
          FailureInjectionConfig, "failure_injection")


# ------------------------------------------------------------ validation ----
def validate(
    raw: Any,
    existing: Dict[str, EntityConfig],
    limits: Limits,
    *,
    forced_name: Optional[str] = None,
) -> Tuple[EntityConfig, Optional[bool]]:
    """Validate a provider's config. Returns (normalized config, is_only_me or None).

    `existing` is {logical name: config} for the provider's OTHER configs — the only
    things a `ref` field may point at. `forced_name` is used when editing a config
    (its name can't change). Raises ApiError("invalid_config", ..., 422).
    """
    if not isinstance(raw, dict):
        raise config_error(
            "the config must be an object (a mapping of keys to values)")
    raw = dict(raw)

    unknown = sorted(set(raw) - TOP_LEVEL_KEYS)
    if unknown:
        raise config_error(
            f"unknown top-level key(s): {unknown}. Allowed: {sorted(TOP_LEVEL_KEYS)}")

    only_me = raw.pop("is_only_me", None)
    if only_me is not None and not isinstance(only_me, bool):
        raise config_error("is_only_me must be true or false")

    entity, alias = raw.pop("entity", None), raw.pop("name", None)
    if entity is not None and alias is not None and entity != alias:
        raise config_error(
            "'entity' and 'name' are the same thing; give one, or give them the same value")
    name = entity if entity is not None else alias
    if forced_name is not None:
        if name is not None and name != forced_name:
            raise config_error(
                f"a config's name can't be changed (it is '{forced_name}'); delete and recreate it instead")
        name = forced_name
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise config_error(
            "name must be 1-32 characters: a lowercase letter, then lowercase letters, digits or underscores")
    if name in RESERVED_NAMES:
        raise config_error(
            f"'{name}' is reserved (it is a file name inside export bundles); pick another name")

    fields_raw = raw.get("fields")
    if not isinstance(fields_raw, dict) or not fields_raw:
        raise config_error(
            "fields must be a non-empty mapping of field name to definition")
    _reject_unknown_keys(raw)

    try:
        cfg = EntityConfig(entity=name, **raw)
    except ValidationError as exc:
        raise config_error(_format_pydantic(exc))
    except TypeError as exc:
        raise config_error(str(exc))

    fields = cfg.fields

    # -- the bookkeeping the engine relies on -------------------------------------------
    for fname, f in fields.items():
        if f.auto == "version" and fname != "version":
            raise config_error(
                f"field '{fname}': the version counter must be named 'version'")
    if "version" in fields:
        v = fields["version"]
        if v.type != "int" or v.auto != "version":
            raise config_error(
                "field 'version' is reserved for the change-feed counter: use type: int, auto: version")
    else:
        # added for you; it is required
        fields["version"] = FieldConfig(type="int", auto="version")

    if len(fields) > limits.max_fields:
        raise config_error(
            f"too many fields ({len(fields)}); the limit is {limits.max_fields}")

    pks = [n for n, f in fields.items() if f.primary_key]
    if len(pks) != 1:
        raise config_error("exactly one field must have primary_key: true")
    if fields[pks[0]].type != "uuid" or fields[pks[0]].auto:
        raise config_error(
            f"primary key '{pks[0]}' must be type: uuid (with no 'auto')")

    soft = [n for n, f in fields.items() if f.auto == "soft_delete"]
    if len(soft) > 1:
        raise config_error(
            f"only one soft_delete field is allowed, found {soft}")

    for fname, f in fields.items():
        if not FIELD_RE.match(fname):
            raise config_error(
                f"field name '{fname}' must be lowercase letters, digits and underscores, starting with a letter (max 40)")
        if f.auto in ("created", "updated", "soft_delete") and f.type != "timestamp":
            raise config_error(
                f"field '{fname}': auto: {f.auto} needs type: timestamp")
        if f.auto == "soft_delete":
            f.nullable = True  # a live row has no deletion time
        if f.type == "enum":
            vals = f.values
            if (not vals or len(vals) > MAX_ENUM_VALUES or len(set(vals)) != len(vals)
                    or any(not isinstance(x, str) or not x or len(x) > MAX_ENUM_VALUE_LENGTH for x in vals)):
                raise config_error(
                    f"field '{fname}': an enum needs 1-{MAX_ENUM_VALUES} distinct, non-empty string values "
                    f"(each up to {MAX_ENUM_VALUE_LENGTH} characters)"
                )
        elif f.values is not None:
            raise config_error(
                f"field '{fname}': 'values' only applies to type: enum")
        if f.type == "ref":
            if f.primary_key:
                raise config_error(
                    f"field '{fname}': a primary key can't be a ref")
            if not f.ref_entity:
                raise config_error(
                    f"field '{fname}' is type 'ref' but has no ref_entity")
            if f.ref_entity == name:
                raise config_error(
                    f"field '{fname}': a config can't reference itself")
            if f.ref_entity not in existing:
                raise config_error(
                    f"field '{fname}' references '{f.ref_entity}', which is not one of your configs "
                    f"(yours: {sorted(existing) or 'none yet'}). Create the parent first; refs can only point at your own configs."
                )
        elif f.ref_entity is not None:
            raise config_error(
                f"field '{fname}': 'ref_entity' only applies to type: ref")
        if f.min is not None and f.max is not None and f.min > f.max:
            raise config_error(
                f"field '{fname}': min ({f.min}) is greater than max ({f.max})")
        if f.key_label is not None:
            if not isinstance(f.key_label, str) or not f.key_label or len(f.key_label) > MAX_KEY_LABEL_LENGTH:
                raise config_error(
                    f"field '{fname}': key_label must be a provider name of up to {MAX_KEY_LABEL_LENGTH} characters")
        if f.ik_options is not None:
            if f.key_label is None:
                raise config_error(
                    f"field '{fname}': ik_options needs a key_label")
            if len(json.dumps(f.ik_options, default=str)) > MAX_IK_OPTIONS_BYTES:
                raise config_error(
                    f"field '{fname}': ik_options is too large (limit {MAX_IK_OPTIONS_BYTES} bytes)")

    _check_no_cycle(name, fields, existing)

    # -- sizes and rates -----------------------------------------------------------------
    if not 0 <= cfg.seed.initial_count <= limits.max_initial_count:
        raise config_error(
            f"seed.initial_count must be between 0 and {limits.max_initial_count}")
    if cfg.seed.initial_count > limits.max_rows:
        raise config_error(
            f"seed.initial_count can't exceed the per-config row limit ({limits.max_rows})")
    sched = cfg.update_schedule
    lo, hi = sched.new_records
    if not (0 <= lo <= hi <= MAX_NEW_RECORDS):
        raise config_error(
            f"update_schedule.new_records must be a [low, high] pair within 0-{MAX_NEW_RECORDS}")
    for label, pct in (("mutate_existing_pct", sched.mutate_existing_pct), ("soft_delete_pct", sched.soft_delete_pct)):
        if not 0 <= pct <= 100:
            raise config_error(
                f"update_schedule.{label} must be between 0 and 100")
    if not 0 <= sched.jitter_seconds <= MAX_JITTER_SECONDS:
        raise config_error(
            f"update_schedule.jitter_seconds must be between 0 and {MAX_JITTER_SECONDS}")
    fail = cfg.failure_injection
    if not 0.0 <= fail.fail_rate <= 1.0:
        raise config_error(
            "failure_injection.fail_rate must be between 0.0 and 1.0")
    if not 0 <= fail.latency_ms <= limits.max_latency_ms:
        raise config_error(
            f"failure_injection.latency_ms must be between 0 and {limits.max_latency_ms}")

    # -- does it actually generate? -----------------------------------------------------
    sample_refs = {f.ref_entity: ["sample"]
                   for f in fields.values() if f.type == "ref"}
    try:
        generate_row(cfg, sample_refs)
        generate_update(cfg)
    except Exception as exc:  # e.g. an unknown iki key_label, or a bad ik_options
        raise config_error(f"this config can't generate data: {exc}")

    return cfg, only_me


def _check_no_cycle(name: str, fields: Dict[str, FieldConfig], existing: Dict[str, EntityConfig]) -> None:
    """Refs must form a DAG. A new config can only point at existing ones, so a cycle can
    only appear when an EDIT makes a parent point back at (a descendant of) itself."""
    seen: set = set()
    stack = [f.ref_entity for f in fields.values() if f.type ==
             "ref" and f.ref_entity]
    while stack:
        current = stack.pop()
        if current == name:
            raise config_error(
                f"'{name}' would reference itself through a chain of refs (circular dependency)")
        if current in seen or current not in existing:
            continue
        seen.add(current)
        stack.extend(f.ref_entity for f in existing[current].fields.values(
        ) if f.type == "ref" and f.ref_entity)


# --------------------------------------------------------------- helpers ----
def normalize(cfg: EntityConfig) -> Dict[str, Any]:
    """The stored / returned form of a config: plain JSON, the provider's own names."""
    return cfg.model_dump(mode="json", exclude={"max_rows"}, exclude_none=True)


def to_runtime(
    stored: EntityConfig, physical: str, name_map: Dict[str, str], max_rows: Optional[int] = None
) -> EntityConfig:
    """The config the engine runs: `entity` and every `ref_entity` become namespaced
    physical table names, so two providers' `orders` never meet in one database."""
    cfg = stored.model_copy(deep=True)
    cfg.entity = physical
    for f in cfg.fields.values():
        if f.type == "ref":
            f.ref_entity = name_map[f.ref_entity]
    cfg.max_rows = max_rows
    return cfg


def physical_name(config_id: str, name: str) -> str:
    """`cfg_8f3a91c2d4e5f607` + `orders` -> `d_8f3a91c2d4_orders` (≤ 45 characters)."""
    return f"d_{config_id.split('_', 1)[1][:10]}_{name}"
