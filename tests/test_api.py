from app.services.scheduler import run_entity_job


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_entities_lists_config(client):
    resp = client.get("/entities")
    assert resp.status_code == 200
    body = resp.json()
    assert "customers" in body
    assert "orders" in body


def test_seeded_customers_present(client):
    resp = client.get("/customers?limit=100")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 30  # seed.initial_count in customers.yaml


def test_seeded_orders_reference_real_customers(client):
    customers = client.get("/customers?limit=100").json()["items"]
    customer_ids = {c["customer_id"] for c in customers}

    orders = client.get("/orders?limit=100").json()["items"]
    assert len(orders) == 50  # seed.initial_count in orders.yaml
    for order in orders:
        assert order["customer_id"] in customer_ids
        assert 10 <= order["amount"] <= 500
        assert order["status"] in {"pending", "shipped", "cancelled"}
        assert order["version"] >= 1


def test_create_order(client):
    resp = client.post("/orders", json={"amount": 42.5})
    assert resp.status_code == 201
    body = resp.json()
    assert body["amount"] == 42.5
    assert "order_id" in body
    assert body["version"] >= 1


def test_get_single_order(client):
    created = client.post("/orders", json={"amount": 99.9}).json()
    order_id = created["order_id"]

    resp = client.get(f"/orders/{order_id}")
    assert resp.status_code == 200
    assert resp.json()["order_id"] == order_id


def test_get_missing_order_returns_404_envelope(client):
    resp = client.get("/orders/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"


def test_update_order_bumps_version(client):
    created = client.post("/orders", json={"amount": 10.0}).json()
    order_id = created["order_id"]
    v1 = created["version"]

    resp = client.put(f"/orders/{order_id}",
                      json={"amount": 250.0, "status": "shipped"})
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["amount"] == 250.0
    assert updated["status"] == "shipped"
    assert updated["version"] > v1


def test_delete_order_soft_deletes_and_hides_from_get(client):
    created = client.post("/orders", json={"amount": 5.0}).json()
    order_id = created["order_id"]

    del_resp = client.delete(f"/orders/{order_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    get_resp = client.get(f"/orders/{order_id}")
    assert get_resp.status_code == 404


def test_change_feed_reports_insert_update_delete(client):
    # Establish a baseline cursor
    baseline = client.get("/orders/changes?since=0&limit=500").json()
    since = baseline["next_cursor"]

    created = client.post("/orders", json={"amount": 77.0}).json()
    order_id = created["order_id"]
    client.put(f"/orders/{order_id}", json={"amount": 88.0})
    client.delete(f"/orders/{order_id}")

    changes = client.get(
        f"/orders/changes?since={since}&limit=500").json()["changes"]
    ops_for_order = [c["op"] for c in changes if c["order_id"] == order_id]

    assert "insert" in ops_for_order
    assert "update" in ops_for_order
    assert "delete" in ops_for_order
    # and they must appear in that order, since version is monotonic
    assert ops_for_order.index("insert") < ops_for_order.index(
        "update") < ops_for_order.index("delete")


def test_scheduler_job_runs_and_logs(client):
    from app.main import TABLES, CONFIGS
    from app.db.engine import engine as test_engine

    before = client.get("/orders?limit=500").json()["items"]

    run_entity_job(test_engine, "orders", TABLES, CONFIGS)

    after = client.get("/orders?limit=500").json()["items"]
    assert len(after) >= len(before)  # inserts should have happened

    runs = client.get("/scheduler/runs?entity=orders&limit=5").json()["runs"]
    assert len(runs) >= 1
    assert runs[0]["status"] == "success"


def test_metrics_endpoint(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert "orders" in body and "customers" in body
    assert body["orders"]["row_count"] > 0


def test_api_key_enforced_when_configured(client, monkeypatch):
    from app.security import api_keys as security

    monkeypatch.setattr(security, "API_KEYS", ["secret-key"])

    no_key = client.get("/orders?limit=1")
    assert no_key.status_code == 401
    assert no_key.json()["error"]["code"] == "unauthorized"

    wrong_key = client.get("/orders?limit=1", headers={"X-API-Key": "nope"})
    assert wrong_key.status_code == 401

    ok = client.get("/orders?limit=1", headers={"X-API-Key": "secret-key"})
    assert ok.status_code == 200


def test_failure_injection_returns_error_envelope(client):
    import app.main as main_mod

    cfg = main_mod.CONFIGS["orders"]
    original = cfg.failure_injection.fail_rate
    cfg.failure_injection.fail_rate = 1.0
    try:
        resp = client.get("/orders?limit=1")
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "injected_failure"
    finally:
        cfg.failure_injection.fail_rate = original

    # and it recovers once the toggle is off
    assert client.get("/orders?limit=1").status_code == 200


def test_validation_error_uses_same_envelope(client):
    resp = client.get("/orders?limit=not-a-number")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
