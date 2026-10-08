"""Who can see and change what. This is the isolation guarantee, tested route by route:

  owner       reads and writes their projects and the configs inside them
  other       reads another provider's project (and its configs) unless it is is_only_me;
              never writes it
  private     a project with is_only_me, and every config in it, looks nonexistent (404)
              to everyone but its owner
  superuser   reads and writes everything

Visibility belongs to the PROJECT (a schema is shared or hidden as a unit), so "a private
config" below means a config that lives in a private project.
"""
import io
import json
import re
import zipfile

import pytest

from conftest import customers_cfg, make_project, orders_cfg, publish

NAMESPACED = re.compile(r"\bd_[0-9a-f]{10}_")


@pytest.fixture(scope="module")
def world(client, su, alice, bob):
    pub = publish(client, alice, customers_cfg("acc_pub", count=3))
    hidden = make_project(client, alice, "acc_hidden", is_only_me=True)
    priv = publish(client, alice, customers_cfg(
        "acc_priv", count=3), project=hidden["id"])
    rows = {}
    for key, made in (("pub", pub), ("priv", priv)):
        rows[key] = client.get(
            f"/v1/configs/{made['id']}/data?limit=10", headers=alice["h"]).json()["items"]
    return {"pub": pub["id"], "priv": priv["id"], "priv_project": hidden["id"],
            "pub_project": pub["project"]["id"], "row": {k: [r["customer_id"] for r in v] for k, v in rows.items()}}


# (label, method, path with {id} and {row}, json body, status the OWNER gets)
READS = [
    ("get config", "GET", "/v1/configs/{id}", None, 200),
    ("list data", "GET", "/v1/configs/{id}/data", None, 200),
    ("get row", "GET", "/v1/configs/{id}/data/{row}", None, 200),
    ("changes", "GET", "/v1/configs/{id}/changes", None, 200),
    ("export", "GET", "/v1/configs/{id}/export", None, 200),
    ("ddl", "GET", "/v1/configs/{id}/ddl", None, 200),
    ("metrics", "GET", "/v1/configs/{id}/metrics", None, 200),
    ("runs", "GET", "/v1/configs/{id}/runs", None, 200),
    ("bundle", "GET", "/v1/export?configs={id}", None, 200),
]
WRITES = [
    ("patch config", "PATCH",
     "/v1/configs/{id}", {"failure_injection": {"latency_ms": 0}}, 200),
    ("delete config (unconfirmed)", "DELETE", "/v1/configs/{id}", None, 400),
    ("create row", "POST", "/v1/configs/{id}/data", {}, 201),
    ("update row", "PUT",
     "/v1/configs/{id}/data/{row}", {"name": "Renamed"}, 200),
    ("batch", "POST", "/v1/configs/{id}/batch",
     {"mode": "append", "count": 0}, 200),
    ("simulate", "POST", "/v1/configs/{id}/simulate",
     {"inserts": 0, "updates": 0, "deletes": 0}, 200),
    ("delete row", "DELETE", "/v1/configs/{id}/data/{row}", None, 200),
]
ROUTES = READS + WRITES


def call(client, headers, which, world, route):
    _, method, path, body, _ = route
    # the last row, so deleting it doesn't disturb the others
    row = world["row"][which][-1]
    url = path.format(id=world[which], row=row)
    return client.request(method, url, json=body, headers=headers)


@pytest.mark.parametrize("route", READS, ids=lambda r: r[0])
def test_other_providers_can_read_a_public_config(client, bob, world, route):
    assert call(client, bob["h"], "pub", world,
                route).status_code == route[4], route[0]


@pytest.mark.parametrize("route", WRITES, ids=lambda r: r[0])
def test_other_providers_can_never_change_a_public_config(client, bob, world, route):
    resp = call(client, bob["h"], "pub", world, route)
    assert resp.status_code == 403 and resp.json(
    )["error"]["code"] == "forbidden", (route[0], resp.text)


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r[0])
def test_a_private_config_is_invisible_to_other_providers(client, bob, world, route):
    resp = call(client, bob["h"], "priv", world, route)
    assert resp.status_code == 404 and resp.json(
    )["error"]["code"] == "not_found", (route[0], resp.text)


@pytest.mark.parametrize("route", READS + WRITES[:-1], ids=lambda r: r[0])
@pytest.mark.parametrize("which", ["pub", "priv"])
def test_the_owner_can_use_every_route_on_both(client, alice, world, which, route):
    assert call(client, alice["h"], which, world,
                route).status_code == route[4], (which, route[0])


@pytest.mark.parametrize("route", READS + WRITES[:-1], ids=lambda r: r[0])
@pytest.mark.parametrize("which", ["pub", "priv"])
def test_the_superuser_can_use_every_route_on_both(client, su, world, which, route):
    assert call(client, su, which, world,
                route).status_code == route[4], (which, route[0])


def test_the_owner_and_superuser_can_delete_rows_too(client, alice, su, world):
    last = WRITES[-1]
    assert call(client, alice["h"], "pub", world, last).status_code == 200
    world["row"]["priv"].pop()  # delete a different row for the superuser
    assert call(client, su, "priv", world, last).status_code == 200


def test_a_private_config_is_indistinguishable_from_one_that_does_not_exist(client, bob, world):
    ghost = client.get(
        "/v1/configs/cfg_0000000000000000/data", headers=bob["h"])
    real = client.get(f"/v1/configs/{world['priv']}/data", headers=bob["h"])
    assert (ghost.status_code, ghost.json()["error"]["code"]) == (
        real.status_code, real.json()["error"]["code"]) == (404, "not_found")
    # the message doesn't add detail beyond the id asked for
    assert world["priv"] not in real.text.replace(f"'{world['priv']}'", "")


def test_flipping_a_projects_is_only_me_takes_effect_immediately(client, alice, bob):
    project = make_project(client, alice, "acc_flip")
    made = publish(client, alice, customers_cfg(
        "acc_flip"), project=project["id"])
    url = f"/v1/configs/{made['id']}/data"
    purl = f"/v1/projects/{project['id']}"
    assert client.get(url, headers=bob["h"]).status_code == 200
    assert client.patch(
        purl, json={"is_only_me": True}, headers=alice["h"]).status_code == 200
    # the config vanished...
    assert client.get(url, headers=bob["h"]).status_code == 404
    # ...and so did its project
    assert client.get(purl, headers=bob["h"]).status_code == 404
    assert made["id"] not in {i["id"] for i in client.get(
        "/v1/configs", headers=bob["h"]).json()["items"]}
    assert project["id"] not in {i["id"] for i in client.get(
        "/v1/projects", headers=bob["h"]).json()["items"]}
    client.patch(purl, json={"is_only_me": False}, headers=alice["h"])
    assert client.get(url, headers=bob["h"]).status_code == 200


def test_visibility_cannot_be_set_on_a_single_config(client, alice, bob):
    """A shared config must never point at a hidden parent, so there is no per-config switch."""
    project = make_project(client, alice, "acc_noswitch")
    made = publish(client, alice, customers_cfg(
        "acc_noswitch"), project=project["id"])
    resp = client.patch(
        f"/v1/configs/{made['id']}", json={"is_only_me": True}, headers=alice["h"])
    assert resp.status_code == 422 and "PATCH /v1/projects" in resp.json()[
        "error"]["message"]
    resp = client.post(f"/v1/projects/{project['id']}/configs",
                       json=customers_cfg("acc_noswitch2", is_only_me=True), headers=alice["h"])
    assert resp.status_code == 422 and "project setting" in resp.json()[
        "error"]["message"]
    assert client.get(
        f"/v1/configs/{made['id']}/data", headers=bob["h"]).status_code == 200


def test_readers_of_a_public_config_see_the_owners_new_data(client, alice, bob):
    made = publish(client, alice, customers_cfg("acc_live", count=2))
    url = f"/v1/configs/{made['id']}/data?limit=50"
    assert len(client.get(url, headers=bob["h"]).json()["items"]) == 2
    client.post(f"/v1/configs/{made['id']}/data",
                json={"name": "Fresh Face"}, headers=alice["h"])
    names = [r["name"]
             for r in client.get(url, headers=bob["h"]).json()["items"]]
    assert len(names) == 3 and "Fresh Face" in names
    assert len(client.get(
        f"/v1/configs/{made['id']}/changes?since=0", headers=bob["h"]).json()["changes"]) == 3


def test_readers_get_the_owners_failure_injection(client, alice, bob):
    made = publish(client, alice, customers_cfg("acc_fail"))
    url = f"/v1/configs/{made['id']}/data"
    client.patch(f"/v1/configs/{made['id']}", json={
                 "failure_injection": {"fail_rate": 1.0}}, headers=alice["h"])
    for who in (alice, bob):
        resp = client.get(url, headers=who["h"])
        assert resp.status_code == 500 and resp.json(
        )["error"]["code"] == "injected_failure"
    client.patch(f"/v1/configs/{made['id']}", json={
                 "failure_injection": {"fail_rate": 0.0}}, headers=alice["h"])
    assert client.get(url, headers=bob["h"]).status_code == 200


# ----------------------------------------------- tenants never reach each other ----
def test_a_providers_bulk_operations_only_touch_their_own_tables(client, alice, bob):
    """Both providers have `parent`/`child`. Bob wipes and regenerates his; Alice's are untouched."""
    a_parent = publish(client, alice, customers_cfg("iso_parent", count=4))
    a_child = publish(client, alice, orders_cfg(
        "iso_parent", name="iso_child", count=6))
    b_parent = publish(client, bob, customers_cfg("iso_parent", count=5))
    b_child = publish(client, bob, orders_cfg(
        "iso_parent", name="iso_child", count=7))

    def count(who, made): return client.get(
        f"/v1/configs/{made['id']}/metrics", headers=who["h"]).json()["row_count"]
    a_ids = {r["customer_id"] for r in client.get(
        f"/v1/configs/{a_parent['id']}/data?limit=100", headers=alice["h"]).json()["items"]}

    replaced = client.post(f"/v1/configs/{b_parent['id']}/batch", json={"mode": "replace", "confirm": True, "count": 9},
                           headers=bob["h"])
    assert replaced.status_code == 200, replaced.text
    body = replaced.json()
    assert [e["entity"] for e in body["entities"]] == [
        "iso_parent", "iso_child"]  # his own, in his own names
    # his child was refreshed with it
    assert body["auto_included"] == ["iso_child"]
    assert not NAMESPACED.search(replaced.text)

    assert (count(alice, a_parent), count(
        alice, a_child)) == (4, 6)  # hers: untouched
    assert count(bob, b_parent) == 9 and count(bob, b_child) == 7
    assert a_ids == {r["customer_id"] for r in client.get(
        f"/v1/configs/{a_parent['id']}/data?limit=100", headers=alice["h"]).json()["items"]}
    # and a row Bob generates only ever points at Bob's parents
    b_ids = {r["customer_id"] for r in client.get(
        f"/v1/configs/{b_parent['id']}/data?limit=100", headers=bob["h"]).json()["items"]}
    for _ in range(5):
        row = client.post(
            f"/v1/configs/{b_child['id']}/data", json={}, headers=bob["h"]).json()
        assert row["customer_id"] in b_ids and row["customer_id"] not in a_ids


def test_a_row_cannot_reference_another_providers_rows(client, alice, bob):
    a_parent = publish(client, alice, customers_cfg("xref_parent", count=3))
    b_parent = publish(client, bob, customers_cfg("xref_parent", count=3))
    b_child = publish(client, bob, orders_cfg(
        "xref_parent", name="xref_child"))
    alices_row = client.get(
        f"/v1/configs/{a_parent['id']}/data?limit=1", headers=alice["h"]).json()["items"][0]["customer_id"]
    resp = client.post(
        f"/v1/configs/{b_child['id']}/data", json={"customer_id": alices_row}, headers=bob["h"])
    assert resp.status_code == 422 and resp.json(
    )["error"]["code"] == "invalid_reference"
    own = client.get(f"/v1/configs/{b_parent['id']}/data?limit=1",
                     headers=bob["h"]).json()["items"][0]["customer_id"]
    assert client.post(f"/v1/configs/{b_child['id']}/data", json={
                       "customer_id": own}, headers=bob["h"]).status_code == 201


def test_change_feeds_are_per_config(client, alice, bob):
    a = publish(client, alice, customers_cfg("feed_same", count=2))
    b = publish(client, bob, customers_cfg("feed_same", count=5))
    assert len(client.get(
        f"/v1/configs/{a['id']}/changes?since=0&limit=100", headers=alice["h"]).json()["changes"]) == 2
    assert len(client.get(
        f"/v1/configs/{b['id']}/changes?since=0&limit=100", headers=bob["h"]).json()["changes"]) == 5


# ----------------------------------------------------------- the superuser ----
def test_the_superuser_can_create_projects_and_configs_on_behalf_of_a_provider(client, su, alice, bob):
    made_project = client.post(f"/v1/projects?provider_id={alice['id']}",
                               json={"name": "su_made"}, headers=su)
    assert made_project.status_code == 201 and made_project.json()[
        "owner"]["id"] == alice["id"]
    pid = made_project.json()["id"]
    made = client.post(
        f"/v1/projects/{pid}/configs", json=customers_cfg("su_made", count=2), headers=su)
    # the config belongs to the PROJECT's owner, not to whoever typed the request
    assert made.status_code == 201 and made.json()[
        "owner"]["id"] == alice["id"]
    assert client.get(  # hers now
        f"/v1/configs/{made.json()['id']}/data", headers=alice["h"]).status_code == 200
    assert client.get(  # public by default
        f"/v1/configs/{made.json()['id']}/data", headers=bob["h"]).status_code == 200
    assert client.post("/v1/projects?provider_id=prov_nope",
                       json={"name": "su_x"}, headers=su).status_code == 404
    default = client.post(
        "/v1/projects", json={"name": "su_system_made"}, headers=su)
    assert default.status_code == 201 and default.json()[
        "owner"]["id"] == "prov_system"
    cfg = client.post(f"/v1/projects/{default.json()['id']}/configs",
                      json=customers_cfg("su_system_made", count=1), headers=su)
    assert cfg.status_code == 201 and cfg.json()[
        "owner"]["id"] == "prov_system"


def test_a_provider_cannot_create_projects_or_configs_for_someone_else(client, alice, bob):
    resp = client.post(f"/v1/projects?provider_id={bob['id']}",
                       json={"name": "sneaky"}, headers=alice["h"])
    assert resp.status_code == 403 and resp.json()[
        "error"]["code"] == "forbidden"
    assert client.post(f"/v1/projects?provider_id={alice['id']}", json={"name": "sneaky_ok"},
                       headers=alice["h"]).status_code == 201
    # nor can she add a table to a schema that is only Bob's, even a public one
    bobs = make_project(client, bob, "bob_public")
    resp = client.post(f"/v1/projects/{bobs['id']}/configs", json=customers_cfg("intruder"),
                       headers=alice["h"])
    assert resp.status_code == 403 and resp.json()[
        "error"]["code"] == "forbidden"


def test_the_superuser_can_change_and_delete_any_providers_config(client, su, alice):
    project = make_project(client, alice, "su_edit", is_only_me=True)
    made = publish(client, alice, customers_cfg(
        "su_edit", count=2), project=project["id"])
    cid = made["id"]
    assert client.patch(f"/v1/projects/{project['id']}", json={"is_only_me": False},
                        headers=su).json()["is_only_me"] is False
    assert client.post(
        f"/v1/configs/{cid}/data", json={"name": "Su Wrote"}, headers=su).status_code == 201
    assert client.delete(
        f"/v1/configs/{cid}?confirm=true", headers=su).status_code == 200
    assert client.get(f"/v1/configs/{cid}",
                      headers=alice["h"]).status_code == 404
    assert client.delete(
        f"/v1/projects/{project['id']}?confirm=true", headers=su).status_code == 200


def test_the_superuser_can_write_to_the_built_in_examples_but_providers_cannot(client, su, alice):
    assert client.post("/v1/configs/cfg_sys_orders/data",
                       json={"amount": 12.5}, headers=su).status_code == 201
    resp = client.post("/v1/configs/cfg_sys_orders/data",
                       json={"amount": 12.5}, headers=alice["h"])
    assert resp.status_code == 403


# -------------------------------------------------------------- bundles ----
def test_bundles_respect_access_and_use_your_own_names(client, alice, bob):
    parent = publish(client, alice, customers_cfg("bun_parent", count=3))
    child = publish(client, alice, orders_cfg(
        "bun_parent", name="bun_child", count=4))
    hidden = make_project(client, alice, "bun_hidden", is_only_me=True)
    private = publish(client, alice, customers_cfg(
        "bun_private"), project=hidden["id"])
    ids = f"{parent['id']},{child['id']}"

    # Bob may read public configs
    resp = client.get(f"/v1/export?configs={ids}&format=sql", headers=bob["h"])
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert sorted(zf.namelist()) == [
        "bun_child.sql", "bun_parent.sql", "manifest.json", "schema.sql"]
    everything = "".join(zf.read(n).decode() for n in zf.namelist())
    assert not NAMESPACED.search(everything)
    assert "CREATE TABLE IF NOT EXISTS bun_parent" in everything and "INSERT INTO bun_child" in everything
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["load_order"] == ["bun_parent", "bun_child"]
    assert {f["entity"]: f["config_id"] for f in manifest["files"]} == {
        "bun_parent": parent["id"], "bun_child": child["id"]}

    assert client.get(
        f"/v1/export?configs={private['id']}", headers=bob["h"]).status_code == 404
    assert client.get(
        f"/v1/export?configs={parent['id']}&configs={private['id']}", headers=bob["h"]).status_code == 404
    assert client.get("/v1/export?configs=",
                      headers=alice["h"]).status_code == 422


def test_a_bundle_holds_one_projects_configs(client, su, alice, bob):
    a = publish(client, alice, customers_cfg("mix_a"))
    b = publish(client, bob, customers_cfg("mix_b"))
    other = make_project(client, alice, "mix_other")
    a2 = publish(client, alice, customers_cfg("mix_a2"), project=other["id"])
    for who in (alice["h"], su):
        for ids in (f"{a['id']},{b['id']}",      # two owners
                    f"{a['id']},{a2['id']}"):    # one owner, two projects
            resp = client.get(f"/v1/export?configs={ids}", headers=who)
            assert resp.status_code == 422 and resp.json(
            )["error"]["code"] == "mixed_projects"


def test_a_bundle_can_mix_changes_and_snapshots(client, alice):
    parent = publish(client, alice, customers_cfg("mixchg_parent", count=3))
    child = publish(client, alice, orders_cfg(
        "mixchg_parent", name="mixchg_child", count=4))
    resp = client.get(
        f"/v1/export?configs={parent['id']},{child['id']}&since={child['id']}=2", headers=alice["h"])
    assert resp.status_code == 200, resp.text
    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert "mixchg_child.changes.csv" in names and "mixchg_parent.csv" in names
    stray = client.get(
        f"/v1/export?configs={parent['id']}&since={child['id']}=1", headers=alice["h"])
    assert stray.status_code == 422 and stray.json(
    )["error"]["code"] == "since_not_targeted"


# ------------------------------------------------------- every route, listed ----
def test_every_v1_route_with_a_config_id_goes_through_the_access_check(client, bob, world):
    """Walk the route table: any /v1 route that takes a config_id must 404 on a private config for a stranger."""
    from fastapi.routing import APIRoute

    checked = 0
    for route in client.app.routes:
        if not (isinstance(route, APIRoute) and route.path.startswith(("/v1/configs/{config_id}", "/v1/projects/{project_id}"))):
            continue
        for method in sorted(route.methods):
            url = (route.path.replace("{config_id}", world["priv"])
                   .replace("{project_id}", world["priv_project"])
                   .replace("{row_id}", world["row"]["priv"][0]))
            resp = client.request(method, url, json={}, headers=bob["h"])
            assert resp.status_code == 404, f"{method} {route.path} answered {resp.status_code} to a stranger on a private config"
            checked += 1
    assert checked >= 22
