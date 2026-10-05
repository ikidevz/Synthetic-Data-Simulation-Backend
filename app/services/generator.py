"""Synthetic value generation for each supported field type.

Every generator takes an optional `rng` (a `random.Random` instance, or the
`random` module itself). Passing a seeded `random.Random` makes the output
reproducible — including uuids, which are built from the rng rather than
`uuid.uuid4()`. Timestamps always use the wall clock, so they are the one
thing a seed does not pin down.

`generate_row` deliberately skips 'version' fields — the correct next
version number depends on what else already exists in the table, which
only the caller (holding the DB connection/transaction) knows.
"""
from __future__ import annotations

import random
import string
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from ikidatagen import IkiDataGenerator

from ..config.entities import EntityConfig, FieldConfig


class MissingReferenceError(ValueError):
    """A 'ref' field has no rows in its target entity to point at."""


def _random_string(rng: Any, length: int = 8) -> str:
    return "".join(rng.choices(string.ascii_lowercase, k=length))


def generate_field_value(field: FieldConfig, rng: Any = random) -> Any:
    """Generate a synthetic value for one field. Does not handle 'ref' or
    'auto' fields — those need extra context the caller supplies.

    If field.key_label is set, generation is handed off to iki-data-generator
    instead of the type-based branches below. A seed is derived from `rng` so
    a batch's overall --seed still makes the whole row reproducible, even
    though Iki does its own seeding internally rather than taking an rng.
    """
    if field.key_label:
        seed = rng.randint(0, 2**31 - 1)
        schema = [{"key_label": field.key_label,
                   "options": field.ik_options or {}}]
        return IkiDataGenerator(schema, seed=seed).one()[field.key_label]
    if field.type == "uuid":
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))
    if field.type == "int":
        lo = int(field.min) if field.min is not None else 0
        hi = int(field.max) if field.max is not None else 1000
        return rng.randint(lo, hi)
    if field.type == "float":
        lo = field.min if field.min is not None else 0.0
        hi = field.max if field.max is not None else 1000.0
        return round(rng.uniform(lo, hi), 2)
    if field.type == "string":
        return _random_string(rng)
    if field.type == "enum":
        if not field.values:
            raise ValueError("enum field requires a 'values' list")
        return rng.choice(field.values)
    if field.type == "bool":
        return rng.choice([True, False])
    if field.type == "timestamp":
        return datetime.now(timezone.utc)
    raise ValueError(
        f"generate_field_value cannot handle type '{field.type}' directly")


def generate_row(
    cfg: EntityConfig, existing_ids: Dict[str, List[Any]], rng: Any = random
) -> Dict[str, Any]:
    """Build one synthetic row for an entity.

    existing_ids: {entity_name: [primary key values]} — used to resolve
    'ref' fields to a real, already-seeded row of the referenced entity.
    Raises MissingReferenceError if a 'ref' field's target entity has no
    rows yet — entities must be generated in dependency order.
    """
    now = datetime.now(timezone.utc)
    row: Dict[str, Any] = {}

    for name, field in cfg.fields.items():
        if field.auto == "version":
            continue  # caller assigns the correct next version
        elif field.auto in ("created", "updated"):
            row[name] = now
        elif field.auto == "soft_delete":
            row[name] = None
        elif field.type == "ref":
            candidates = existing_ids.get(field.ref_entity, [])
            if not candidates:
                raise MissingReferenceError(
                    f"Cannot generate '{cfg.entity}.{name}': no rows exist yet "
                    f"for referenced entity '{field.ref_entity}'. Generate "
                    f"'{field.ref_entity}' first."
                )
            row[name] = rng.choice(candidates)
        else:
            row[name] = generate_field_value(field, rng)

    return row


def mutable_fields(cfg: EntityConfig) -> List[tuple]:
    """Fields a real-world edit would touch: everything except the primary key,
    relationships ('ref') and bookkeeping ('auto': created/updated/soft_delete/version)."""
    return [
        (name, f)
        for name, f in cfg.fields.items()
        if not f.primary_key and not f.auto and f.type != "ref"
    ]


def generate_update(cfg: EntityConfig, rng: Any = random) -> Dict[str, Any]:
    """New synthetic values for a row's mutable fields. An update rewrites all of
    them, so the row visibly changes in the change feed and in exports."""
    return {name: generate_field_value(f, rng) for name, f in mutable_fields(cfg)}
