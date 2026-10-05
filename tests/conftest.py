import os

import pytest
from fastapi.testclient import TestClient

# app.db.engine reads DATABASE_URL once, at import time. So this has to run BEFORE
# anything under `app` is imported — otherwise the engine silently points at whatever
# the environment gave it (./synthetic.db by default) and state leaks between runs.
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
if TEST_DB_URL:
    from sqlalchemy import create_engine, text

    _engine = create_engine(TEST_DB_URL)
    with _engine.begin() as _conn:
        _conn.execute(text("DROP SCHEMA public CASCADE"))
        _conn.execute(text("CREATE SCHEMA public"))
    _engine.dispose()
    os.environ["DATABASE_URL"] = TEST_DB_URL
else:
    TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_api.db")
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

# Provider tests create many configs under one provider; the quota tests set their own limits.
os.environ.setdefault("MAX_CONFIGS_PER_PROVIDER", "500")

from app.main import app  # noqa: E402  (must follow the DATABASE_URL setup above)


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def restore_dataset(client):
    """For modules that rewrite the dataset: put the default data back afterwards."""
    yield
    client.post("/admin/batch", json={"mode": "replace", "confirm": True})


# ---------------------------------------------------------------------------
# Fixtures for the provider / /v1 tests. Authentication is switched on for one test
# MODULE (the rest of the suite runs with it off) and restored afterwards.
# ---------------------------------------------------------------------------
SUPER_KEY = "super-" + "s" * 40


@pytest.fixture(scope="module")
def authed(client):
    from app.security import api_keys as security

    patch = pytest.MonkeyPatch()
    patch.setattr(security, "API_KEYS", [SUPER_KEY])
    yield
    patch.undo()


@pytest.fixture(scope="module")
def su(authed):
    """Headers for the superuser (an env-configured admin key)."""
    return {"X-API-Key": SUPER_KEY}


def make_provider(client, su, full_name):
    """Create a provider and return a working key for it.

    `alice`/`bob` are module-scoped but the database is session-wide, so by the second
    test module the name already exists. Rather than hand out a duplicate name, reuse the
    existing provider and issue a *new* key for it -- the original secret exists only as a
    hash, so a reused provider must be given a fresh one to be usable.
    """
    resp = client.post("/v1/admin/providers", params={"on_exists": "reuse"},
                       json={"full_name": full_name}, headers=su)
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    if not body["created"]:
        again = client.post(f"/v1/admin/providers/{body['id']}/keys",
                            json={"label": "test"}, headers=su)
        assert again.status_code == 201, again.text
        body = {**body, **again.json()}
    return {"id": body["id"], "full_name": body["full_name"], "key_id": body["key"]["id"],
            "api_key": body["api_key"], "h": {"X-API-Key": body["api_key"]}}


@pytest.fixture(scope="module")
def alice(client, su):
    return make_provider(client, su, "Alice Almeida")


@pytest.fixture(scope="module")
def bob(client, su):
    return make_provider(client, su, "Bob Bautista")


# Small configs the tests share. `customers` is a parent, `orders` references it.
def customers_cfg(name="customers", count=5, **extra):
    return {
        "entity": name,
        "fields": {
            "customer_id": {"type": "uuid", "primary_key": True},
            "name": {"type": "string", "key_label": "full_name"},
            "tier": {"type": "enum", "values": ["free", "pro"]},
            "created_at": {"type": "timestamp", "auto": "created"},
            "updated_at": {"type": "timestamp", "auto": "updated"},
            "deleted_at": {"type": "timestamp", "auto": "soft_delete", "nullable": True},
        },
        "seed": {"initial_count": count},
        **extra,
    }


def orders_cfg(parent="customers", name="orders", count=8, **extra):
    return {
        "entity": name,
        "fields": {
            "order_id": {"type": "uuid", "primary_key": True},
            "customer_id": {"type": "ref", "ref_entity": parent},
            "amount": {"type": "float", "min": 10, "max": 500},
            "status": {"type": "enum", "values": ["pending", "shipped"]},
            "note": {"type": "string", "nullable": True},
            "deleted_at": {"type": "timestamp", "auto": "soft_delete", "nullable": True},
        },
        "seed": {"initial_count": count},
        **extra,
    }


def publish(client, who, cfg, expect=201):
    resp = client.post("/v1/configs", json=cfg,
                       headers=who["h"] if "h" in who else who)
    assert resp.status_code == expect, resp.text
    return resp.json()
