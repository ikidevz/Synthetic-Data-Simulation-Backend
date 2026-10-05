import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.usefixtures("restore_dataset")

ROOT = os.path.dirname(os.path.dirname(__file__))
VOLATILE = {"created_at", "updated_at", "version"}


def drain(client, entity, since=0):
    changes = []
    while True:
        body = client.get(f"/{entity}/changes?since={since}&limit=500").json()
        changes.extend(body["changes"])
        since = body["next_cursor"]
        if not body["has_more"]:
            return changes, since


def fresh(client, customers=10, orders=100, seed=1):
    resp = client.post("/admin/batch", json={"mode": "replace", "counts": {"customers": customers, "orders": orders},
                                             "seed": seed, "confirm": True})
    assert resp.status_code == 200


def changes(client, **payload):
    return client.post("/admin/changes", json=payload)


def by_id(client, entity, key):
    return {i[key]: i for i in client.get(f"/{entity}?limit=500").json()["items"]}


def test_a_change_batch_inserts_updates_and_deletes_and_logs_every_one(client):
    fresh(client)
    _, cursor = drain(client, "orders")
    before = by_id(client, "orders", "order_id")

    resp = changes(client, entities=["orders"],
                   inserts=7, updates=20, deletes=5, seed=2)
    assert resp.status_code == 200
    out = resp.json()["entities"][0]
    assert (out["inserted"], out["updated"], out["deleted"]) == (7, 20, 5)
    assert out["live_rows"] == 100 + 7 - 5
    assert out["first_version"] == cursor + \
        1 and out["last_version"] == cursor + 32

    feed, _ = drain(client, "orders", cursor)
    ops = [c["op"] for c in feed]
    assert (ops.count("insert"), ops.count("update"),
            ops.count("delete")) == (7, 20, 5)
    assert [c["version"] for c in feed] == list(
        range(cursor + 1, cursor + 33))  # contiguous

    after = by_id(client, "orders", "order_id")
    assert len(after) == 102
    for gone in (c["order_id"] for c in feed if c["op"] == "delete"):
        assert gone in before and gone not in after  # soft-deleted rows leave the API
        assert client.get(f"/orders/{gone}").status_code == 404


def test_updates_actually_change_the_data(client):
    fresh(client)
    _, cursor = drain(client, "orders")
    before = by_id(client, "orders", "order_id")

    changes(client, entities=["orders"], inserts=0,
            updates=30, deletes=0, seed=3)
    updated = [c["order_id"] for c in drain(client, "orders", cursor)[
        0] if c["op"] == "update"]
    after = by_id(client, "orders", "order_id")

    assert len(updated) == 30
    changed = sum(before[i]["amount"] != after[i]["amount"] for i in updated)
    assert changed >= 27  # rewritten values, not just a bumped timestamp
    assert all(before[i]["customer_id"] == after[i]["customer_id"]
               for i in updated)  # relationships stay put
    assert all(after[i]["updated_at"] > before[i]["updated_at"]
               for i in updated)


def test_the_scheduler_tick_now_changes_values_too(client):
    from app.main import CONFIGS, TABLES
    from app.db.engine import engine
    from app.services.scheduler import run_entity_job

    fresh(client, customers=10, orders=200)
    _, cursor = drain(client, "orders")
    before = by_id(client, "orders", "order_id")
    run_entity_job(engine, "orders", TABLES, CONFIGS)
    feed = drain(client, "orders", cursor)[0]
    updated = [c["order_id"] for c in feed if c["op"] == "update"]
    after = by_id(client, "orders", "order_id")
    # a tick never edits a row it just inserted
    assert updated and all(i in before for i in updated)
    still_live = [i for i in updated if i in after]
    assert sum(before[i]["amount"] != after[i]["amount"]
               for i in still_live) >= len(still_live) * 0.8


def test_the_scheduler_never_touches_soft_deleted_rows(client):
    from app.main import CONFIGS, TABLES
    from app.db.engine import engine
    from app.services.scheduler import run_entity_job

    fresh(client, customers=10, orders=100)
    _, before_deletes = drain(client, "orders")
    changes(client, entities=["orders"], inserts=0,
            updates=0, deletes=60, seed=1)  # 60 of 100 deleted
    feed, cursor = drain(client, "orders", before_deletes)
    gone = {c["order_id"] for c in feed if c["op"] == "delete"}
    assert len(gone) == 60

    for _ in range(30):  # many ticks: with the old bug a deleted row would be picked sooner or later
        run_entity_job(engine, "orders", TABLES, CONFIGS)
    touched = {c["order_id"] for c in drain(client, "orders", cursor)[
        0] if c["op"] in ("update", "delete")}
    assert not touched & gone


def test_same_seed_gives_the_same_change_batch(client):
    def run():
        fresh(client, seed=5)
        changes(client, entities=["orders"],
                inserts=5, updates=10, deletes=3, seed=9)

        def strip(items): return [
            {k: v for k, v in i.items() if k not in VOLATILE} for i in items]
        return strip(client.get("/orders?limit=500").json()["items"])

    assert run() == run()


def test_defaults_are_one_scheduler_tick_and_oversized_counts_are_clamped(client):
    fresh(client, customers=5, orders=50)
    out = changes(client, entities=["orders"], seed=11).json()["entities"][0]
    assert 5 <= out["inserted"] <= 15  # new_records: [5, 15] in orders.yaml

    body = changes(client, entities=["customers"],
                   inserts=0, updates=50, deletes=0).json()
    assert body["entities"][0]["updated"] == 5  # only 5 live customers
    assert any("only 5 are live" in n for n in body["notes"])


def test_failures_roll_back_and_bad_requests_are_clean(client, monkeypatch):
    import app.services.batch as batch_mod
    import app.main as main_mod

    fresh(client)
    before = client.get("/metrics").json()["orders"]["row_count"]

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(batch_mod, "generate_update", boom)
    with pytest.raises(RuntimeError):
        batch_mod.run_changes(main_mod.engine, main_mod.TABLES, main_mod.CONFIGS,
                              entities=["orders"], inserts=5, updates=3, deletes=0)
    monkeypatch.undo()
    # the inserts rolled back too
    assert client.get("/metrics").json()["orders"]["row_count"] == before
    assert changes(client, entities=[
                   "orders"], inserts=1, updates=0, deletes=0).status_code == 200  # lock free

    for payload, code in [({"entities": ["nope"]}, "unknown_entity"), ({"inserts": -1}, "validation_error"),
                          ({"batch_size": 0}, "validation_error")]:
        resp = changes(client, **payload)
        assert resp.status_code == 422 and resp.json()["error"]["code"] == code


def test_child_inserts_need_live_parents(client):
    # support_tickets depends on orders, so it must be zeroed too, or this reset would
    # itself fail trying to generate support_tickets rows with no orders to reference.
    client.post("/admin/batch", json={"mode": "replace",
                                      "counts": {"customers": 0, "orders": 0, "support_tickets": 0},
                                      "confirm": True})
    resp = changes(client, entities=["orders"],
                   inserts=3, updates=0, deletes=0)
    assert resp.status_code == 409 and resp.json(
    )["error"]["code"] == "missing_parent_rows"


def test_changes_route_respects_the_api_key(client, monkeypatch):
    from app.security import api_keys as security

    monkeypatch.setattr(security, "API_KEYS", ["k"])
    assert changes(client).status_code == 401


def test_cli_changes(tmp_path):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'c.db'}"}
    run = lambda *a: subprocess.run([sys.executable, "-m", "app.cli", *a],
                                    cwd=ROOT, capture_output=True, text=True, env=env)
    assert run("batch", "--seed", "1").returncode == 0
    result = run("changes", "--entities", "orders", "--inserts",
                 "5", "--updates", "3", "--deletes", "1", "--seed", "1")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)["entities"][0]
    assert (out["inserted"], out["updated"], out["deleted"]) == (5, 3, 1)


def test_reused_seed_that_collides_gets_a_clean_error_not_a_crash(client):
    """A real, if uncommon, property of seeded PRNGs: reuse the exact same seed for two
    separate inserts into a table that's still live, and their random sequences can
    realign after enough rows and start producing identical values. This is a known
    seed=1-vs-seed=1 collision for these exact configs and row counts — reproduced here
    on purpose to prove the app now turns it into a clean 409, not a raw IntegrityError."""
    fresh(client, customers=5, orders=50, seed=1)
    resp = changes(client, entities=["orders"], seed=1)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "duplicate_primary_key"
    # and the table is left exactly as it was — the failed insert didn't partially land
    assert len(by_id(client, "orders", "order_id")) == 50
