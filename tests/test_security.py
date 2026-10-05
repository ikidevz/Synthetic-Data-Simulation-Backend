import logging
import os
import re
import subprocess
import sys

import pytest
from fastapi.routing import APIRoute

from app.security import api_keys as security

ROOT = os.path.dirname(os.path.dirname(__file__))
KEY = "test-key-" + "x" * 30  # long enough to avoid the short-key warning


@pytest.fixture
def keyed(monkeypatch):
    """Switch authentication on for one test."""
    monkeypatch.setattr(security, "API_KEYS", [KEY])
    return {"X-API-Key": KEY}


def protected_routes(client):
    found = []
    for route in client.app.routes:
        if isinstance(route, APIRoute) and route.path not in security.PUBLIC_PATHS:
            for method in sorted(route.methods):
                found.append((method, route.path.replace("{item_id}", "x")))
    return found


# ------------------------------------------------------- secure by default ----
def test_only_health_is_public():
    # widening this is a deliberate act
    assert security.PUBLIC_PATHS == {"/health"}


def test_every_route_rejects_a_missing_key(client, keyed):
    routes = protected_routes(client)
    assert len(routes) >= 20  # entity routes for both entities + system routes
    for method, path in routes:
        resp = client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} answered {resp.status_code} without a key"
        assert resp.json()["error"]["code"] == "unauthorized"
        assert resp.headers["www-authenticate"] == "ApiKey"


def test_the_routes_that_used_to_be_open_are_now_protected(client, keyed):
    for path in ("/entities", "/metrics", "/scheduler/runs?entity=orders"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=keyed).status_code == 200


def test_a_valid_key_opens_every_kind_of_route(client, keyed):
    for path in ("/orders?limit=1", "/orders/changes", "/orders/export", "/ddl", "/export"):
        assert client.get(path, headers=keyed).status_code == 200, path
    assert client.post("/admin/changes", json={"entities": ["orders"], "inserts": 0, "updates": 0, "deletes": 0},
                       headers=keyed).status_code == 200


def test_health_and_docs_stay_public(client, keyed):
    for path in ("/health", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 200, path


# ----------------------------------------------------------- key handling ----
def test_wrong_empty_and_query_string_keys_are_rejected(client, keyed):
    assert client.get(
        "/entities", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get(
        "/entities", headers={"X-API-Key": ""}).status_code == 401
    assert client.get(
        # near miss
        "/entities", headers={"X-API-Key": KEY[:-1]}).status_code == 401
    # keys never travel in URLs
    assert client.get(f"/entities?api_key={KEY}").status_code == 401
    assert client.get(
        "/entities", headers={"Authorization": f"Bearer {KEY}"}).status_code == 401


def test_several_keys_allow_rotation(client, monkeypatch):
    old, new = "old-key-" + "a" * 30, "new-key-" + "b" * 30
    monkeypatch.setattr(security, "API_KEYS", [old, new])
    assert client.get(
        "/entities", headers={"X-API-Key": old}).status_code == 200
    assert client.get(
        "/entities", headers={"X-API-Key": new}).status_code == 200
    assert client.get(
        "/entities", headers={"X-API-Key": "third-key"}).status_code == 401
    monkeypatch.setattr(security, "API_KEYS", [new])  # old key retired
    assert client.get(
        "/entities", headers={"X-API-Key": old}).status_code == 401


def test_key_comparison_handles_unicode_and_prefixes():
    assert security.key_is_valid("é🔑-key", ["é🔑-key"]) is True
    # would crash a naive str compare_digest
    assert security.key_is_valid("é🔑", ["abc"]) is False
    assert security.key_is_valid("good", ["good-and-longer"]) is False
    assert security.key_is_valid(None, ["k"]) is False
    assert security.key_is_valid("k", []) is False


def test_failed_attempts_are_logged_without_the_key(client, keyed, caplog):
    with caplog.at_level(logging.WARNING, logger="security"):
        client.get("/metrics", headers={"X-API-Key": "super-secret-guess"})
    assert "auth failed" in caplog.text and "/metrics" in caplog.text
    assert "super-secret-guess" not in caplog.text and KEY not in caplog.text


def test_openapi_declares_the_security_scheme(client):
    spec = client.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"].values()
    assert any(s["type"] == "apiKey" and s["in"] ==
               "header" and s["name"] == "X-API-Key" for s in schemes)
    # docs show the padlock + Authorize button
    assert spec["paths"]["/metrics"]["get"]["security"]


# ---------------------------------------------------------- configuration ----
def test_keys_load_from_both_variables_trimmed_and_deduplicated(monkeypatch):
    monkeypatch.setenv("API_KEYS", " a , b,,a ")
    monkeypatch.setenv("API_KEY", "c")
    assert security.load_api_keys() == ["a", "b", "c"]
    monkeypatch.delenv("API_KEYS")
    monkeypatch.delenv("API_KEY")
    assert security.load_api_keys() == []


def test_startup_check_fails_fast_and_warns(caplog):
    with pytest.raises(RuntimeError, match="genkey"):
        security.check_configuration(keys=[], require=True)
    with caplog.at_level(logging.WARNING, logger="security"):
        security.check_configuration(keys=[], require=False)
        assert "DISABLED" in caplog.text
        caplog.clear()
        security.check_configuration(keys=["short"], require=True)
        assert "shorter" in caplog.text
        caplog.clear()
        security.check_configuration(keys=["x" * 32], require=True)
        assert caplog.text == ""


# ---------------------------------- end to end, in a fresh process each time ----
def run_app(tmp_path, code, **env):
    clean = {k: v for k, v in os.environ.items()
             if k not in ("API_KEY", "API_KEYS", "REQUIRE_API_KEY", "ENABLE_DOCS")}
    clean.update(DATABASE_URL=f"sqlite:///{tmp_path / 's.db'}", **env)
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env=clean)


BOOT = "from fastapi.testclient import TestClient\nfrom app.main import app\nwith TestClient(app) as c:\n"


def test_env_vars_really_switch_authentication_on(tmp_path):
    code = BOOT + \
        "    h = {'X-API-Key': 'k' * 32}\n    print(c.get('/metrics').status_code, c.get('/metrics', headers=h).status_code, c.get('/health').status_code)\n"
    for env in ({"API_KEY": "k" * 32}, {"API_KEYS": "zzzzzzzz," + "k" * 32}):
        result = run_app(tmp_path, code, **env)
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["401", "200", "200"]


def test_no_key_means_open_for_local_dev(tmp_path):
    result = run_app(tmp_path, BOOT +
                     "    print(c.get('/metrics').status_code)\n")
    assert result.returncode == 0 and result.stdout.split() == ["200"]
    assert "authentication is DISABLED" in result.stderr


def test_require_api_key_refuses_to_start_without_one(tmp_path):
    code = BOOT + "    print(c.get('/health').status_code)\n"
    refused = run_app(tmp_path, code, REQUIRE_API_KEY="true")
    assert refused.returncode != 0 and "REQUIRE_API_KEY" in refused.stderr
    started = run_app(tmp_path, code, REQUIRE_API_KEY="true", API_KEY="k" * 32)
    assert started.returncode == 0, started.stderr
    assert started.stdout.split() == ["200"]


def test_docs_can_be_switched_off(tmp_path):
    code = BOOT + \
        "    print(c.get('/docs').status_code, c.get('/openapi.json').status_code, c.get('/health').status_code)\n"
    assert run_app(tmp_path, code).stdout.split() == ["200", "200", "200"]
    assert run_app(tmp_path, code, ENABLE_DOCS="false").stdout.split() == [
        "404", "404", "200"]


def test_genkey_prints_a_strong_random_key():
    keys = [subprocess.run([sys.executable, "-m", "app.cli", "genkey"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
            for _ in range(2)]
    assert keys[0] != keys[1]
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", k)
               for k in keys)  # 256 bits, URL-safe
