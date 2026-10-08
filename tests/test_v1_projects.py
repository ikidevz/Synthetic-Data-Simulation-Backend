"""Projects: a project is a schema, a config is a table.

  create / list / get / rename / share / delete       the project itself
  quotas                                              projects per provider, configs per project
  one schema = one scope                              refs, bulk refresh and bundles stay inside it
  the project as a schema                             /ddl and /export for the whole project
  migration                                           a database from before projects
"""
import io
import json
import re
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from conftest import customers_cfg, make_project, make_provider, orders_cfg, publish

NAMESPACED = re.compile(r"\bd_[0-9a-f]{10}_")


def project_names(client, who, scope="all"):
    # a provider dict, or the superuser's bare headers
    headers = who["h"] if "h" in who else who
    return {p["name"] for p in client.get(
        f"/v1/projects?scope={scope}", headers=headers).json()["items"]}


# ----------------------------------------------------------------- creating ----
def test_create_returns_an_empty_project_owned_by_the_caller(client, alice):
    made = make_project(client, alice, "p_basic", description="Shop tables")
    assert made["id"].startswith("prj_") and made["name"] == "p_basic"
    assert made["owner"] == {"id": alice["id"], "full_name": "Alice Almeida"}
    assert made["description"] == "Shop tables" and made["is_only_me"] is False
    assert made["source"] == "api" and made["can_write"] is True
    assert made["config_count"] == 0 and made["configs"] == []


@pytest.mark.parametrize("body, fragment", [
    ({}, "name must be"),
    ({"name": "Bad Name"}, "name must be"),
    ({"name": "x" * 33}, "name must be"),
    ({"name": "9lives"}, "name must be"),
    ({"name": "schema"}, "reserved"),
    ({"name": "ok", "typo": 1}, "unknown key"),
    ({"name": "ok", "is_only_me": "yes"}, "is_only_me"),
    ({"name": "ok", "description": "d" * 501}, "description"),
    ({"name": "ok", "description": 5}, "description"),
])
def test_bad_projects_are_rejected_with_a_clear_reason(client, alice, body, fragment):
    resp = client.post("/v1/projects", json=body, headers=alice["h"])
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "invalid_project" and fragment in err["message"], err
    assert "ok" not in project_names(client, alice, "mine")


def test_a_project_name_is_unique_per_provider_not_global(client, alice, bob):
    make_project(client, alice, "p_unique")
    dupe = client.post(
        "/v1/projects", json={"name": "p_unique"}, headers=alice["h"])
    assert dupe.status_code == 409 and dupe.json(
    )["error"]["code"] == "already_exists"
    # Bob has his own schema of that name
    make_project(client, bob, "p_unique")


# -------------------------------------------------------------------- listing ----
def test_listing_and_reading_projects_follows_visibility(client, su, alice, bob):
    shared = make_project(client, alice, "p_list_shared")
    hidden = make_project(client, alice, "p_list_hidden", is_only_me=True)
    assert {"p_list_shared", "p_list_hidden"} <= project_names(
        client, alice, "mine")
    assert "p_list_shared" in project_names(client, bob, "shared")
    assert not {"p_list_hidden"} & project_names(client, bob)
    assert {"p_list_shared", "p_list_hidden"} <= project_names(client, su)
    assert client.get(
        f"/v1/projects/{shared['id']}", headers=bob["h"]).status_code == 200
    ghost = client.get("/v1/projects/prj_000000000000", headers=bob["h"])
    real = client.get(f"/v1/projects/{hidden['id']}", headers=bob["h"])
    assert (ghost.status_code, ghost.json()["error"]["code"]) == (
        real.status_code, real.json()["error"]["code"]) == (404, "not_found")


def test_the_built_in_examples_are_one_read_only_project(client, alice):
    items = {p["name"]: p for p in client.get(
        "/v1/projects?scope=shared", headers=alice["h"]).json()["items"]}
    examples = items["examples"]
    assert examples["id"] == "prj_system_examples" and examples["source"] == "yaml"
    assert examples["owner"]["id"] == "prov_system" and examples["can_write"] is False
    detail = client.get("/v1/projects/prj_system_examples",
                        headers=alice["h"]).json()
    assert {"customers", "orders", "support_tickets"} <= {
        c["name"] for c in detail["configs"]}
    cfg = client.get("/v1/configs/cfg_sys_orders", headers=alice["h"]).json()
    assert cfg["project"]["id"] == "prj_system_examples"


def test_configs_list_by_project(client, alice):
    a = make_project(client, alice, "p_by_a")
    b = make_project(client, alice, "p_by_b")
    publish(client, alice, customers_cfg("in_a"), project=a["id"])
    publish(client, alice, customers_cfg("in_b1"), project=b["id"])
    publish(client, alice, customers_cfg("in_b2"), project=b["id"])
    listed = client.get(
        f"/v1/projects/{b['id']}/configs", headers=alice["h"]).json()
    assert listed["project"]["name"] == "p_by_b"
    assert [i["name"] for i in listed["items"]] == ["in_b1", "in_b2"]
    narrowed = client.get(
        f"/v1/configs?project_id={a['id']}", headers=alice["h"]).json()["items"]
    assert [i["name"] for i in narrowed] == ["in_a"]
    assert client.get(
        f"/v1/projects/{b['id']}", headers=alice["h"]).json()["config_count"] == 2


# --------------------------------------------------------------- patch / rename ----
def test_patch_renames_describes_and_shares_without_touching_the_data(client, alice, bob):
    project = make_project(client, alice, "p_patch")
    made = publish(client, alice, customers_cfg(
        "p_patch_cfg", count=4), project=project["id"])
    resp = client.patch(f"/v1/projects/{project['id']}", headers=alice["h"],
                        json={"name": "p_patched", "description": "now described", "is_only_me": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["name"], body["description"], body["is_only_me"]) == (
        "p_patched", "now described", True)
    assert client.get(
        f"/v1/configs/{made['id']}", headers=bob["h"]).status_code == 404
    rows = client.get(
        f"/v1/configs/{made['id']}/data?limit=50", headers=alice["h"]).json()["items"]
    assert len(rows) == 4
    assert client.get(
        f"/v1/configs/{made['id']}", headers=alice["h"]).json()["project"]["name"] == "p_patched"


@pytest.mark.parametrize("body", [{}, {"nope": 1}, {"name": "Bad Name"}, {"is_only_me": "yes"}])
def test_patch_rejects_bad_requests(client, alice, body):
    project = make_project(
        client, alice, f"p_pbad_{abs(hash(str(body))) % 10**6}")
    assert client.patch(
        f"/v1/projects/{project['id']}", json=body, headers=alice["h"]).status_code == 422


def test_renaming_onto_a_taken_name_is_refused(client, alice):
    make_project(client, alice, "p_taken")
    other = make_project(client, alice, "p_wants_it")
    resp = client.patch(
        f"/v1/projects/{other['id']}", json={"name": "p_taken"}, headers=alice["h"])
    assert resp.status_code == 409 and resp.json(
    )["error"]["code"] == "already_exists"
    assert client.patch(f"/v1/projects/{other['id']}", json={"name": "p_wants_it"},
                        # its own name is not a clash
                        headers=alice["h"]).status_code == 200


def test_only_the_owner_can_change_or_delete_a_project(client, alice, bob):
    project = make_project(client, alice, "p_guarded")
    for method, kwargs in (("PATCH", {"json": {"description": "x"}}), ("DELETE", {"params": {"confirm": "true"}})):
        resp = client.request(
            method, f"/v1/projects/{project['id']}", headers=bob["h"], **kwargs)
        assert resp.status_code == 403 and resp.json(
        )["error"]["code"] == "forbidden", method
    assert "p_guarded" in project_names(client, alice, "mine")


# ----------------------------------------------------------------------- delete ----
def test_deleting_a_project_deletes_its_configs_data_and_jobs_children_first(client, alice):
    project = make_project(client, alice, "p_doomed")
    parent = publish(client, alice, customers_cfg(
        "doomed_parent", count=3), project=project["id"])
    child = publish(client, alice, orders_cfg("doomed_parent",
                    name="doomed_child", count=5), project=project["id"])
    from sqlalchemy import inspect
    from app.db.engine import engine
    from app.services.catalog import catalog
    physical = [catalog.entries[parent["id"]].physical,
                catalog.entries[child["id"]].physical]
    assert all(p in inspect(engine).get_table_names() for p in physical)
    jobs = [catalog.scheduler.get_job(f"job_{p}") for p in physical]
    assert all(jobs)

    refused = client.delete(
        f"/v1/projects/{project['id']}", headers=alice["h"])
    assert refused.status_code == 400 and refused.json(
    )["error"]["code"] == "confirmation_required"
    assert client.get(
        # untouched
        f"/v1/configs/{child['id']}", headers=alice["h"]).status_code == 200

    done = client.delete(
        f"/v1/projects/{project['id']}?confirm=true", headers=alice["h"])
    assert done.status_code == 200, done.text
    assert done.json() == {
        "deleted": True, "id": project["id"], "name": "p_doomed", "configs_deleted": 2}
    names = set(inspect(engine).get_table_names())
    assert not set(physical) & names
    assert not any(catalog.scheduler.get_job(f"job_{p}") for p in physical)
    for cid in (parent["id"], child["id"]):
        assert client.get(f"/v1/configs/{cid}",
                          headers=alice["h"]).status_code == 404
    assert client.get(
        f"/v1/projects/{project['id']}", headers=alice["h"]).status_code == 404
    assert project["id"] not in catalog.projects and project["id"] not in catalog.scopes
    make_project(client, alice, "p_doomed")  # the name is free again


def test_deleting_an_empty_project_works_and_frees_a_quota_slot(client, alice):
    project = make_project(client, alice, "p_empty")
    done = client.delete(
        f"/v1/projects/{project['id']}?confirm=true", headers=alice["h"])
    assert done.status_code == 200 and done.json()["configs_deleted"] == 0


def test_a_project_delete_never_reaches_another_projects_tables(client, alice, bob):
    a = make_project(client, alice, "p_iso")
    b = make_project(client, bob, "p_iso")
    keep = publish(client, bob, customers_cfg(
        "iso_table", count=6), project=b["id"])
    publish(client, alice, customers_cfg(
        "iso_table", count=2), project=a["id"])
    assert client.delete(
        f"/v1/projects/{a['id']}?confirm=true", headers=alice["h"]).status_code == 200
    rows = client.get(
        f"/v1/configs/{keep['id']}/data?limit=50", headers=bob["h"]).json()["items"]
    assert len(rows) == 6


# ----------------------------------------------------------------------- quotas ----
def test_project_and_config_quotas(client, su, monkeypatch):
    p = make_provider(client, su, "Quota Pat")
    monkeypatch.setenv("MAX_PROJECTS_PER_PROVIDER", "2")
    monkeypatch.setenv("MAX_CONFIGS_PER_PROJECT", "2")
    monkeypatch.setenv("MAX_CONFIGS_PER_PROVIDER", "3")
    one = make_project(client, p, "q_one")
    two = make_project(client, p, "q_two")
    third = client.post(
        "/v1/projects", json={"name": "q_three"}, headers=p["h"])
    assert third.status_code == 409 and third.json(
    )["error"]["code"] == "quota_exceeded"
    assert "2 projects" in third.json()["error"]["message"]

    publish(client, p, customers_cfg("q_a"), project=one["id"])
    publish(client, p, customers_cfg("q_b"), project=one["id"])
    full = client.post(
        f"/v1/projects/{one['id']}/configs", json=customers_cfg("q_c"), headers=p["h"])
    assert full.status_code == 409 and "at most 2 configs" in full.json()[
        "error"]["message"]

    # 3 in total: the provider ceiling
    publish(client, p, customers_cfg("q_d"), project=two["id"])
    over = client.post(
        f"/v1/projects/{two['id']}/configs", json=customers_cfg("q_e"), headers=p["h"])
    assert over.status_code == 409 and "across all its projects" in over.json()[
        "error"]["message"]

    # deleting a whole project frees every slot it held
    assert client.delete(
        f"/v1/projects/{one['id']}?confirm=true", headers=p["h"]).status_code == 200
    publish(client, p, customers_cfg("q_e"), project=two["id"])
    again = make_project(client, p, "q_three")
    assert again["config_count"] == 0

    me = client.get("/v1/me", headers=p["h"]).json()
    assert me["projects_owned"] == 2 and me["configs_owned"] == 2
    assert me["limits"]["max_projects"] == 2 and me["limits"]["max_configs_per_project"] == 2


# ------------------------------------------------- one project = one schema/scope ----
def test_bulk_refresh_stays_inside_the_project(client, alice):
    """The same names in two of one provider's projects are separate tables: replacing one
    parent refreshes only the dependents in ITS project."""
    a = make_project(client, alice, "p_scope_a")
    b = make_project(client, alice, "p_scope_b")
    a_parent = publish(client, alice, customers_cfg(
        "sc_parent", count=4), project=a["id"])
    a_child = publish(client, alice, orders_cfg(
        "sc_parent", name="sc_child", count=6), project=a["id"])
    b_parent = publish(client, alice, customers_cfg(
        "sc_parent", count=5), project=b["id"])
    b_child = publish(client, alice, orders_cfg(
        "sc_parent", name="sc_child", count=7), project=b["id"])

    def count(made):
        return client.get(f"/v1/configs/{made['id']}/metrics", headers=alice["h"]).json()["row_count"]

    resp = client.post(f"/v1/configs/{b_parent['id']}/batch", headers=alice["h"],
                       json={"mode": "replace", "confirm": True, "count": 9})
    assert resp.status_code == 200, resp.text
    assert resp.json()["auto_included"] == [
        "sc_child"] and not NAMESPACED.search(resp.text)
    assert (count(a_parent), count(a_child)) == (4, 6)
    assert count(b_parent) == 9 and count(b_child) == 7
    # rows only ever point at parents from their own project
    a_ids = {r["customer_id"] for r in client.get(
        f"/v1/configs/{a_parent['id']}/data?limit=100", headers=alice["h"]).json()["items"]}
    for _ in range(5):
        row = client.post(
            f"/v1/configs/{b_child['id']}/data", json={}, headers=alice["h"]).json()
        assert row["customer_id"] not in a_ids


def test_a_row_cannot_reference_a_row_in_another_project(client, alice):
    a = make_project(client, alice, "p_xr_a")
    b = make_project(client, alice, "p_xr_b")
    a_parent = publish(client, alice, customers_cfg(
        "xr_parent", count=3), project=a["id"])
    publish(client, alice, customers_cfg(
        "xr_parent", count=3), project=b["id"])
    b_child = publish(client, alice, orders_cfg(
        "xr_parent", name="xr_child"), project=b["id"])
    foreign = client.get(
        f"/v1/configs/{a_parent['id']}/data?limit=1", headers=alice["h"]).json()["items"][0]
    resp = client.post(f"/v1/configs/{b_child['id']}/data",
                       json={"customer_id": foreign["customer_id"]}, headers=alice["h"])
    assert resp.status_code == 422 and resp.json(
    )["error"]["code"] == "invalid_reference"


# ------------------------------------------------------ the project as a schema ----
@pytest.fixture(scope="module")
def shop(client, alice):
    project = make_project(client, alice, "shop", description="A tiny shop")
    customers = publish(client, alice, customers_cfg(
        "customers", count=3), project=project["id"])
    orders = publish(client, alice, orders_cfg(
        "customers", name="orders", count=4), project=project["id"])
    return {"project": project, "customers": customers, "orders": orders}


def test_project_ddl_is_the_whole_schema_parents_first(client, alice, bob, shop):
    url = f"/v1/projects/{shop['project']['id']}/ddl"
    for who in (alice, bob):  # a public project is readable by others
        text = client.get(url, headers=who["h"]).text
        assert not NAMESPACED.search(text)
        assert text.index("CREATE TABLE IF NOT EXISTS customers") < text.index(
            "CREATE TABLE IF NOT EXISTS orders")
        # SQLite has no schemas
        assert "-- Project: shop" in text and "CREATE SCHEMA" not in text


def test_project_ddl_on_postgresql_creates_a_real_schema(client, alice, shop):
    text = client.get(
        f"/v1/projects/{shop['project']['id']}/ddl?dialect=postgresql", headers=alice["h"]).text
    assert "CREATE SCHEMA IF NOT EXISTS shop;" in text and "SET search_path TO shop;" in text
    assert text.index("CREATE SCHEMA") < text.index(
        "CREATE TABLE IF NOT EXISTS customers")
    assert not NAMESPACED.search(text)


def test_a_projects_ddl_runs_on_its_own_and_matches_the_data(client, alice, shop, tmp_path):
    text = client.get(
        f"/v1/projects/{shop['project']['id']}/ddl", headers=alice["h"]).text
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(text)
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"customers", "orders"} <= tables
    fk = db.execute("PRAGMA foreign_key_list(orders)").fetchall()
    assert fk and fk[0][2] == "customers"


def test_project_export_is_a_zip_of_every_table(client, alice, bob, shop):
    url = f"/v1/projects/{shop['project']['id']}/export"
    resp = client.get(url + "?format=sql", headers=bob["h"])
    assert resp.status_code == 200
    assert 'filename="shop-export.zip"' in resp.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert sorted(zf.namelist()) == [
        "customers.sql", "manifest.json", "orders.sql", "schema.sql"]
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["project"] == {"id": shop["project"]["id"], "name": "shop"}
    assert manifest["load_order"] == ["customers", "orders"]
    assert {f["entity"]: f["rows"]
            for f in manifest["files"]} == {"customers": 3, "orders": 4}
    assert not NAMESPACED.search(
        "".join(zf.read(n).decode() for n in zf.namelist()))
    # the schema + data load into a database with foreign keys enforced
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    for name in ("schema.sql", "customers.sql", "orders.sql"):
        db.executescript(zf.read(name).decode())
    assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 4


def test_project_export_on_postgresql_names_the_schema(client, alice, shop):
    resp = client.get(
        f"/v1/projects/{shop['project']['id']}/export?dialect=postgresql", headers=alice["h"])
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert "SET search_path TO shop;" in zf.read("schema.sql").decode()
    assert "search_path TO shop" in json.loads(
        zf.read("manifest.json"))["handoff"]


def test_project_export_can_mix_changes_and_snapshots(client, alice, shop):
    orders = shop["orders"]["id"]
    resp = client.get(
        f"/v1/projects/{shop['project']['id']}/export?since={orders}=2", headers=alice["h"])
    assert resp.status_code == 200, resp.text
    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert "orders.changes.csv" in names and "customers.csv" in names
    stray = client.get(
        f"/v1/projects/{shop['project']['id']}/export?since=cfg_nope=1", headers=alice["h"])
    assert stray.status_code == 422 and stray.json(
    )["error"]["code"] == "since_not_targeted"


def test_an_empty_project_has_nothing_to_export_but_still_has_a_ddl(client, alice):
    project = make_project(client, alice, "p_nothing")
    resp = client.get(
        f"/v1/projects/{project['id']}/export", headers=alice["h"])
    assert resp.status_code == 409 and resp.json(
    )["error"]["code"] == "empty_project"
    ddl = client.get(
        f"/v1/projects/{project['id']}/ddl?dialect=postgresql", headers=alice["h"])
    assert ddl.status_code == 200 and "CREATE SCHEMA IF NOT EXISTS p_nothing;" in ddl.text
    assert "CREATE TABLE" not in ddl.text


def test_the_builtin_examples_export_as_a_project_too(client, alice):
    resp = client.get(
        "/v1/projects/prj_system_examples/export", headers=alice["h"])
    assert resp.status_code == 200
    names = set(zipfile.ZipFile(io.BytesIO(resp.content)).namelist())
    assert {"customers.csv", "orders.csv", "support_tickets.csv",
            "schema.sql", "manifest.json"} <= names


# -------------------------------------------------------------------- migration ----
LEGACY_REGISTRY = """
CREATE TABLE registry_providers (
    id VARCHAR PRIMARY KEY, full_name VARCHAR NOT NULL, name_key VARCHAR,
    is_active BOOLEAN NOT NULL, is_system BOOLEAN NOT NULL, created_at DATETIME NOT NULL);
CREATE TABLE registry_configs (
    id VARCHAR PRIMARY KEY, provider_id VARCHAR NOT NULL REFERENCES registry_providers (id),
    name VARCHAR NOT NULL, physical_name VARCHAR NOT NULL UNIQUE, config_json TEXT NOT NULL,
    is_only_me BOOLEAN NOT NULL, source VARCHAR NOT NULL,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
    CONSTRAINT uq_registry_configs_provider_name UNIQUE (provider_id, name));
CREATE INDEX ix_registry_configs_provider_id ON registry_configs (provider_id);
"""


def _legacy_db(path):
    """A database as the previous version left it: a provider-owned config with its own is_only_me."""
    db = sqlite3.connect(path)
    db.executescript(LEGACY_REGISTRY)
    now = "2025-01-01 00:00:00.000000"
    db.executemany("INSERT INTO registry_providers VALUES (?,?,?,?,?,?)", [
        ("prov_system", "System (built-in examples)",
         "system (built-in examples)", 1, 1, now),
        ("prov_old", "Olga Old", "olga old", 1, 0, now),
        ("prov_mixed", "Max Mixed", "max mixed", 1, 0, now)])
    cfg = json.dumps({"entity": "things", "fields": {"id": {"type": "uuid", "primary_key": True},
                                                     "version": {"type": "int", "auto": "version"}},
                      "seed": {"initial_count": 0}})
    rows = [("cfg_aaaaaaaaaaaaaaa1", "prov_old", "things", "d_aaaaaaaaaa_things", 0),
            ("cfg_bbbbbbbbbbbbbbb1", "prov_mixed", "open", "d_bbbbbbbbbb_open", 0),
            ("cfg_bbbbbbbbbbbbbbb2", "prov_mixed", "secret", "d_bbbbbbbbbb_secret", 1)]
    db.executemany("INSERT INTO registry_configs VALUES (?,?,?,?,?,?,?,?,?)",
                   [(i, p, n, ph, cfg.replace("things", n), flag, "api", now, now)
                    for i, p, n, ph, flag in rows])
    db.commit()
    db.close()


MIGRATE = (
    "import json\n"
    "from fastapi.testclient import TestClient\n"
    "from app.main import app\n"
    "with TestClient(app) as c:\n"
    "    h = {'X-API-Key': 'k' * 32}\n"
    "    out = {}\n"
    "    for p in c.get('/v1/projects', headers=h).json()['items']:\n"
    "        out[p['owner']['full_name'] + '/' + p['name']] = [p['is_only_me'], p['config_count']]\n"
    "    out['cfgs'] = sorted((i['name'], i['project']['name']) for i in c.get('/v1/configs', headers=h).json()['items']\n"
    "                         if i['source'] == 'api')\n"
    "    print(json.dumps(out))\n"
)


def test_a_database_from_before_projects_is_migrated_on_startup(tmp_path):
    import os
    db_path = tmp_path / "legacy.db"
    _legacy_db(str(db_path))
    env = {k: v for k, v in os.environ.items() if k not in (
        "API_KEYS", "TEST_DATABASE_URL")}
    env.update(DATABASE_URL=f"sqlite:///{db_path}", API_KEY="k" * 32)
    root = os.path.dirname(os.path.dirname(__file__))
    for boot in (1, 2):  # the second boot proves the migration is idempotent
        out = subprocess.run([sys.executable, "-c", MIGRATE],
                             cwd=root, capture_output=True, text=True, env=env)
        assert out.returncode == 0, out.stderr
        got = json.loads(out.stdout.strip().splitlines()[-1])
        # one `default` project per provider; a provider with ANY private config gets a private project
        assert got["Olga Old/default"] == [False, 1], boot
        assert got["Max Mixed/default"] == [True, 2], boot
        assert got["cfgs"] == [["open", "default"], [
            "secret", "default"], ["things", "default"]], boot
        assert got["System (built-in examples)/examples"][1] >= 3
    db = sqlite3.connect(str(db_path))
    cols = {r[1] for r in db.execute("PRAGMA table_info(registry_configs)")}
    assert "project_id" in cols and "is_only_me" not in cols
    assert not db.execute(
        "SELECT name FROM sqlite_master WHERE name='registry_configs_old'").fetchall()
    # the UNIQUE rule is now per project: the same name can exist in two projects
    assert any("project_id" in (r[0] or "") for r in db.execute(
        "SELECT sql FROM sqlite_master WHERE name='registry_configs'"))
