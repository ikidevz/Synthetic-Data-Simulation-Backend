"""POST /v1/configs/{config_id}/data/bulk"""
import pytest

from conftest import customers_cfg, make_project, make_provider, orders_cfg, publish


@pytest.fixture(scope="module")
def shop(client, alice):
    cust = publish(client, alice, customers_cfg("bk_cust", count=6))
    orders = publish(client, alice, orders_cfg(
        "bk_cust", name="bk_orders", count=3))
    return {"cust": cust["id"], "orders": orders["id"], "h": alice["h"]}


def bulk(client, shop, rows, key="orders", h=None):
    return client.post(f"/v1/configs/{shop[key]}/data/bulk",
                       json={"rows": rows}, headers=h or shop["h"])


def row_count(client, shop, key="orders"):
    return client.get(f"/v1/configs/{shop[key]}/metrics",
                      headers=shop["h"]).json()["row_count"]


# ---------------------------------------------------------------- success ----
def test_bulk_inserts_every_row_and_returns_them(client, shop):
    before = row_count(client, shop)
    resp = bulk(client, shop, [{"amount": 11.5, "note": "a"}, {
                "status": "shipped"}, {}])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["inserted"] == 3 and len(body["items"]) == 3
    assert body["items"][0]["amount"] == 11.5 and body["items"][0]["note"] == "a"
    assert body["items"][1]["status"] == "shipped"
    assert all(r["order_id"] for r in body["items"])
    assert row_count(client, shop) == before + 3
    for r in body["items"]:  # each one is really stored and readable
        got = client.get(
            f"/v1/configs/{shop['orders']}/data/{r['order_id']}", headers=shop["h"])
        assert got.status_code == 200


def test_bulk_versions_are_contiguous_and_every_row_is_in_the_change_feed(client, shop):
    body = bulk(client, shop, [{}, {}, {}, {}]).json()
    versions = [r["version"] for r in body["items"]]
    assert versions == list(
        range(body["first_version"], body["last_version"] + 1))
    feed = client.get(
        f"/v1/configs/{shop['orders']}/changes?since={body['first_version'] - 1}&limit=500",
        headers=shop["h"]).json()
    ids = {r["order_id"] for r in body["items"]}
    logged = [c for c in feed["changes"] if c["order_id"] in ids]
    assert len(logged) == 4 and all(c["op"] == "insert" for c in logged)
    assert [c["version"] for c in logged] == versions


def test_bulk_keeps_a_valid_explicit_parent_and_generates_the_rest_from_this_configs_parents(client, shop):
    parents = client.get(f"/v1/configs/{shop['cust']}/data?limit=500",
                         headers=shop["h"]).json()["items"]
    parent_ids = {p["customer_id"] for p in parents}
    pick = parents[0]["customer_id"]
    items = bulk(client, shop, [{"customer_id": pick}, {}, {}]).json()["items"]
    assert items[0]["customer_id"] == pick
    assert all(r["customer_id"] in parent_ids for r in items)


def test_bulk_accepts_exactly_the_maximum(client, alice):
    from app.services.entity_ops import MAX_BULK_ROWS
    made = publish(client, alice, customers_cfg("bk_big", count=1))
    resp = client.post(f"/v1/configs/{made['id']}/data/bulk",
                       json={"rows": [{}] * MAX_BULK_ROWS}, headers=alice["h"])
    assert resp.status_code == 201 and resp.json()["inserted"] == MAX_BULK_ROWS


# ------------------------------------------------------------- validation ----
def test_one_bad_row_stores_nothing_and_names_the_row(client, shop):
    before = row_count(client, shop)
    resp = bulk(client, shop, [{"amount": 10}, {"amount": 20}, {"bogus": 1}])
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "unknown_field" and err["message"].startswith(
        "rows[2]:")
    assert row_count(client, shop) == before


@pytest.mark.parametrize("bad, code", [
    ({"order_id": "mine"}, "read_only_field"),
    ({"version": 5}, "read_only_field"),
    ({"amount": "lots"}, "invalid_value"),
    ({"amount": True}, "invalid_value"),
    ({"status": "teleported"}, "invalid_value"),
    ({"amount": None}, "invalid_value"),
    ({"customer_id": "no-such-customer"}, "invalid_reference"),
])
def test_each_row_is_validated_like_a_single_create(client, shop, bad, code):
    before = row_count(client, shop)
    resp = bulk(client, shop, [{}, bad])
    err = resp.json()["error"]
    assert resp.status_code == 422 and err["code"] == code, resp.text
    assert err["message"].startswith("rows[1]:")
    assert row_count(client, shop) == before


@pytest.mark.parametrize("body", [{}, {"rows": []}, {"rows": None}])
def test_missing_or_empty_rows_are_rejected(client, shop, body):
    resp = client.post(
        f"/v1/configs/{shop['orders']}/data/bulk", json=body, headers=shop["h"])
    assert resp.status_code == 422 and resp.json(
    )["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("body", [{"rows": ["nope"]}, {"rows": {"a": 1}}, {"rows": 5}])
def test_rows_must_be_a_list_of_objects(client, shop, body):
    resp = client.post(
        f"/v1/configs/{shop['orders']}/data/bulk", json=body, headers=shop["h"])
    assert resp.status_code == 422 and resp.json(
    )["error"]["code"] == "validation_error"


def test_too_many_rows_is_rejected_before_anything_is_written(client, shop):
    from app.services.entity_ops import MAX_BULK_ROWS
    before = row_count(client, shop)
    resp = bulk(client, shop, [{}] * (MAX_BULK_ROWS + 1))
    assert resp.status_code == 422 and resp.json(
    )["error"]["code"] == "too_many_rows"
    assert row_count(client, shop) == before


# ------------------------------------------------------------------ quota ----
def test_bulk_respects_the_row_cap(client, su, monkeypatch):
    p = make_provider(client, su, "Bulk Cap Carl")
    monkeypatch.setenv("MAX_ROWS_PER_CONFIG", "12")
    made = publish(client, p, customers_cfg("bk_capped", count=10))
    url = f"/v1/configs/{made['id']}/data/bulk"
    over = client.post(url, json={"rows": [{}, {}, {}]}, headers=p["h"])
    assert over.status_code == 422 and over.json(
    )["error"]["code"] == "quota_exceeded"
    assert client.post(
        url, json={"rows": [{}, {}]}, headers=p["h"]).status_code == 201
    count = client.get(
        f"/v1/configs/{made['id']}/metrics", headers=p["h"]).json()["row_count"]
    assert count == 12


# ----------------------------------------------------------------- access ----
def test_other_providers_cannot_bulk_insert(client, alice, bob):
    pub = publish(client, alice, customers_cfg("bk_pub", count=2))
    hidden = make_project(client, alice, "bk_hidden", is_only_me=True)
    priv = publish(client, alice, customers_cfg(
        "bk_priv", count=2), project=hidden["id"])
    r = client.post(
        f"/v1/configs/{pub['id']}/data/bulk", json={"rows": [{}]}, headers=bob["h"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    r = client.post(
        f"/v1/configs/{priv['id']}/data/bulk", json={"rows": [{}]}, headers=bob["h"])
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    count = client.get(
        f"/v1/configs/{pub['id']}/metrics", headers=alice["h"]).json()["row_count"]
    assert count == 2  # untouched


def test_the_superuser_can_bulk_insert_into_a_providers_config(client, su, shop):
    resp = client.post(f"/v1/configs/{shop['orders']}/data/bulk",
                       json={"rows": [{}]}, headers=su)
    assert resp.status_code == 201 and resp.json()["inserted"] == 1


# ------------------------------------------------------------------- lock ----
def test_bulk_answers_busy_while_another_bulk_write_holds_the_lock(client, shop, monkeypatch):
    from app.services import entity_ops
    from app.services.scheduler import WRITE_LOCK
    monkeypatch.setattr(entity_ops, "BULK_LOCK_TIMEOUT", 0.05)
    with WRITE_LOCK:
        resp = bulk(client, shop, [{}])
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "busy"
    # lock was released properly
    assert bulk(client, shop, [{}]).status_code == 201
