"""Loads entity configuration files (YAML) into typed EntityConfig objects.

Adding a new entity to the system means adding a new *.yaml file here —
no Python code changes required. Invalid configs (bad types, dangling
'ref' relationships) fail loudly at load time, not at request time.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, field_validator

SUPPORTED_TYPES = {"uuid", "int", "float",
                   "string", "enum", "bool", "timestamp", "ref"}
SUPPORTED_AUTO = {"created", "updated", "soft_delete", "version"}
SUPPORTED_CADENCE = {"hourly", "daily", "weekly"}


class FieldConfig(BaseModel):
    type: str
    primary_key: bool = False
    nullable: bool = False
    min: Optional[float] = None
    max: Optional[float] = None
    values: Optional[List[str]] = None
    ref_entity: Optional[str] = None
    auto: Optional[str] = None  # created | updated | soft_delete | version
    key_label: Optional[str] = None
    ik_options: Optional[dict] = None

    @field_validator("type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in SUPPORTED_TYPES:
            raise ValueError(
                f"Unsupported field type '{v}'. Supported: {sorted(SUPPORTED_TYPES)}")
        return v

    @field_validator("auto")
    @classmethod
    def _check_auto(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in SUPPORTED_AUTO:
            raise ValueError(
                f"Unsupported 'auto' value '{v}'. Supported: {sorted(SUPPORTED_AUTO)}")
        return v


class UpdateScheduleConfig(BaseModel):
    cadence: str = "hourly"
    new_records: List[int] = [5, 15]
    mutate_existing_pct: float = 5.0
    soft_delete_pct: float = 1.0
    jitter_seconds: int = 0

    @field_validator("cadence")
    @classmethod
    def _check_cadence(cls, v: str) -> str:
        if v not in SUPPORTED_CADENCE:
            raise ValueError(
                f"Unsupported cadence '{v}'. Supported: {sorted(SUPPORTED_CADENCE)}")
        return v

    @field_validator("new_records")
    @classmethod
    def _check_range(cls, v: List[int]) -> List[int]:
        if len(v) != 2 or v[0] > v[1]:
            raise ValueError(
                "new_records must be a [low, high] pair with low <= high")
        return v


class FailureInjectionConfig(BaseModel):
    fail_rate: float = 0.0
    latency_ms: int = 0
    drift: bool = False


class SeedConfig(BaseModel):
    initial_count: int = 50


class EntityConfig(BaseModel):
    entity: str
    fields: Dict[str, FieldConfig]
    seed: SeedConfig = SeedConfig()
    update_schedule: UpdateScheduleConfig = UpdateScheduleConfig()
    failure_injection: FailureInjectionConfig = FailureInjectionConfig()
    max_rows: Optional[int] = None

    @property
    def primary_key_field(self) -> str:
        for name, f in self.fields.items():
            if f.primary_key:
                return name
        raise ValueError(
            f"Entity '{self.entity}' has no field with primary_key: true")

    @property
    def soft_delete_field(self) -> Optional[str]:
        for name, f in self.fields.items():
            if f.auto == "soft_delete":
                return name
        return None

    @property
    def version_field(self) -> Optional[str]:
        for name, f in self.fields.items():
            if f.auto == "version":
                return name
        return None


def load_entity_configs(config_dir: str) -> Dict[str, EntityConfig]:
    """Read every *.yaml/*.yml file in config_dir and return {entity_name: EntityConfig}."""
    configs: Dict[str, EntityConfig] = {}
    if not os.path.isdir(config_dir):
        raise FileNotFoundError(f"Config directory not found: {config_dir}")

    for filename in sorted(os.listdir(config_dir)):
        if not (filename.endswith(".yaml") or filename.endswith(".yml")):
            continue
        path = os.path.join(config_dir, filename)
        with open(path, "r") as fh:
            raw = yaml.safe_load(fh)
        if not raw:
            continue
        try:
            cfg = EntityConfig(**raw)
        except Exception as exc:
            raise ValueError(
                f"Invalid entity config in '{filename}': {exc}") from exc

        try:
            cfg.primary_key_field
        except ValueError as exc:
            raise ValueError(
                f"Invalid entity config in '{filename}': {exc}") from exc

        configs[cfg.entity] = cfg

    _validate_refs(configs)
    return configs


def _validate_refs(configs: Dict[str, EntityConfig]) -> None:
    """Fail loudly at load time if a 'ref' field points at an unknown entity,
    or if 'ref' entities form a cycle (which would make seeding impossible)."""
    for name, cfg in configs.items():
        for field_name, field in cfg.fields.items():
            if field.type == "ref":
                if not field.ref_entity:
                    raise ValueError(
                        f"{name}.{field_name} is type 'ref' but has no ref_entity set")
                if field.ref_entity not in configs:
                    raise ValueError(
                        f"{name}.{field_name} references unknown entity '{field.ref_entity}'. "
                        f"Known entities: {sorted(configs)}"
                    )

    # detect cycles via simple dependency-resolution simulation
    remaining = set(configs)
    resolved: set = set()
    while remaining:
        progressed = False
        for name in list(remaining):
            deps = {
                f.ref_entity for f in configs[name].fields.values() if f.type == "ref"
            }
            if deps <= resolved:
                resolved.add(name)
                remaining.discard(name)
                progressed = True
        if not progressed:
            raise ValueError(
                f"Circular 'ref' dependency detected among entities: {sorted(remaining)}"
            )


# <project root>/app/config/entities.py -> <project root>/configs. Resolved from
# __file__ rather than the cwd, so it holds no matter where the server is started.
_DEFAULT_CONFIG_DIR = str(
    Path(__file__).resolve().parents[2] / "configs"
)


def get_config_dir() -> str:
    """Where entity YAML files live: $CONFIG_DIR, or ./configs next to the app."""
    return os.environ.get("CONFIG_DIR", _DEFAULT_CONFIG_DIR)
