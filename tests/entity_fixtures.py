"""Entity configs owned by the TEST SUITE.

The files in the repo's `configs/` directory are examples for people reading the project:
nothing in `tests/` loads, reads or asserts on them, so editing an example can never break
a test (and a test can never silently depend on one). Tests that need the built-in "legacy"
entities (the per-entity routes, /admin/batch, the CLI) get them from here instead:
conftest writes these dicts to a temporary directory and points CONFIG_DIR at it before
the app is imported.

The shape mirrors what the legacy-route tests assert: a 3-level chain
(customers <- orders <- support_tickets), mixed cadences, int/bool fields.
"""
from __future__ import annotations

import os
from typing import Any, Dict

import yaml

_TIMESTAMPS = {
    "created_at": {"type": "timestamp", "auto": "created"},
    "updated_at": {"type": "timestamp", "auto": "updated"},
    "deleted_at": {"type": "timestamp", "nullable": True, "auto": "soft_delete"},
    "version": {"type": "int", "auto": "version"},
}

LEGACY_ENTITIES: Dict[str, Dict[str, Any]] = {
    "customers": {
        "entity": "customers",
        "fields": {
            "customer_id": {"type": "uuid", "primary_key": True},
            "name": {"type": "string", "key_label": "full_name"},
            "email": {"type": "string", "key_label": "email_address"},
            "segment": {"type": "enum", "values": ["retail", "wholesale", "enterprise"]},
            **_TIMESTAMPS,
        },
        "seed": {"initial_count": 30},
        "update_schedule": {"cadence": "hourly", "new_records": [1, 5],
                            "mutate_existing_pct": 5, "soft_delete_pct": 1, "jitter_seconds": 30},
        "failure_injection": {"fail_rate": 0.0, "latency_ms": 0},
    },
    "orders": {
        "entity": "orders",
        "fields": {
            "order_id": {"type": "uuid", "primary_key": True},
            "customer_id": {"type": "ref", "ref_entity": "customers"},
            "amount": {"type": "float", "min": 10, "max": 500},
            "status": {"type": "enum", "values": ["pending", "shipped", "cancelled"]},
            **_TIMESTAMPS,
        },
        "seed": {"initial_count": 50},
        "update_schedule": {"cadence": "hourly", "new_records": [5, 15],
                            "mutate_existing_pct": 5, "soft_delete_pct": 1, "jitter_seconds": 30},
        "failure_injection": {"fail_rate": 0.0, "latency_ms": 0},
    },
    "support_tickets": {
        "entity": "support_tickets",
        "fields": {
            "ticket_id": {"type": "uuid", "primary_key": True},
            "order_id": {"type": "ref", "ref_entity": "orders"},
            "agent_job_title": {"type": "string", "key_label": "job_title"},
            "category": {"type": "enum",
                         "values": ["billing", "shipping", "product_defect", "account", "other"]},
            "priority": {"type": "int", "min": 1, "max": 5},
            "is_escalated": {"type": "bool"},
            **_TIMESTAMPS,
        },
        "seed": {"initial_count": 20},
        "update_schedule": {"cadence": "daily", "new_records": [2, 8],
                            "mutate_existing_pct": 10, "soft_delete_pct": 3, "jitter_seconds": 60},
        "failure_injection": {"fail_rate": 0.02, "latency_ms": 50},
    },
}


def write_entity_dir(path: str) -> str:
    """Write LEGACY_ENTITIES as YAML files into `path` (created if needed); returns it."""
    os.makedirs(path, exist_ok=True)
    for name, cfg in LEGACY_ENTITIES.items():
        with open(os.path.join(path, f"{name}.yaml"), "w") as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False)
    return path
