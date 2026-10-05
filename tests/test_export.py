import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile

import pytest

pytestmark = pytest.mark.usefixtures("restore_dataset")

ROOT = os.path.dirname(os.path.dirname(__file__))
NASTY = "O'Brien; DROP TABLE orders;-- \"quoted\", with a comma\nand a newline"


def drain(client, entity, since=0):
    changes = []
    while True:
        body = client.get(f"/{entity}/changes?since={since}&limit=500").json()
        changes.extend(body["changes"])
        since = body["next_cursor"]
        if not body["has_more"]:
            return changes, since


def load_sqlite(schema, *data_files):
    """Load exported schema + data into a fresh database with foreign keys ENFORCED."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema)
    for sql in data_files:
        conn.executescript(sql)
    return conn


def fresh(client):
    assert client.post("/admin/batch", json={"mode": "replace", "counts": {"customers": 15, "orders": 60},
                                             "seed": 4, "confirm": True}).status_code == 200


def test_csv_snapshot_matches_the_api(client):
    fresh(client)
    resp = client.get("/orders/export?format=csv")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith(
        "text/csv")
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    api = {o["order_id"]: o for o in client.get(
        "/orders?limit=500").json()["items"]}
    assert list(rows[0]) == ["order_id", "customer_id", "amount",
                             "status", "created_at", "updated_at", "deleted_at", "version"]
    assert {r["order_id"] for r in rows} == set(api) and len(rows) == 60
    assert all(float(r["amount"]) == api[r["order_id"]]
               ["amount"] and r["deleted_at"] == "" for r in rows)
    assert int(resp.headers["x-snapshot-cursor"]) == drain(client, "orders")[1]


def test_soft_deleted_rows_are_excluded_unless_asked_for(client):
    fresh(client)
    victim = client.post("/orders", json={"amount": 12.5}).json()["order_id"]
    client.delete(f"/orders/{victim}")
    default = list(csv.DictReader(
        io.StringIO(client.get("/orders/export").text)))
    everything = list(csv.DictReader(io.StringIO(
        client.get("/orders/export?include_deleted=true").text)))
    assert victim not in {r["order_id"] for r in default}
    gone = [r for r in everything if r["order_id"] == victim]
    assert len(gone) == 1 and gone[0]["deleted_at"] != ""


def test_ndjson_snapshot_keeps_types(client):
    fresh(client)
    lines = client.get(
        "/customers/export?format=ndjson").text.strip().split("\n")
    records = [json.loads(line) for line in lines]
    assert len(records) == 15
    assert all(r["deleted_at"] is None and isinstance(
        r["version"], int) for r in records)


def test_sql_export_loads_into_a_real_database_in_dependency_order(client):
    fresh(client)
    schema = client.get("/ddl").text
    conn = load_sqlite(schema, client.get(
        "/customers/export?format=sql").text, client.get("/orders/export?format=sql").text)
    assert conn.execute("select count(*) from customers").fetchone()[0] == 15
    assert conn.execute("select count(*) from orders").fetchone()[0] == 60
    api = {o["order_id"]: o for o in client.get(
        "/orders?limit=500").json()["items"]}
    for order_id, customer_id, amount, status in conn.execute("select order_id, customer_id, amount, status from orders"):
        assert (customer_id, amount, status) == (
            api[order_id]["customer_id"], api[order_id]["amount"], api[order_id]["status"])
    # children before parents must fail under enforced foreign keys: the ordering matters
    with pytest.raises(sqlite3.IntegrityError):
        load_sqlite(schema, client.get("/orders/export?format=sql").text)


def test_hostile_strings_survive_csv_ndjson_and_sql(client):
    fresh(client)
    cid = client.post("/customers", json={"name": NASTY}).json()["customer_id"]

    csv_row = next(r for r in csv.DictReader(io.StringIO(
        client.get("/customers/export").text)) if r["customer_id"] == cid)
    assert csv_row["name"] == NASTY
    nd = next(json.loads(l) for l in client.get(
        "/customers/export?format=ndjson").text.splitlines() if cid in l)
    assert nd["name"] == NASTY

    conn = load_sqlite(client.get("/ddl").text,
                       client.get("/customers/export?format=sql").text)
    assert conn.execute(
        "select name from customers where customer_id=?", (cid,)).fetchone()[0] == NASTY
    # tables still there: nothing got executed as SQL
    conn.execute("select count(*) from customers")


def test_changes_export_is_the_delta_since_a_cursor(client):
    fresh(client)
    _, cursor = drain(client, "orders")
    client.post("/admin/changes",
                json={"entities": ["orders"], "inserts": 4, "updates": 6, "deletes": 2, "seed": 1})

    resp = client.get(f"/orders/export?since={cursor}&format=csv")
    assert resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    feed, latest = drain(client, "orders", cursor)
    assert [r["op"] for r in rows] == [c["op"] for c in feed]
    assert [int(r["version"]) for r in rows] == [c["version"] for c in feed]
    assert (resp.headers["x-from-cursor"],
            resp.headers["x-to-cursor"]) == (str(cursor), str(latest))
    assert [r["op"] for r in rows].count("update") == 6

    assert client.get(f"/orders/export?since={latest + 10}").status_code == 409
    bad = client.get(f"/orders/export?since={cursor}&format=sql")
    assert bad.status_code == 422 and bad.json(
    )["error"]["code"] == "unsupported_format"


def test_replace_shows_up_in_the_delta_as_deletes_then_inserts(client):
    fresh(client)
    _, cursor = drain(client, "orders")
    client.post("/admin/batch", json={"mode": "replace", "counts": {
                "customers": 3, "orders": 5}, "seed": 8, "confirm": True})
    rows = list(csv.DictReader(io.StringIO(
        client.get(f"/orders/export?since={cursor}").text)))
    ops = [r["op"] for r in rows]
    assert ops.count("delete") == 60 and ops.count("insert") == 5
    wiped = [r for r in rows if r["op"] == "delete"]
    # only the key survives a wipe
    assert all(r["order_id"] and r["amount"] == "" for r in wiped)


def test_bundle_is_schema_plus_data_plus_manifest_and_loads(client):
    fresh(client)
    resp = client.get("/export?format=sql")
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert set(zf.namelist()) == {"schema.sql", "customers.sql",
                                  "orders.sql", "support_tickets.sql", "manifest.json"}
    manifest = json.loads(zf.read("manifest.json"))
    # support_tickets depends on orders, so it comes out after it in load order
    assert manifest["load_order"] == ["customers", "orders",
                                      "support_tickets"] and manifest["schema"] == "schema.sql"
    counts = {f["entity"]: f["rows"] for f in manifest["files"]}
    # 20: support_tickets' own seed default
    assert counts == {"customers": 15, "orders": 60, "support_tickets": 20}
    assert all(isinstance(f["snapshot_cursor"], int)
               for f in manifest["files"])

    conn = load_sqlite(zf.read("schema.sql").decode(), *
                       [zf.read(f["file"]).decode() for f in manifest["files"]])
    for entity, n in counts.items():
        assert conn.execute(
            f"select count(*) from {entity}").fetchone()[0] == n


def test_bundle_can_mix_snapshots_and_changes(client):
    fresh(client)
    _, cursor = drain(client, "orders")
    client.post("/admin/changes",
                json={"entities": ["orders"], "inserts": 2, "updates": 2, "deletes": 1, "seed": 2})
    zf = zipfile.ZipFile(io.BytesIO(client.get(
        f"/export?format=csv&since=orders={cursor}").content))
    assert set(zf.namelist()) == {"schema.sql", "customers.csv",
                                  "orders.changes.csv", "support_tickets.csv", "manifest.json"}
    files = {f["entity"]: f for f in json.loads(
        zf.read("manifest.json"))["files"]}
    assert files["customers"]["kind"] == "snapshot" and files["orders"]["kind"] == "changes"
    assert files["orders"]["rows"] == 5 and files["orders"]["from_cursor"] == cursor

    only_changes = zipfile.ZipFile(io.BytesIO(client.get(
        f"/export?format=ndjson&entities=orders&since=orders={cursor}").content))
    # nothing to create for a delta-only export
    assert "schema.sql" not in only_changes.namelist()


@pytest.mark.parametrize("query, status, code", [
    ("format=xml", 422, "validation_error"),
    ("entities=nope", 422, "unknown_entity"),
    ("since=orders=abc", 422, "invalid_since"),
    ("format=sql&since=orders=0", 422, "unsupported_format"),
    ("entities=customers&since=orders=0", 422, "since_not_targeted"),
    ("since=orders=99999999", 409, "cursor_ahead_of_source"),
])
def test_bundle_rejects_bad_requests_cleanly(client, query, status, code):
    resp = client.get(f"/export?{query}")
    assert resp.status_code == status and resp.json()["error"]["code"] == code


def test_export_routes_respect_the_api_key(client, monkeypatch):
    from app.security import api_keys as security

    monkeypatch.setattr(security, "API_KEYS", ["k"])
    assert client.get("/export").status_code == 401
    assert client.get("/orders/export").status_code == 401
    assert client.get("/orders/export",
                      headers={"X-API-Key": "k"}).status_code == 200


def test_cli_export_writes_a_loadable_directory(tmp_path):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'e.db'}"}
    run = lambda *a: subprocess.run([sys.executable, "-m", "app.cli", *a],
                                    cwd=ROOT, capture_output=True, text=True, env=env)
    assert run("batch", "--counts", "customers=8",
               "orders=20", "--seed", "1").returncode == 0

    out = tmp_path / "dump"
    result = run("export", "--out-dir", str(out), "--format", "sql")
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    # support_tickets is exported too (export defaults to every entity), even though it
    # was never populated above — an entity with zero rows still gets a (empty) data file.
    assert sorted(os.listdir(out)) == [
        "customers.sql", "manifest.json", "orders.sql", "schema.sql", "support_tickets.sql"]
    conn = load_sqlite((out / "schema.sql").read_text(), *
                       [(out / f["file"]).read_text() for f in manifest["files"]])
    assert conn.execute("select count(*) from orders").fetchone()[0] == 20

    delta = run("export", "--out-dir", str(tmp_path / "delta"),
                "--format", "csv", "--entities", "orders", "--since", "orders=5")
    assert delta.returncode == 0, delta.stderr
    assert json.loads(delta.stdout)["files"][0]["from_cursor"] == 5
