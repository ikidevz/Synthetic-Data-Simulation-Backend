import json
import os
import random
import sqlite3
import subprocess
import sys

import pytest

from app.services.batch import dependency_order, run_batch, with_dependents
from app.config.entities import load_entity_configs
from app.services.generator import generate_row

ROOT = os.path.dirname(os.path.dirname(__file__))
CONFIG_DIR = os.path.join(ROOT, "configs")
VOLATILE = {"created_at", "updated_at", "version"}  # not pinned by a seed


@pytest.fixture(autouse=True, scope="module")
def restore_default_data(client):
    """These tests rewrite the dataset; put the defaults back afterwards."""
    yield
    client.post("/admin/batch", json={"mode": "replace", "confirm": True})


def drain(client, entity, since=0):
    """Follow the change feed to the end. Returns (changes, final_cursor)."""
    changes = []
    while True:
        body = client.get(f"/{entity}/changes?since={since}&limit=500").json()
        changes.extend(body["changes"])
        since = body["next_cursor"]
        if not body["has_more"]:
            return changes, since


def rows(client, entity):
    return client.get("/metrics").json()[entity]["row_count"]


def batch(client, **payload):
    return client.post("/admin/batch", json=payload)


# ---------------------------------------------------------------- unit ----
def test_dependency_order_and_dependents():
    configs = load_entity_configs(CONFIG_DIR)
    assert dependency_order(configs, ["orders", "customers"]) == [
        "customers", "orders"]
    # support_tickets depends on orders, which depends on customers — both dependents
    # of customers are transitive, so replacing customers must refresh both.
    assert with_dependents(configs, ["customers"]) == {
        "customers", "orders", "support_tickets"}
    assert with_dependents(configs, ["orders"]) == {
        "orders", "support_tickets"}
    assert with_dependents(configs, ["support_tickets"]) == {
        "support_tickets"}  # nothing depends on it


def test_seeded_rng_gives_reproducible_rows():
    cfg = load_entity_configs(CONFIG_DIR)["customers"]

    def make(seed):
        rng = random.Random(seed)
        return [
            {k: v for k, v in generate_row(
                cfg, {}, rng).items() if k not in VOLATILE}
            for _ in range(20)
        ]

    assert make(5) == make(5)
    assert make(5) != make(6)


# ------------------------------------------------------------- append ----
def test_append_adds_rows_in_batches_and_logs_them(client):
    before = rows(client, "orders")
    _, cursor = drain(client, "orders")

    resp = batch(client, entities=["orders"],
                 count=250, batch_size=100, seed=7)
    assert resp.status_code == 200
    out = resp.json()["entities"][0]
    assert (out["entity"], out["inserted"], out["batches"],
            out["deleted"]) == ("orders", 250, 3, 0)
    assert out["first_version"] == cursor + 1
    assert rows(client, "orders") == before + 250

    changes, _ = drain(client, "orders", cursor)
    assert [c["op"] for c in changes] == ["insert"] * 250
    versions = [c["version"] for c in changes]
    # contiguous, in order
    assert versions == list(range(cursor + 1, cursor + 251))


# ----------------------------------------------------- replace / reset ----
def test_destructive_modes_require_confirmation(client):
    for mode in ("replace", "reset"):
        resp = batch(client, mode=mode)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "confirmation_required"


def test_replace_regenerates_and_keeps_consumer_cursors_valid(client):
    _, old_cursor = drain(client, "orders")

    resp = batch(client, mode="replace", entities=["customers"],
                 counts={"customers": 10, "orders": 40}, seed=1, confirm=True)
    assert resp.status_code == 200
    body = resp.json()
    # dependents get refreshed too
    assert body["auto_included"] == ["orders", "support_tickets"]
    by = {e["entity"]: e for e in body["entities"]}
    assert (by["customers"]["inserted"], by["orders"]["inserted"]) == (10, 40)
    assert by["orders"]["deleted"] > 0

    customers = client.get("/customers?limit=500").json()["items"]
    orders = client.get("/orders?limit=500").json()["items"]
    assert (len(customers), len(orders)) == (10, 40)
    valid_ids = {c["customer_id"] for c in customers}
    # no dangling refs
    assert all(o["customer_id"] in valid_ids for o in orders)

    # The old cursor still works: the refresh reads as deletes then inserts.
    changes, new_cursor = drain(client, "orders", old_cursor)
    ops = [c["op"] for c in changes]
    assert ops.count("delete") == by["orders"]["deleted"]
    assert ops.count("insert") == 40
    assert new_cursor > old_cursor


def test_same_seed_regenerates_the_same_dataset(client):
    payload = dict(mode="replace", counts={
                   "customers": 12, "orders": 30}, seed=99, confirm=True)

    def snapshot():
        def strip(items): return [
            {k: v for k, v in i.items() if k not in VOLATILE} for i in items]
        return (
            strip(client.get("/customers?limit=500").json()["items"]),
            strip(client.get("/orders?limit=500").json()["items"]),
        )

    assert batch(client, **payload).status_code == 200
    first = snapshot()
    assert batch(client, **payload).status_code == 200
    assert snapshot() == first
    assert len(first[0]) == 12 and len(first[1]) == 30


def test_reset_restarts_versions_and_flags_stale_cursors(client):
    _, old_cursor = drain(client, "orders")
    assert old_cursor > 5

    resp = batch(client, mode="reset", counts={
                 "customers": 3, "orders": 5}, seed=3, confirm=True)
    assert resp.status_code == 200

    changes, cursor = drain(client, "orders")
    assert [c["version"] for c in changes] == [1, 2, 3, 4, 5] and cursor == 5

    stale = client.get(f"/orders/changes?since={old_cursor}")
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "cursor_ahead_of_source"


# ------------------------------------------------------------ failures ----
def test_failed_batch_rolls_back_completely_and_releases_the_lock(client, monkeypatch):
    import app.services.batch as batch_mod
    import app.main as main_mod

    before = rows(client, "customers")
    real = batch_mod.generate_row
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(batch_mod, "generate_row", flaky)
    with pytest.raises(RuntimeError):
        run_batch(main_mod.engine, main_mod.TABLES, main_mod.CONFIGS,
                  mode="append", entities=["customers"], count=10, batch_size=2)
    monkeypatch.undo()

    assert rows(client, "customers") == before  # nothing half-applied
    assert batch(client, entities=["customers"],
                 count=1).status_code == 200  # lock released


def test_append_to_a_child_with_no_parent_rows_is_a_clean_409(client):
    # support_tickets depends on orders, so it must be zeroed too, or the replace itself
    # would fail trying to generate support_tickets rows with no orders left to reference.
    assert batch(client, mode="replace",
                 counts={"customers": 0, "orders": 0, "support_tickets": 0}, confirm=True).status_code == 200
    assert rows(client, "customers") == 0 and rows(client, "orders") == 0

    resp = batch(client, entities=["orders"], count=5)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "missing_parent_rows"
    assert rows(client, "orders") == 0


@pytest.mark.parametrize(
    "payload, code",
    [
        ({"mode": "nuke"}, "validation_error"),
        ({"batch_size": 0}, "validation_error"),
        ({"counts": {"orders": -1}}, "validation_error"),
        ({"count": 10_000_000}, "validation_error"),
        ({"entities": ["nope"]}, "unknown_entity"),
        ({"counts": {"nope": 5}}, "unknown_entity"),
        ({"entities": ["customers"], "counts": {
         "orders": 5}}, "counts_not_targeted"),
    ],
)
def test_bad_requests_get_a_422_with_the_standard_envelope(client, payload, code):
    resp = batch(client, **payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == code


# ----------------------------------------------------------------- DDL ----
def test_ddl_endpoint_emits_ordered_idempotent_sqlite_ddl(client):
    resp = client.get("/ddl")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.text
    parent = text.index("CREATE TABLE IF NOT EXISTS customers")
    child = text.index("CREATE TABLE IF NOT EXISTS orders")
    assert parent < child  # parents first, so the script runs top to bottom
    assert "REFERENCES customers (customer_id)" in text
    assert "CREATE INDEX IF NOT EXISTS idx_orders_version" in text
    assert "change_log" not in text  # internal tables are opt-in


def test_generated_ddl_actually_runs_and_matches_the_configs(client):
    text = client.get("/ddl?include_system=true").text
    conn = sqlite3.connect(":memory:")
    conn.executescript(text)
    conn.executescript(text)  # IF NOT EXISTS: safe to run twice

    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"customers", "orders", "change_log", "scheduler_runs"} <= names

    configs = load_entity_configs(CONFIG_DIR)
    for entity, cfg in configs.items():
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({entity})")]
        assert cols == list(cfg.fields)


def test_ddl_postgres_dialect_and_bad_dialect(client):
    pg = client.get("/ddl?dialect=postgresql")
    assert pg.status_code == 200
    assert "CREATE TABLE IF NOT EXISTS orders" in pg.text
    assert "TIMESTAMP" in pg.text and "VARCHAR" in pg.text

    bad = client.get("/ddl?dialect=oracle")
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "validation_error"


def test_ddl_and_batch_respect_the_api_key(client, monkeypatch):
    from app.security import api_keys as security

    monkeypatch.setattr(security, "API_KEYS", ["k"])
    assert client.get("/ddl").status_code == 401
    assert client.post(
        "/admin/batch", json={"mode": "append"}).status_code == 401
    assert client.get("/ddl", headers={"X-API-Key": "k"}).status_code == 200


# ----------------------------------------------------------------- CLI ----
def run_cli(tmp_path, *args):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'cli.db'}"}
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )


def test_cli_ddl_writes_a_file(tmp_path):
    out = tmp_path / "schema.sql"
    result = run_cli(tmp_path, "ddl", "--dialect",
                     "postgresql", "--out", str(out))
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE IF NOT EXISTS orders" in out.read_text()


def test_cli_batch_generates_then_refreshes_with_confirmation(tmp_path):
    first = run_cli(tmp_path, "batch", "--seed", "1")
    assert first.returncode == 0, first.stderr
    inserted = {e["entity"]: e["inserted"]
                for e in json.loads(first.stdout)["entities"]}
    assert inserted == {"customers": 30, "orders": 50,
                        "support_tickets": 20}  # defaults from the configs

    refused = run_cli(tmp_path, "batch", "--mode", "replace")
    assert refused.returncode != 0
    assert "confirm" in refused.stderr.lower()

    ok = run_cli(tmp_path, "batch", "--mode", "replace",
                 "--yes", "--counts", "customers=5", "orders=8")
    assert ok.returncode == 0, ok.stderr
    totals = {e["entity"]: e["total_rows"]
              for e in json.loads(ok.stdout)["entities"]}

    assert totals == {"customers": 5, "orders": 8, "support_tickets": 20}
