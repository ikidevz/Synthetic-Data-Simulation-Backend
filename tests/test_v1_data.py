"""The data side of a published config: rows, the change feed, exports, DDL, batch, simulate."""
import csv
import io
import json
import re

import pytest

from conftest import customers_cfg, make_provider, orders_cfg, publish

NAMESPACED = re.compile(r"\bd_[0-9a-f]{10}_")


@pytest.fixture(scope="module")
def shop(client, alice):
    cust = publish(client, alice, customers_cfg("dt_cust", count=6))
    orders = publish(client, alice, orders_cfg(
        "dt_cust", name="dt_orders", count=10))
    return {"cust": cust["id"], "orders": orders["id"], "h": alice["h"]}


# ------------------------------------------------------------- row writes ----
def test_create_get_update_and_soft_delete_a_row(client, shop):
    h, oid = shop["h"], shop["orders"]
    made = client.post(
        f"/v1/configs/{oid}/data", json={"amount": 99.5, "note": "hello"}, headers=h)
    assert made.status_code == 201
    row = made.json()
    assert row["amount"] == 99.5 and row["note"] == "hello" and row["version"] >= 1 and row["order_id"]

    got = client.get(
        f"/v1/configs/{oid}/data/{row['order_id']}", headers=h).json()
    assert got["order_id"] == row["order_id"]

    upd = client.put(
        f"/v1/configs/{oid}/data/{row['order_id']}", json={"status": "shipped"}, headers=h)
    assert upd.status_code == 200 and upd.json()["status"] == "shipped" and upd.json()[
        "version"] > row["version"]

    gone = client.delete(
        f"/v1/configs/{oid}/data/{row['order_id']}", headers=h)
    assert gone.status_code == 200 and gone.json(
    ) == {"deleted": True, "order_id": row["order_id"]}
    assert client.get(
        f"/v1/configs/{oid}/data/{row['order_id']}", headers=h).status_code == 404
    assert client.put(f"/v1/configs/{oid}/data/{row['order_id']}", json={
                      "note": "x"}, headers=h).status_code == 404
    listed = client.get(
        f"/v1/configs/{oid}/data?limit=500", headers=h).json()["items"]
    assert row["order_id"] not in {r["order_id"]
                                   # soft-deleted rows are hidden
                                   for r in listed}


def test_a_missing_row_uses_your_config_name_in_the_error(client, shop):
    resp = client.get(
        f"/v1/configs/{shop['orders']}/data/nope", headers=shop["h"])
    assert resp.status_code == 404 and "dt_orders 'nope'" in resp.json()[
        "error"]["message"]


@pytest.mark.parametrize("payload, code", [
    ({"bogus": 1}, "unknown_field"),
    ({"order_id": "mine"}, "read_only_field"),
    ({"version": 5}, "read_only_field"),
    ({"deleted_at": "2026-01-01T00:00:00Z"}, "read_only_field"),
    ({"amount": "lots"}, "invalid_value"),
    ({"amount": True}, "invalid_value"),
    ({"status": "teleported"}, "invalid_value"),
    ({"amount": None}, "invalid_value"),
    ({"customer_id": 7}, "invalid_value"),
    ({"customer_id": "no-such-customer"}, "invalid_reference"),
])
def test_row_writes_are_validated(client, shop, payload, code):
    resp = client.post(
        f"/v1/configs/{shop['orders']}/data", json=payload, headers=shop["h"])
    assert resp.status_code == 422 and resp.json(
    )["error"]["code"] == code, resp.text


def test_nullable_fields_accept_null_and_ints_are_accepted_for_floats(client, shop):
    resp = client.post(f"/v1/configs/{shop['orders']}/data",
                       json={"note": None, "amount": 40}, headers=shop["h"])
    assert resp.status_code == 201 and resp.json()["note"] is None and resp.json()[
        "amount"] == 40.0


def test_timestamps_are_parsed_from_iso_strings(client, alice):
    made = publish(client, alice, {"entity": "dt_ts", "fields": {
        "id": {"type": "uuid", "primary_key": True}, "at": {"type": "timestamp"}}, "seed": {"initial_count": 1}})
    ok = client.post(f"/v1/configs/{made['id']}/data",
                     json={"at": "2026-01-31T12:00:00Z"}, headers=alice["h"])
    assert ok.status_code == 201 and ok.json(
    )["at"].startswith("2026-01-31T12:00:00")
    bad = client.post(
        f"/v1/configs/{made['id']}/data", json={"at": "last tuesday"}, headers=alice["h"])
    assert bad.status_code == 422 and bad.json(
    )["error"]["code"] == "invalid_value"


def test_a_config_without_a_soft_delete_field_cannot_delete_rows(client, alice):
    made = publish(client, alice, {"entity": "dt_hard", "fields": {
        "id": {"type": "uuid", "primary_key": True}, "v": {"type": "int"}}, "seed": {"initial_count": 2}})
    row = client.get(
        f"/v1/configs/{made['id']}/data", headers=alice["h"]).json()["items"][0]["id"]
    resp = client.delete(
        f"/v1/configs/{made['id']}/data/{row}", headers=alice["h"])
    assert resp.status_code == 400 and resp.json(
    )["error"]["code"] == "not_supported"


# ------------------------------------------------------------------ reads ----
def test_keyset_pagination_walks_every_row_exactly_once(client, alice):
    made = publish(client, alice, customers_cfg("dt_page", count=25))
    seen, after = [], None
    for _ in range(10):
        url = f"/v1/configs/{made['id']}/data?limit=10" + \
            (f"&after={after}" if after else "")
        page = client.get(url, headers=alice["h"]).json()
        seen += [r["customer_id"] for r in page["items"]]
        after = page["next_after"]
        if after is None:
            break
    assert len(seen) == 25 and len(set(seen)) == 25 and seen == sorted(seen)


def test_limit_is_bounded(client, shop):
    assert client.get(
        f"/v1/configs/{shop['cust']}/data?limit=501", headers=shop["h"]).status_code == 422
    assert client.get(
        f"/v1/configs/{shop['cust']}/data?limit=0", headers=shop["h"]).status_code == 422


# ----------------------------------------------------------- change feed ----
def test_the_change_feed_records_inserts_updates_and_deletes_in_order(client, alice):
    made = publish(client, alice, customers_cfg("dt_feed", count=2))
    cid, h = made["id"], alice["h"]
    start = client.get(f"/v1/configs/{cid}/changes?since=0", headers=h).json()
    assert [c["op"] for c in start["changes"]] == ["insert", "insert"]
    assert start["next_cursor"] == start["latest_version"] == 2 and start["has_more"] is False

    row = client.post(f"/v1/configs/{cid}/data",
                      json={"name": "Feed Me"}, headers=h).json()
    client.put(
        f"/v1/configs/{cid}/data/{row['customer_id']}", json={"tier": "pro"}, headers=h)
    client.delete(f"/v1/configs/{cid}/data/{row['customer_id']}", headers=h)
    later = client.get(
        f"/v1/configs/{cid}/changes?since={start['next_cursor']}", headers=h).json()
    assert [c["op"] for c in later["changes"]] == [
        "insert", "update", "delete"]
    assert {c["customer_id"] for c in later["changes"]} == {row["customer_id"]}
    versions = [c["version"] for c in later["changes"]]
    assert versions == [3, 4, 5] and later["next_cursor"] == 5

    paged = client.get(
        f"/v1/configs/{cid}/changes?since=0&limit=2", headers=h).json()
    assert len(
        paged["changes"]) == 2 and paged["has_more"] is True and paged["next_cursor"] == 2


def test_a_stale_cursor_is_explained_with_your_config_name(client, shop):
    resp = client.get(
        f"/v1/configs/{shop['cust']}/changes?since=999999", headers=shop["h"])
    assert resp.status_code == 409 and resp.json(
    )["error"]["code"] == "cursor_ahead_of_source"
    assert "dt_cust" in resp.json(
    )["error"]["message"] and not NAMESPACED.search(resp.text)


# ---------------------------------------------------------------- export ----
def test_csv_ndjson_and_sql_exports_use_your_names(client, shop):
    h, oid = shop["h"], shop["orders"]
    csv_resp = client.get(f"/v1/configs/{oid}/export", headers=h)
    assert csv_resp.status_code == 200 and 'filename="dt_orders.csv"' in csv_resp.headers[
        "content-disposition"]
    rows = list(csv.reader(io.StringIO(csv_resp.text)))
    assert rows[0] == ["order_id", "customer_id", "amount",
                       "status", "note", "deleted_at", "version"] and len(rows) > 5
    assert int(csv_resp.headers["x-snapshot-cursor"]) >= 1

    nd = client.get(f"/v1/configs/{oid}/export?format=ndjson", headers=h)
    assert json.loads(nd.text.splitlines()[0])["order_id"]

    sql = client.get(
        f"/v1/configs/{oid}/export?format=sql&dialect=postgresql", headers=h).text
    assert sql.startswith(
        "-- dt_orders: INSERT statements (postgresql)") and "INSERT INTO dt_orders (" in sql
    assert not NAMESPACED.search(sql)


def test_delta_export_returns_only_changes_after_the_cursor(client, alice):
    made = publish(client, alice, customers_cfg("dt_delta", count=3))
    cid, h = made["id"], alice["h"]
    cursor = client.get(
        f"/v1/configs/{cid}/export", headers=h).headers["x-snapshot-cursor"]
    client.post(f"/v1/configs/{cid}/data",
                json={"name": "After Cursor"}, headers=h)
    delta = client.get(f"/v1/configs/{cid}/export?since={cursor}", headers=h)
    assert 'filename="dt_delta.changes.csv"' in delta.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(delta.text)))
    assert [r["op"] for r in rows] == [
        "insert"] and rows[0]["name"] == "After Cursor"
    stale = client.get(f"/v1/configs/{cid}/export?since=99999", headers=h)
    assert stale.status_code == 409 and "dt_delta" in stale.json()[
        "error"]["message"]
    assert client.get(f"/v1/configs/{cid}/export?since=0&format=sql",
                      headers=h).status_code == 422  # sql can't carry changes
    assert client.get(
        f"/v1/configs/{cid}/export?format=xml", headers=h).status_code == 422


def test_include_deleted_brings_soft_deleted_rows_back(client, alice):
    made = publish(client, alice, customers_cfg("dt_del", count=3))
    cid, h = made["id"], alice["h"]
    victim = client.get(
        f"/v1/configs/{cid}/data", headers=h).json()["items"][0]["customer_id"]
    client.delete(f"/v1/configs/{cid}/data/{victim}", headers=h)
    live = list(csv.DictReader(io.StringIO(client.get(
        f"/v1/configs/{cid}/export", headers=h).text)))
    every = list(csv.DictReader(io.StringIO(client.get(
        f"/v1/configs/{cid}/export?include_deleted=true", headers=h).text)))
    assert (len(live), len(every)) == (2, 3)


# ------------------------------------------------------------------- ddl ----
def test_ddl_uses_your_table_names_and_includes_parents_by_default(client, shop):
    h = shop["h"]
    full = client.get(f"/v1/configs/{shop['orders']}/ddl", headers=h)
    assert full.status_code == 200 and full.headers["content-type"].startswith(
        "text/plain")
    assert "CREATE TABLE IF NOT EXISTS dt_cust" in full.text and "CREATE TABLE IF NOT EXISTS dt_orders" in full.text
    assert full.text.index("dt_cust (") < full.text.index(
        "dt_orders (")  # parents first
    assert "REFERENCES dt_cust (customer_id)" in full.text and not NAMESPACED.search(
        full.text)
    assert "idx_dt_orders_version" in full.text

    alone = client.get(
        f"/v1/configs/{shop['orders']}/ddl?include_parents=false", headers=h).text
    assert "CREATE TABLE IF NOT EXISTS dt_cust" not in alone
    pg = client.get(
        f"/v1/configs/{shop['cust']}/ddl?dialect=postgresql", headers=h).text
    assert "TIMESTAMP WITHOUT TIME ZONE" in pg or "TIMESTAMP" in pg


# ----------------------------------------------------------------- batch ----
def test_batch_append_adds_rows_and_is_reproducible_with_a_seed(client, alice):
    a = publish(client, alice, customers_cfg("dt_seed_a", count=0))
    b = publish(client, alice, customers_cfg("dt_seed_b", count=0))
    for made in (a, b):
        resp = client.post(f"/v1/configs/{made['id']}/batch", json={
                           "mode": "append", "count": 5, "seed": 42}, headers=alice["h"])
        assert resp.status_code == 200 and resp.json()[
            "entities"][0]["inserted"] == 5

    def names(made): return [r["name"] for r in client.get(
        f"/v1/configs/{made['id']}/data?limit=50", headers=alice["h"]).json()["items"]]
    assert sorted(names(a)) == sorted(names(b)) and len(
        names(a)) == 5  # same seed, same people


def test_destructive_batch_modes_need_confirmation(client, shop):
    for mode in ("replace", "reset"):
        resp = client.post(
            f"/v1/configs/{shop['cust']}/batch", json={"mode": mode}, headers=shop["h"])
        assert resp.status_code == 400 and resp.json(
        )["error"]["code"] == "confirmation_required"


def test_replace_refreshes_dependents_and_reports_them_by_name(client, alice):
    p = publish(client, alice, customers_cfg("dt_rp", count=4))
    c = publish(client, alice, orders_cfg("dt_rp", name="dt_rc", count=6))
    resp = client.post(f"/v1/configs/{p['id']}/batch", json={
                       "mode": "replace", "confirm": True, "count": 3}, headers=alice["h"])
    body = resp.json()
    assert resp.status_code == 200 and body["auto_included"] == ["dt_rc"]
    sizes = {e["entity"]: e["total_rows"] for e in body["entities"]}
    # the child keeps its own initial_count
    assert sizes == {"dt_rp": 3, "dt_rc": 6}
    assert not NAMESPACED.search(resp.text)


def test_reset_restarts_the_change_feed(client, alice):
    made = publish(client, alice, customers_cfg("dt_reset", count=4))
    client.post(f"/v1/configs/{made['id']}/batch", json={
                "mode": "reset", "confirm": True, "count": 2}, headers=alice["h"])
    feed = client.get(
        f"/v1/configs/{made['id']}/changes?since=0", headers=alice["h"]).json()["changes"]
    assert len(feed) == 2 and min(c["version"] for c in feed) == 1


def test_batch_validation(client, shop):
    h, cid = shop["h"], shop["cust"]
    assert client.post(
        f"/v1/configs/{cid}/batch", json={"mode": "bogus"}, headers=h).status_code == 422
    assert client.post(
        f"/v1/configs/{cid}/batch", json={"count": -1}, headers=h).status_code == 422
    assert client.post(
        f"/v1/configs/{cid}/batch", json={"batch_size": 0}, headers=h).status_code == 422


def test_a_refresh_that_would_orphan_a_child_is_refused_whole_and_names_your_configs(client, alice):
    p = publish(client, alice, customers_cfg("dt_ep", count=2))
    c = publish(client, alice, orders_cfg("dt_ep", name="dt_ec", count=2))
    # Replacing the parent with 0 rows would leave nothing for the regenerated child to point at.
    resp = client.post(f"/v1/configs/{p['id']}/batch", json={
                       "mode": "replace", "confirm": True, "count": 0}, headers=alice["h"])
    assert resp.status_code == 409 and resp.json(
    )["error"]["code"] == "missing_parent_rows"
    assert "dt_ep" in resp.json(
    )["error"]["message"] and not NAMESPACED.search(resp.text)
    # all-or-nothing: neither table changed
    assert client.get(
        f"/v1/configs/{p['id']}/metrics", headers=alice["h"]).json()["row_count"] == 2
    assert client.get(
        f"/v1/configs/{c['id']}/metrics", headers=alice["h"]).json()["row_count"] == 2


# -------------------------------------------------------------- simulate ----
def test_simulate_applies_exact_counts_and_feeds_the_change_log(client, alice):
    made = publish(client, alice, customers_cfg("dt_sim", count=20))
    cid, h = made["id"], alice["h"]
    cursor = client.get(
        f"/v1/configs/{cid}/export", headers=h).headers["x-snapshot-cursor"]
    resp = client.post(f"/v1/configs/{cid}/simulate", json={
                       "inserts": 3, "updates": 4, "deletes": 2, "seed": 7}, headers=h)
    assert resp.status_code == 200, resp.text
    stats = resp.json()["entities"][0]
    assert (stats["entity"], stats["inserted"], stats["updated"],
            stats["deleted"]) == ("dt_sim", 3, 4, 2)
    ops = [c["op"] for c in client.get(
        f"/v1/configs/{cid}/changes?since={cursor}&limit=100", headers=h).json()["changes"]]
    assert (ops.count("insert"), ops.count("update"),
            ops.count("delete")) == (3, 4, 2)
    assert len(client.get(
        # 20 + 3 - 2
        f"/v1/configs/{cid}/data?limit=100", headers=h).json()["items"]) == 21


def test_simulate_clamps_and_explains(client, alice):
    made = publish(client, alice, customers_cfg("dt_clamp", count=2))
    resp = client.post(f"/v1/configs/{made['id']}/simulate", json={
                       "inserts": 0, "updates": 10, "deletes": 10}, headers=alice["h"])
    body = resp.json()
    assert resp.status_code == 200 and body["notes"] and all(
        "dt_clamp" in n and not NAMESPACED.search(n) for n in body["notes"])


def test_simulate_without_counts_runs_one_default_tick(client, alice):
    made = publish(client, alice, customers_cfg(
        "dt_tick", count=10, update_schedule={"new_records": [2, 4]}))
    resp = client.post(
        f"/v1/configs/{made['id']}/simulate", json={}, headers=alice["h"])
    assert resp.status_code == 200 and 2 <= resp.json()[
        "entities"][0]["inserted"] <= 4


# ---------------------------------------------------------------- quotas ----
def test_batch_and_simulate_respect_the_per_config_row_cap(client, su, monkeypatch):
    p = make_provider(client, su, "Cap Cora")
    monkeypatch.setenv("MAX_ROWS_PER_CONFIG", "30")
    made = publish(client, p, customers_cfg("dt_cap", count=10))
    cid, h = made["id"], p["h"]

    assert client.post(f"/v1/configs/{cid}/batch", json={"count": 21},
                       headers=h).status_code == 422  # 10 + 21 > 30
    ok = client.post(f"/v1/configs/{cid}/batch", json={"count": 20}, headers=h)
    assert ok.status_code == 200 and ok.json(
    )["entities"][0]["total_rows"] == 30
    over = client.post(
        f"/v1/configs/{cid}/batch", json={"count": 1}, headers=h)
    assert over.status_code == 422 and over.json(
    )["error"]["code"] == "quota_exceeded" and "30" in over.json()["error"]["message"]
    assert client.post(
        f"/v1/configs/{cid}/simulate", json={"inserts": 1}, headers=h).status_code == 422
    assert client.post(f"/v1/configs/{cid}/simulate", json={
                       "inserts": 0, "deletes": 2}, headers=h).status_code == 200
    # a default tick is clamped to the headroom instead of overshooting
    again = client.post(f"/v1/configs/{cid}/simulate", json={}, headers=h)
    assert again.status_code == 200
    assert client.get(f"/v1/configs/{cid}/metrics",
                      headers=h).json()["row_count"] <= 30
    # replace may refill up to the cap but not past it
    assert client.post(f"/v1/configs/{cid}/batch", json={
                       "mode": "replace", "confirm": True, "count": 31}, headers=h).status_code == 422


# ---------------------------------------------------------------- metrics ----
def test_metrics_report_rows_status_and_the_cap(client, shop):
    m = client.get(
        f"/v1/configs/{shop['orders']}/metrics", headers=shop["h"]).json()
    assert m["name"] == "dt_orders" and m["row_count"] >= 10 and "last_status" in m and m["max_rows"] == 50000
