"""Providers and their API keys: who can authenticate, as whom, and what a key can reach."""
import hashlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.security import api_keys as security
from app.db.engine import engine
from app.db import models
from conftest import make_provider

ROOT = os.path.dirname(os.path.dirname(__file__))


# --------------------------------------------------------- creating providers ----
def test_superuser_creates_a_provider_and_sees_the_key_once(client, su):
    resp = client.post("/v1/admin/providers",
                       json={"full_name": "  Carla Cruz  "}, headers=su)
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"].startswith("prov_") and body["full_name"] == "Carla Cruz"
    assert body["is_active"] is True and body["is_system"] is False
    assert len(body["api_key"]) >= 40 and "shown once" in body["note"]
    assert body["key"]["label"] == "initial" and body["key"]["key_prefix"] == body["api_key"][:8]
    assert "api_key" not in body["key"]

    listed = client.get("/v1/admin/providers", headers=su).json()["items"]
    mine = next(p for p in listed if p["id"] == body["id"])
    assert mine["config_count"] == 0 and len(mine["keys"]) == 1
    assert body["api_key"] not in json.dumps(
        listed)  # the secret is never listed again


def test_only_a_hash_of_the_key_is_stored(client, su):
    p = make_provider(client, su, "Dana Diaz")
    with engine.connect() as conn:
        rows = conn.execute(select(models.registry_api_keys.c.key_hash, models.registry_api_keys.c.key_prefix)
                            .where(models.registry_api_keys.c.provider_id == p["id"])).all()
    assert len(rows) == 1
    assert rows[0][0] == hashlib.sha256(p["api_key"].encode()).hexdigest()
    assert p["api_key"] not in rows[0][0] and rows[0][1] == p["api_key"][:8]


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_provider_full_name_is_validated(client, su, name):
    resp = client.post("/v1/admin/providers",
                       json={"full_name": name}, headers=su)
    assert resp.status_code == 422 and resp.json(
    )["error"]["code"] == "invalid_provider"


def test_a_taken_full_name_is_a_409_not_a_second_provider(client, su):
    a = make_provider(client, su, "Twin")
    again = client.post("/v1/admin/providers",
                        json={"full_name": "Twin"}, headers=su)
    assert again.status_code == 409
    err = again.json()["error"]
    assert err["code"] == "already_exists" and a["id"] in err["message"]

    # names are compared case- and whitespace-insensitively
    for variant in ("  twin  ", "TWIN", "\tTwin\n"):
        assert client.post("/v1/admin/providers",
                           json={"full_name": variant}, headers=su).status_code == 409

    twins = [p for p in client.get("/v1/admin/providers", headers=su).json()["items"]
             if p["full_name"] == "Twin"]
    assert [t["id"] for t in twins] == [a["id"]]  # still exactly one


def test_on_exists_reuse_is_idempotent_and_issues_no_key(client, su):
    first = client.post("/v1/admin/providers",
                        json={"full_name": "Retryable"}, headers=su).json()
    assert first["created"] is True and first["api_key"]

    # the same request again: same provider, nothing created, and no new secret
    second = client.post("/v1/admin/providers", params={"on_exists": "reuse"},
                         json={"full_name": "Retryable"}, headers=su)
    assert second.status_code == 200
    body = second.json()
    assert body["created"] is False and body["id"] == first["id"]
    assert body["api_key"] is None and body["key"] is None
    assert first["id"] in body["note"]  # says where to get a key

    listed = next(p for p in client.get("/v1/admin/providers", headers=su).json()["items"]
                  if p["id"] == first["id"])
    assert len(listed["keys"]) == 1  # the retry did not mint a second key
    # and the first key still works
    assert client.get(
        "/v1/me", headers={"X-API-Key": first["api_key"]}).json()["id"] == first["id"]

    # a still-unused name is created normally through the same call
    fresh = client.post("/v1/admin/providers", params={"on_exists": "reuse"},
                        json={"full_name": "Unused"}, headers=su)
    assert fresh.status_code == 201 and fresh.json()["created"] is True


def test_names_that_look_identical_are_still_one_provider(client, su):
    """Characters that render as nothing must not buy you a second copy of a name."""
    client.post("/v1/admin/providers",
                json={"full_name": "Uni Form"}, headers=su)
    # Built with chr() rather than literals: these characters are invisible in an
    # editor and get silently mangled when written out by hand. Each variant spells
    # "Uni Form" using a different separator, so they must all normalise to the same name.
    ZWSP, ZWNJ, BOM, NBSP = chr(0x200B), chr(0x200C), chr(0xFEFF), chr(0x00A0)
    for variant in (
        BOM + "Uni Form",       # BOM: invisible, and str.strip() does not remove it
        "Uni" + ZWSP + " Form",  # zero-width space used as the space
        "Uni " + ZWNJ + "Form",
        "UNI   FORM",           # case + a run of spaces collapsed to one
        "Uni\tForm\n",
        "Uni" + NBSP + "Form",   # non-breaking space
    ):
        assert client.post("/v1/admin/providers",
                           json={"full_name": variant},
                           headers=su).status_code == 409, ascii(variant)
    # the ligature and full-width spellings of a *different* name normalise to the same thing
    client.post("/v1/admin/providers",
                json={"full_name": "fin co"}, headers=su)
    for variant in (chr(0xFB01) + "n co",     # the "fi" ligature
                    "".join(chr(c) for c in (0xFF26, 0xFF29, 0xFF4E)) + " co"):
        assert client.post("/v1/admin/providers",
                           json={"full_name": variant},
                           headers=su).status_code == 409, ascii(variant)
    names = [p["full_name"] for p in client.get(
        "/v1/admin/providers", headers=su).json()["items"]]
    assert names.count("Uni Form") == 1 and names.count("fin co") == 1


def test_concurrent_creates_of_one_name_make_exactly_one_provider(client, su):
    """The uniqueness rule must survive a race.

    The old check was `SELECT` all names, compare in Python, then `INSERT` -- a
    read-then-write gap, so simultaneous requests all saw the name as free and all
    inserted. This pins the guarantee that one name means one provider, and that the
    losers get a clean 409 rather than a 500.
    """
    barrier = threading.Barrier(6)

    def create():
        barrier.wait()  # fire all six at the same instant
        return client.post("/v1/admin/providers",
                           json={"full_name": "Race Condition"},
                           headers=su).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(lambda _: create(), range(6)))

    assert sorted(codes) == [201, 409, 409, 409, 409, 409], codes
    listed = client.get("/v1/admin/providers", headers=su).json()["items"]
    assert [p["full_name"] for p in listed].count("Race Condition") == 1


def test_the_database_itself_refuses_a_duplicate_name(client, su):
    """Belt and braces: the rule is in the schema, not only in the service layer."""
    made = client.post("/v1/admin/providers",
                       json={"full_name": "Schema Guard"}, headers=su).json()
    with engine.begin() as conn:
        # Bypass registry entirely -- a direct insert of the same name_key must fail.
        with pytest.raises(Exception):
            conn.execute(
                models.registry_providers.insert().values(
                    id="prov_sneaky", full_name="schema guard",
                    name_key="schema guard", is_active=True,
                    is_system=False, created_at=datetime.now(timezone.utc)))
    assert client.get("/v1/admin/providers", headers=su).json()["items"]
    assert made["id"].startswith("prov_")


def test_renaming_onto_a_taken_name_is_a_409(client, su):
    a, b = make_provider(client, su, "Nina Nunez"), make_provider(
        client, su, "Omar Oden")
    clash = client.patch(f"/v1/admin/providers/{a['id']}",
                         json={"full_name": b["full_name"]}, headers=su)
    assert clash.status_code == 409 and clash.json(
    )["error"]["code"] == "already_exists"
    # unchanged, and a rename to a free name (or its own) still works
    assert client.get("/v1/admin/providers", headers=su).json() is not None
    me = {"X-API-Key": a["api_key"]}
    assert client.get("/v1/me", headers=me).json()["full_name"] == "Nina Nunez"
    assert client.patch(f"/v1/admin/providers/{a['id']}",
                        json={"full_name": "Nina N."}, headers=su).status_code == 200
    assert client.patch(f"/v1/admin/providers/{a['id']}",
                        # its own name
                        json={"full_name": "Nina Nunez"}, headers=su).status_code == 200


# ------------------------------------------------------------------ identity ----
def test_me_reports_who_the_key_belongs_to(client, su, alice):
    me = client.get("/v1/me", headers=alice["h"]).json()
    assert (me["role"], me["id"], me["full_name"]) == (
        "provider", alice["id"], "Alice Almeida")
    assert me["limits"]["max_configs"] >= 1 and me["configs_owned"] >= 0

    root = client.get("/v1/me", headers=su).json()
    assert (root["role"], root["id"]) == ("superuser", None)


def test_last_used_is_recorded_after_a_key_is_used(client, su):
    p = make_provider(client, su, "Ivy Ibarra")
    def key(): return next(k for pr in client.get("/v1/admin/providers", headers=su).json()["items"]
                           if pr["id"] == p["id"] for k in pr["keys"])
    assert key()["last_used_at"] is None
    client.get("/v1/me", headers=p["h"])
    assert key()["last_used_at"] is not None


# ------------------------------------------------------- authentication ----
def test_unknown_keys_are_rejected(client, authed):
    for headers in ({}, {"X-API-Key": "nope"}, {"X-API-Key": "x" * 43}):
        resp = client.get("/v1/me", headers=headers)
        assert resp.status_code == 401 and resp.json(
        )["error"]["code"] == "unauthorized"


def test_a_revoked_key_stops_working_but_a_second_key_keeps_the_provider_in(client, su):
    p = make_provider(client, su, "Eli Estrada")
    second = client.post(
        f"/v1/admin/providers/{p['id']}/keys", json={"label": "ci"}, headers=su)
    assert second.status_code == 201 and second.json()["key"]["label"] == "ci"
    new_h = {"X-API-Key": second.json()["api_key"]}

    assert client.get("/v1/me", headers=p["h"]).status_code == 200
    revoked = client.delete(
        f"/v1/admin/providers/{p['id']}/keys/{p['key_id']}", headers=su)
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    assert client.get(
        "/v1/me", headers=p["h"]).status_code == 401   # rotated out
    # the new key works
    assert client.get("/v1/me", headers=new_h).json()["id"] == p["id"]

    assert client.delete(
        f"/v1/admin/providers/{p['id']}/keys/key_nope", headers=su).status_code == 404
    other = make_provider(client, su, "Someone else")
    assert client.delete(f"/v1/admin/providers/{other['id']}/keys/{second.json()['key']['id']}",
                         headers=su).status_code == 404  # a key can only be revoked via its own provider


def test_deactivating_a_provider_locks_its_keys_and_reactivating_restores_them(client, su):
    p = make_provider(client, su, "Fay Fabian")
    off = client.patch(
        f"/v1/admin/providers/{p['id']}", json={"is_active": False}, headers=su)
    assert off.status_code == 200 and off.json()["is_active"] is False
    assert client.get("/v1/me", headers=p["h"]).status_code == 401
    client.patch(f"/v1/admin/providers/{p['id']}", json={
                 "is_active": True, "full_name": "Fay F."}, headers=su)
    assert client.get("/v1/me", headers=p["h"]).json()["full_name"] == "Fay F."


def test_the_built_in_system_provider_cannot_be_modified_or_given_a_key(client, su):
    assert client.patch("/v1/admin/providers/prov_system",
                        json={"is_active": False}, headers=su).status_code == 409
    assert client.post("/v1/admin/providers/prov_system/keys",
                       headers=su).status_code == 409
    assert client.post("/v1/admin/providers/prov_nope/keys",
                       headers=su).status_code == 404


# -------------------------------------------- what a provider key can reach ----
def test_provider_keys_cannot_use_superuser_routes(client, alice):
    for method, path, body in (
        ("POST", "/v1/admin/providers", {"full_name": "x"}),
        ("GET", "/v1/admin/providers", None),
        ("PATCH", f"/v1/admin/providers/{alice['id']}", {"is_active": False}),
        ("POST", f"/v1/admin/providers/{alice['id']}/keys", None),
    ):
        resp = client.request(method, path, json=body, headers=alice["h"])
        assert resp.status_code == 403 and resp.json(
        )["error"]["code"] == "forbidden", (method, path)


def test_provider_keys_cannot_reach_any_legacy_route(client, alice):
    legacy = [(m, r.path.replace("{item_id}", "x")) for r in client.app.routes if isinstance(r, APIRoute)
              and not r.path.startswith("/v1") and r.path not in security.PUBLIC_PATHS for m in sorted(r.methods)]
    assert len(legacy) >= 20
    for method, path in legacy:
        resp = client.request(method, path, headers=alice["h"])
        assert resp.status_code == 403, f"{method} {path} answered {resp.status_code} to a provider key"
    assert client.get("/health").status_code == 200  # still public


def test_the_superuser_still_reaches_the_legacy_routes(client, su):
    for path in ("/entities", "/metrics", "/orders?limit=1", "/ddl", "/export"):
        assert client.get(path, headers=su).status_code == 200, path


# ----------------------------- end to end, in a fresh process each time ----
def run_app(tmp_path, code, **env):
    clean = {k: v for k, v in os.environ.items()
             if k not in ("API_KEY", "API_KEYS", "REQUIRE_API_KEY", "ENABLE_DOCS", "TEST_DATABASE_URL")}
    clean.update(DATABASE_URL=f"sqlite:///{tmp_path / 's.db'}", **env)
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env=clean)


BOOT = "from fastapi.testclient import TestClient\nfrom app.main import app\nwith TestClient(app) as c:\n"


def test_with_no_superuser_key_authentication_is_off_and_everyone_is_the_superuser(tmp_path):
    result = run_app(
        tmp_path, BOOT + "    print(c.get('/v1/me').json()['role'], c.get('/v1/configs').status_code)\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["superuser", "200"]


def test_providers_projects_configs_and_keys_survive_a_restart(tmp_path):
    k = "k" * 32
    first = BOOT + (
        "    h = {'X-API-Key': '%s'}\n"
        "    p = c.post('/v1/admin/providers', json={'full_name': 'Gus'}, headers=h).json()\n"
        "    ph = {'X-API-Key': p['api_key']}\n"
        "    proj = c.post('/v1/projects', json={'name': 'vault', 'is_only_me': True}, headers=ph).json()\n"
        "    cfg = {'entity': 'things', 'fields': {'id': {'type': 'uuid', 'primary_key': True}, 'label': {'type': 'string'}},\n"
        "           'seed': {'initial_count': 4}}\n"
        "    made = c.post('/v1/projects/' + proj['id'] + '/configs', json=cfg, headers=ph).json()\n"
        "    print(p['api_key'], made['id'], made['seeded_rows'], proj['id'])\n"
    ) % k
    out = run_app(tmp_path, first, API_KEY=k)
    assert out.returncode == 0, out.stderr
    key, config_id, seeded, project_id = out.stdout.split()
    assert seeded == "4"

    second = BOOT + (
        "    ph = {'X-API-Key': '%s'}\n"
        "    me = c.get('/v1/me', headers=ph).json()\n"
        "    rows = c.get('/v1/configs/%s/data', headers=ph).json()['items']\n"
        "    nobody = c.get('/v1/configs/%s/data', headers={'X-API-Key': 'k' * 32}).status_code\n"
        "    from app import main\n"
        "    from app.services.catalog import catalog\n"
        "    job = main._scheduler.get_job('job_' + catalog.entries['%s'].physical) is not None\n"
        "    proj = c.get('/v1/projects/%s', headers=ph).json()\n"
        "    stranger = c.get('/v1/projects/%s', headers={'X-API-Key': 'k' * 32}).json()\n"
        "    print(me['full_name'], me['projects_owned'], me['configs_owned'], len(rows), nobody, job,\n"
        "          proj['is_only_me'], proj['config_count'], stranger['name'])\n"
    ) % (key, config_id, config_id, config_id, project_id, project_id)
    again = run_app(tmp_path, second, API_KEY=k)
    assert again.returncode == 0, again.stderr
    # name, projects and configs owned, rows intact, the superuser key can still read it, its scheduler
    # job was re-registered, and the project is still private with its one config
    assert again.stdout.split() == [
        "Gus", "1", "1", "4", "200", "True", "True", "1", "vault"]


def test_cli_create_provider_prints_a_working_key_once(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in (
        "API_KEY", "API_KEYS", "TEST_DATABASE_URL")}
    env["DATABASE_URL"] = f"sqlite:///{tmp_path / 'c.db'}"
    done = subprocess.run([sys.executable, "-m", "app.cli", "create-provider", "--full-name", "Hana Hidalgo"],
                          cwd=ROOT, capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    made = json.loads(done.stdout)
    assert made["full_name"] == "Hana Hidalgo" and made["id"].startswith(
        "prov_") and len(made["api_key"]) >= 40

    check = BOOT + \
        f"    print(c.get('/v1/me', headers={{'X-API-Key': '{made['api_key']}'}}).json()['id'])\n"
    out = subprocess.run([sys.executable, "-c", check], cwd=ROOT, capture_output=True, text=True,
                         env={**env, "API_KEY": "k" * 32})
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == [made["id"]]


def test_a_malformed_quota_variable_stops_startup_with_a_clear_message(tmp_path):
    result = run_app(tmp_path, BOOT + "    print('up')\n",
                     MAX_ROWS_PER_CONFIG="lots")
    assert result.returncode != 0 and "MAX_ROWS_PER_CONFIG must be an integer" in result.stderr
    assert run_app(tmp_path, BOOT + "    print('up')\n",
                   MAX_ROWS_PER_CONFIG="1000").stdout.split() == ["up"]
