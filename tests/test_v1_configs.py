"""Publishing configs into a project: create (JSON and YAML), validate, limits, edit, delete,
the scheduler. (Projects themselves are in test_v1_projects.py.)

The repo's configs/*.yaml are examples and are deliberately NOT used here: every config
below is built in the test, from conftest's helpers."""
import re

import pytest
import yaml
from sqlalchemy import inspect, select, func

from app.db import models
from app.services.catalog import catalog
from app.db.engine import engine
from app.services.scheduler import run_entity_job
from conftest import customers_cfg, default_project, make_project, make_provider, orders_cfg, publish


def cfg_url(client, who, project=None):
    """Where a config is posted: the provider's `main` project unless one is named."""
    return f"/v1/projects/{project or default_project(client, who)}/configs"


def tables_in_db():
    return set(inspect(engine).get_table_names())


def entry_of(config_id):
    return catalog.entries[config_id]


# --------------------------------------------------------------- creating ----
def test_create_returns_the_config_its_owner_and_the_seeded_row_count(client, su, alice):
    made = publish(client, alice, customers_cfg("c_basic", count=7))
    assert made["id"].startswith("cfg_") and made["name"] == "c_basic"
    assert made["owner"] == {"id": alice["id"], "full_name": "Alice Almeida"}
    assert made["project"]["name"] == "main" and made["project"]["is_only_me"] is False
    assert made["source"] == "api" and made["can_write"] is True
    assert made["seeded_rows"] == 7
    assert made["config"]["fields"]["version"] == {
        "type": "int", "primary_key": False, "nullable": False, "auto": "version"}
    rows = client.get(
        f"/v1/configs/{made['id']}/data?limit=100", headers=alice["h"]).json()["items"]
    assert len(rows) == 7 and all(r["version"] >= 1 for r in rows)


def test_a_yaml_body_posts_as_is(client, alice):
    """A YAML document (what a file under configs/ looks like) posts straight to a project.
    The YAML is built here from the test's own dicts: the example files are not read."""
    for cfg in (customers_cfg("yaml_cust", count=3), orders_cfg("yaml_cust", name="yaml_ord", count=4)):
        resp = client.post(cfg_url(client, alice), content=yaml.safe_dump(cfg),
                           headers={**alice["h"], "Content-Type": "text/yaml"})
        assert resp.status_code == 201, resp.text
    names = {i["name"] for i in client.get(
        "/v1/configs?scope=mine", headers=alice["h"]).json()["items"]}
    assert {"yaml_cust", "yaml_ord"} <= names


def test_the_data_lives_in_a_namespaced_table_but_the_config_uses_your_names(client, alice):
    cust = publish(client, alice, customers_cfg("c_ns"))
    orders = publish(client, alice, orders_cfg("c_ns", name="o_ns"))
    physical = entry_of(orders["id"]).physical
    assert physical.startswith("d_") and physical.endswith(
        "_o_ns") and physical in tables_in_db()
    # not the physical name
    assert orders["config"]["fields"]["customer_id"]["ref_entity"] == "c_ns"
    assert "o_ns" not in tables_in_db()


def test_two_providers_can_both_have_a_config_with_the_same_name(client, alice, bob):
    a = publish(client, alice, customers_cfg("shared_name", count=3))
    b = publish(client, bob, customers_cfg("shared_name", count=9))
    assert a["id"] != b["id"]
    assert len(client.get(
        f"/v1/configs/{a['id']}/data?limit=50", headers=alice["h"]).json()["items"]) == 3
    assert len(client.get(
        f"/v1/configs/{b['id']}/data?limit=50", headers=bob["h"]).json()["items"]) == 9
    assert entry_of(a["id"]).physical != entry_of(b["id"]).physical


def test_the_longest_allowed_names_fit_in_database_identifiers(client, alice):
    """32-character config names and 40-character field names, with their index and FK names, stay
    within PostgreSQL's 63-character identifier limit (run the suite on Postgres to prove it)."""
    long_name = "a" * 32
    cfg = customers_cfg(long_name, count=2)
    cfg["fields"]["f" * 40] = {"type": "string"}
    parent = publish(client, alice, cfg)
    child = publish(client, alice, orders_cfg(
        long_name, name="b" * 32, count=2))
    assert len(entry_of(child["id"]).physical) <= 45
    assert client.get(
        f"/v1/configs/{child['id']}/data", headers=alice["h"]).status_code == 200
    assert f"CREATE TABLE IF NOT EXISTS {long_name}" in client.get(
        f"/v1/configs/{child['id']}/ddl", headers=alice["h"]).text
    assert client.post(cfg_url(client, alice) + "/validate", json=customers_cfg("a" * 33),
                       headers=alice["h"]).status_code == 422


def test_a_config_name_is_unique_per_project_not_per_provider(client, alice):
    publish(client, alice, customers_cfg("dupe"))
    resp = client.post(
        cfg_url(client, alice), json=customers_cfg("dupe"), headers=alice["h"])
    assert resp.status_code == 409 and resp.json(
    )["error"]["code"] == "already_exists"
    # another project is another schema: the same table name is fine there
    other = make_project(client, alice, "dupe_elsewhere")
    again = publish(client, alice, customers_cfg("dupe"), project=other["id"])
    assert again["project"]["id"] == other["id"]


def test_the_built_in_examples_project_takes_no_new_configs_and_its_name_is_taken(client, su):
    examples = "prj_system_examples"
    resp = client.post(
        f"/v1/projects/{examples}/configs", json=customers_cfg("extra"), headers=su)
    assert resp.status_code == 409 and resp.json(
    )["error"]["code"] == "managed_by_yaml"
    dry = client.post(
        f"/v1/projects/{examples}/configs/validate", json=customers_cfg("extra"), headers=su)
    assert dry.status_code == 409 and dry.json(
    )["error"]["code"] == "managed_by_yaml"
    # the system provider already owns a project called `examples`
    assert client.post(
        "/v1/projects", json={"name": "examples"}, headers=su).status_code == 409


def test_a_projects_visibility_applies_to_the_configs_in_it(client, alice, bob):
    shared = publish(client, alice, customers_cfg("c_default"))
    assert shared["project"]["is_only_me"] is False
    hidden = make_project(client, alice, "c_hidden_project", is_only_me=True)
    private = publish(client, alice, customers_cfg(
        "c_private"), project=hidden["id"])
    assert private["project"]["is_only_me"] is True
    assert client.get(
        f"/v1/configs/{shared['id']}", headers=bob["h"]).status_code == 200
    assert client.get(
        f"/v1/configs/{private['id']}", headers=bob["h"]).status_code == 404


# --------------------------------------------------------------- validate ----
def test_validate_checks_everything_and_saves_nothing(client, alice):
    before = len(client.get("/v1/configs?scope=mine",
                 headers=alice["h"]).json()["items"])
    ok = client.post(cfg_url(client, alice) + "/validate",
                     json=customers_cfg("v_dry", count=2), headers=alice["h"])
    assert ok.status_code == 200
    body = ok.json()
    assert body["valid"] is True and body["name_available"] is True and "version" in body["config"]["fields"]
    assert body["project"]["name"] == "main"
    assert len(client.get("/v1/configs?scope=mine",
               headers=alice["h"]).json()["items"]) == before

    publish(client, alice, customers_cfg("v_taken"))
    assert client.post(cfg_url(client, alice) + "/validate", json=customers_cfg("v_taken"),
                       headers=alice["h"]).json()["name_available"] is False


def cfg_with(**changes):
    base = customers_cfg("bad_one")
    base.update(changes)
    return base


@pytest.mark.parametrize("mutate, fragment", [
    (lambda c: c.update(entity="Bad Name"), "name must be"),
    (lambda c: c.update(entity="schema"), "reserved"),
    (lambda c: c.update(entity="x" * 33), "name must be"),
    (lambda c: c.update(typo_key=1), "unknown top-level key"),
    (lambda c: c.update(update_schedul={
     "cadence": "daily"}), "unknown top-level key"),
    (lambda c: c["seed"].update(initial_count=10**7), "initial_count"),
    (lambda c: c["seed"].update(initial_count=-1), "initial_count"),
    (lambda c: c.update(failure_injection={"fail_rate": 1.5}), "fail_rate"),
    (lambda c: c.update(failure_injection={
     "latency_ms": 10**6}), "latency_ms"),
    (lambda c: c.update(update_schedule={
     "new_records": [9, 1]}), "new_records"),
    (lambda c: c.update(update_schedule={
     "new_records": [0, 5000]}), "new_records"),
    (lambda c: c.update(update_schedule={
     "mutate_existing_pct": 150}), "mutate_existing_pct"),
    (lambda c: c.update(update_schedule={"cadence": "minutely"}), "cadence"),
    (lambda c: c["fields"].update(extra={
     "type": "string", "key_label": "no_such_provider"}), "can't generate data"),
    (lambda c: c["fields"].update(extra={
     "type": "string", "ik_options": {"a": 1}}), "ik_options needs a key_label"),
    (lambda c: c["fields"].update(extra={"type": "enum"}), "enum needs"),
    (lambda c: c["fields"].update(
        extra={"type": "string", "values": ["a"]}), "only applies to type: enum"),
    (lambda c: c["fields"].update(
        extra={"type": "ref", "ref_entity": "nope"}), "not one of this project's configs"),
    (lambda c: c["fields"].update(
        extra={"type": "int", "min": 5, "max": 1}), "min"),
    (lambda c: c["fields"].update(
        extra={"type": "int", "bogus_key": 1}), "unknown key"),
    (lambda c: c["fields"].update(Bad_Name={"type": "string"}), "field name"),
    (lambda c: c["fields"].update(
        second={"type": "uuid", "primary_key": True}), "exactly one"),
    (lambda c: c["fields"].update(version={"type": "string"}), "version"),
    (lambda c: c["fields"].update(other_counter={
     "type": "int", "auto": "version"}), "must be named 'version'"),
    (lambda c: c["fields"].update(another_gone={
     "type": "timestamp", "auto": "soft_delete"}), "only one soft_delete"),
    (lambda c: c["fields"].update(bad_auto={
     "type": "string", "auto": "created"}), "timestamp"),
    (lambda c: c["fields"]["customer_id"].update(type="int"), "uuid"),
    (lambda c: c.update(fields={}), "non-empty"),
    (lambda c: c.update(is_only_me="yes"), "is_only_me"),
    (lambda c: c.update(name="other"), "same thing"),
])
def test_bad_configs_are_rejected_with_a_clear_reason(client, alice, mutate, fragment):
    cfg = customers_cfg("bad_one")
    mutate(cfg)
    resp = client.post(cfg_url(client, alice) + "/validate",
                       json=cfg, headers=alice["h"])
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "invalid_config" and fragment in err["message"], err
    # and creating it fails the same way, leaving nothing behind
    names_before = {i["name"] for i in client.get(
        "/v1/configs?scope=mine", headers=alice["h"]).json()["items"]}
    assert client.post(cfg_url(client, alice), json=cfg,
                       headers=alice["h"]).status_code == 422
    names_after = {i["name"] for i in client.get(
        "/v1/configs?scope=mine", headers=alice["h"]).json()["items"]}
    assert names_before == names_after


def test_a_config_cannot_reference_a_config_outside_its_project(client, alice, bob):
    """Refs resolve inside one schema: not another provider's, and not even your own other project's."""
    publish(client, alice, customers_cfg("alice_parent"))
    msg = "not one of this project's configs"
    resp = client.post(cfg_url(client, bob), json=orders_cfg("alice_parent", name="bob_child"),
                       headers=bob["h"])
    assert resp.status_code == 422 and msg in resp.json()["error"]["message"]
    other = make_project(client, alice, "ref_other")
    resp = client.post(cfg_url(client, alice, other["id"]),
                       json=orders_cfg("alice_parent", name="alice_child"), headers=alice["h"])
    assert resp.status_code == 422 and msg in resp.json()["error"]["message"]
    # inside the project it works
    publish(client, alice, orders_cfg("alice_parent", name="alice_child"))


def test_body_problems(client, alice):
    h = alice["h"]
    assert client.post(cfg_url(client, alice), content=b"{not json", headers={
                       **h, "Content-Type": "application/json"}).status_code == 422
    assert client.post(cfg_url(client, alice), content="- a\n- list\n",
                       headers={**h, "Content-Type": "text/yaml"}).status_code == 422
    assert client.post(cfg_url(client, alice), content=b"\xff\xfe",
                       headers={**h, "Content-Type": "text/yaml"}).status_code == 422
    bomb = "a: &a [1,2,3]\nb: &b [*a,*a]\nc: [*b,*b]\n"
    resp = client.post(cfg_url(client, alice), content=bomb,
                       headers={**h, "Content-Type": "text/yaml"})
    assert resp.status_code == 422 and "anchors" in resp.json()[
        "error"]["message"]
    huge = client.post(cfg_url(client, alice), content="x: " + "y" *
                       70_000, headers={**h, "Content-Type": "text/yaml"})
    assert huge.status_code == 413 and huge.json(
    )["error"]["code"] == "payload_too_large"


def test_a_parent_with_no_rows_fails_cleanly_and_leaves_nothing_behind(client, alice):
    parent = publish(client, alice, customers_cfg("empty_parent", count=0))
    before = tables_in_db()
    resp = client.post(cfg_url(client, alice), json=orders_cfg("empty_parent",
                       name="starved_child"), headers=alice["h"])
    assert resp.status_code == 409 and resp.json(
    )["error"]["code"] == "missing_parent_rows"
    msg = resp.json()["error"]["message"]
    assert "empty_parent" in msg and not re.search(
        r"\bd_[0-9a-f]{10}_", msg)  # your names, not the namespaced ones
    assert tables_in_db() == before
    assert "starved_child" not in {i["name"] for i in client.get(
        "/v1/configs?scope=mine", headers=alice["h"]).json()["items"]}
    # once the parent has rows, the same config goes through
    client.post(f"/v1/configs/{parent['id']}/batch",
                json={"count": 3}, headers=alice["h"])
    publish(client, alice, orders_cfg("empty_parent", name="starved_child"))


# ---------------------------------------------------------------- quotas ----
def test_per_provider_config_quotas(client, su, monkeypatch):
    p = make_provider(client, su, "Quota Q")
    monkeypatch.setenv("MAX_CONFIGS_PER_PROVIDER", "2")
    monkeypatch.setenv("MAX_FIELDS_PER_CONFIG", "9")
    monkeypatch.setenv("MAX_INITIAL_COUNT", "20")
    monkeypatch.setenv("MAX_LATENCY_MS", "50")
    publish(client, p, customers_cfg("q1", count=20))
    over = client.post(
        cfg_url(client, p), json=customers_cfg("q2", count=21), headers=p["h"])
    assert over.status_code == 422 and "initial_count" in over.json()[
        "error"]["message"]
    wide = customers_cfg("q2")
    wide["fields"].update({f"f{i}": {"type": "int"} for i in range(5)})
    assert client.post(cfg_url(client, p), json=wide,
                       headers=p["h"]).status_code == 422
    slow = customers_cfg("q2", failure_injection={"latency_ms": 51})
    assert client.post(cfg_url(client, p), json=slow,
                       headers=p["h"]).status_code == 422
    publish(client, p, customers_cfg("q2"))
    third = client.post(
        cfg_url(client, p), json=customers_cfg("q3"), headers=p["h"])
    assert third.status_code == 409 and third.json(
    )["error"]["code"] == "quota_exceeded"
    # deleting one frees a slot
    cid = next(i["id"] for i in client.get("/v1/configs?scope=mine",
               headers=p["h"]).json()["items"] if i["name"] == "q1")
    assert client.delete(
        f"/v1/configs/{cid}?confirm=true", headers=p["h"]).status_code == 200
    publish(client, p, customers_cfg("q3"))


# ------------------------------------------------------------------ list ----
def test_listing_scopes(client, su, alice, bob):
    pub = publish(client, alice, customers_cfg("l_pub"))
    hidden = make_project(client, alice, "l_hidden", is_only_me=True)
    priv = publish(client, alice, customers_cfg(
        "l_priv"), project=hidden["id"])
    def names(who, scope): return {i["name"] for i in client.get(
        f"/v1/configs?scope={scope}", headers=who["h"]).json()["items"]}
    assert {"l_pub", "l_priv"} <= names(alice, "mine") and not names(
        alice, "shared") & {"l_pub", "l_priv"}
    assert "l_pub" in names(bob, "shared") and "l_priv" not in names(
        bob, "all") and "l_pub" not in names(bob, "mine")
    assert {"l_pub", "l_priv"} <= {i["name"] for i in client.get(
        "/v1/configs", headers=su).json()["items"]}  # the superuser sees all
    item = next(i for i in client.get("/v1/configs?scope=shared",
                headers=bob["h"]).json()["items"] if i["id"] == pub["id"])
    assert item["can_write"] is False and item["owner"]["full_name"] == "Alice Almeida"
    # the list is a summary; GET /v1/configs/{id} has the definition
    assert "config" not in item


def test_the_built_in_examples_are_public_system_configs(client, alice):
    items = {i["name"]: i for i in client.get("/v1/configs?scope=shared", headers=alice["h"]).json()["items"]
             if i["source"] == "yaml"}
    assert {"customers", "orders", "support_tickets"} <= set(items)
    assert items["orders"]["id"] == "cfg_sys_orders" and items["orders"]["owner"]["id"] == "prov_system"
    assert items["orders"]["can_write"] is False
    assert client.get("/v1/configs/cfg_sys_orders/data?limit=3",
                      headers=alice["h"]).status_code == 200


# ----------------------------------------------------------------- patch ----
def test_patch_merges_schedule_and_failure_settings_and_reschedules(client, alice):
    made = publish(client, alice, customers_cfg("p_sched"))
    cid, physical = made["id"], entry_of(made["id"]).physical
    job = catalog.scheduler.get_job(f"job_{physical}")
    assert job is not None and job.trigger.interval.total_seconds() == 3600  # hourly by default

    resp = client.patch(f"/v1/configs/{cid}", json={"update_schedule": {"cadence": "daily"},
                                                    "failure_injection": {"latency_ms": 5}}, headers=alice["h"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["config"]["update_schedule"]["cadence"] == "daily"
    # merged
    assert body["config"]["update_schedule"]["new_records"] == made["config"]["update_schedule"]["new_records"]
    assert body["config"]["failure_injection"]["latency_ms"] == 5 and body["data_reset"] is False
    assert catalog.scheduler.get_job(
        f"job_{physical}").trigger.interval.total_seconds() == 86400
    # live immediately
    assert entry_of(cid).cfg.failure_injection.latency_ms == 5
    assert client.get(f"/v1/configs/{cid}", headers=alice["h"]).json()[
        "config"]["update_schedule"]["cadence"] == "daily"


@pytest.mark.parametrize("body, status", [
    ({}, 422), ({"name": "renamed"}, 422), ({
        "entity": "renamed"}, 422), ({"nope": 1}, 422),
    ({"is_only_me": "yes"}, 422), ({"seed": "x"}, 422), ({
        "failure_injection": {"fail_rate": 3}}, 422),
])
def test_patch_rejects_bad_requests(client, alice, body, status):
    made = publish(client, alice, customers_cfg(
        f"p_bad_{abs(hash(str(body))) % 10**6}"))
    assert client.patch(
        f"/v1/configs/{made['id']}", json=body, headers=alice["h"]).status_code == status


def test_changing_fields_needs_confirmation_and_resets_the_data(client, alice):
    made = publish(client, alice, customers_cfg("p_fields", count=6))
    cid = made["id"]
    new_fields = {"customer_id": {"type": "uuid",
                                  "primary_key": True}, "label": {"type": "string"}}
    refused = client.patch(
        f"/v1/configs/{cid}", json={"fields": new_fields}, headers=alice["h"])
    assert refused.status_code == 400 and refused.json(
    )["error"]["code"] == "confirmation_required"
    assert "name" in client.get(
        # untouched
        f"/v1/configs/{cid}/data?limit=1", headers=alice["h"]).json()["items"][0]

    done = client.patch(f"/v1/configs/{cid}?confirm=true", json={"fields": new_fields, "seed": {"initial_count": 3}},
                        headers=alice["h"])
    assert done.status_code == 200, done.text
    assert done.json()["data_reset"] is True and done.json()[
        "seeded_rows"] == 3
    rows = client.get(
        f"/v1/configs/{cid}/data?limit=50", headers=alice["h"]).json()["items"]
    assert len(rows) == 3 and set(rows[0]) == {
        "customer_id", "label", "version"}
    assert len(client.get(f"/v1/configs/{cid}/changes?since=0",
               headers=alice["h"]).json()["changes"]) == 3  # feed restarted
    assert catalog.scheduler.get_job(
        f"job_{entry_of(cid).physical}") is not None


def test_a_config_that_others_reference_cannot_have_its_fields_changed_or_be_deleted(client, alice):
    parent = publish(client, alice, customers_cfg("d_parent"))
    child = publish(client, alice, orders_cfg("d_parent", name="d_child"))
    blocked = client.patch(f"/v1/configs/{parent['id']}?confirm=true",
                           json={"fields": {"customer_id": {"type": "uuid", "primary_key": True}}}, headers=alice["h"])
    assert blocked.status_code == 409 and blocked.json(
    )["error"]["code"] == "has_dependents"
    assert "d_child" in blocked.json()["error"]["message"]
    gone = client.delete(
        f"/v1/configs/{parent['id']}?confirm=true", headers=alice["h"])
    assert gone.status_code == 409 and gone.json(
    )["error"]["code"] == "has_dependents"
    # children first, then the parent
    assert client.delete(
        f"/v1/configs/{child['id']}?confirm=true", headers=alice["h"]).status_code == 200
    assert client.delete(
        f"/v1/configs/{parent['id']}?confirm=true", headers=alice["h"]).status_code == 200


def test_an_edit_cannot_create_a_reference_cycle(client, alice):
    a = publish(client, alice, customers_cfg("cyc_a"))
    b = publish(client, alice, orders_cfg("cyc_a", name="cyc_b"))
    fields = customers_cfg("cyc_a")["fields"]
    fields["loop"] = {"type": "ref", "ref_entity": "cyc_b", "nullable": True}
    resp = client.patch(
        f"/v1/configs/{a['id']}?confirm=true", json={"fields": fields}, headers=alice["h"])
    assert resp.status_code == 422 and "circular" in resp.json()[
        "error"]["message"]


def test_built_in_yaml_configs_are_managed_by_their_files(client, su):
    resp = client.patch("/v1/configs/cfg_sys_orders",
                        json={"failure_injection": {"latency_ms": 1}}, headers=su)
    assert resp.status_code == 409 and resp.json(
    )["error"]["code"] == "managed_by_yaml"
    assert client.delete(
        "/v1/configs/cfg_sys_orders?confirm=true", headers=su).status_code == 409
    # the project that holds them can be hidden and shown again, but not renamed or deleted
    examples = "/v1/projects/prj_system_examples"
    assert client.patch(examples, json={"is_only_me": True}, headers=su).json()[
        "is_only_me"] is True
    assert client.patch(examples, json={"is_only_me": False}, headers=su).json()[
        "is_only_me"] is False
    renamed = client.patch(examples, json={"name": "mine"}, headers=su)
    assert renamed.status_code == 409 and renamed.json(
    )["error"]["code"] == "managed_by_yaml"
    assert client.delete(examples + "?confirm=true",
                         headers=su).status_code == 409


# ---------------------------------------------------------------- delete ----
def test_delete_removes_the_table_the_history_the_job_and_the_registry_row(client, alice):
    made = publish(client, alice, customers_cfg("x_gone", count=4))
    cid = made["id"]
    entry = entry_of(cid)
    physical = entry.physical
    client.post(f"/v1/configs/{cid}/simulate",
                json={"inserts": 2}, headers=alice["h"])
    run_entity_job(engine, physical, *reversed(catalog.scope(entry)))
    assert physical in tables_in_db(
    ) and catalog.scheduler.get_job(f"job_{physical}")

    assert client.delete(f"/v1/configs/{cid}", headers=alice["h"]).json()[
        "error"]["code"] == "confirmation_required"
    done = client.delete(f"/v1/configs/{cid}?confirm=true", headers=alice["h"])
    assert done.status_code == 200 and done.json(
    ) == {"deleted": True, "id": cid, "name": "x_gone"}

    assert physical not in tables_in_db() and physical not in models.metadata.tables
    assert catalog.scheduler.get_job(f"job_{physical}") is None
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(models.change_log).where(
            models.change_log.c.entity == physical)).scalar() == 0
        assert conn.execute(select(func.count()).select_from(models.scheduler_runs).where(
            models.scheduler_runs.c.entity == physical)).scalar() == 0
        assert conn.execute(select(func.count()).select_from(
            models.registry_configs).where(models.registry_configs.c.id == cid)).scalar() == 0
    assert client.get(f"/v1/configs/{cid}",
                      headers=alice["h"]).status_code == 404
    publish(client, alice, customers_cfg("x_gone"))  # the name is free again


# ------------------------------------------------------------- scheduler ----
def test_a_providers_config_is_kept_fresh_by_its_own_scheduler_job(client, alice):
    cust = publish(client, alice, customers_cfg("s_cust", count=5))
    ords = publish(client, alice, orders_cfg("s_cust", name="s_ord", count=10))
    entry = entry_of(ords["id"])
    configs, tables = catalog.scope(entry)
    before = client.get(
        f"/v1/configs/{ords['id']}/metrics", headers=alice["h"]).json()["row_count"]
    run_entity_job(engine, entry.physical, tables, configs)
    after = client.get(
        f"/v1/configs/{ords['id']}/metrics", headers=alice["h"]).json()
    assert after["row_count"] >= before and after["last_status"] == "success"
    runs = client.get(
        f"/v1/configs/{ords['id']}/runs", headers=alice["h"]).json()["runs"]
    assert runs and runs[0]["entity"] == "s_ord" and runs[0]["status"] == "success"
    # the tick only drew parents from this provider's own customers
    parent_ids = {r["customer_id"] for r in client.get(
        f"/v1/configs/{cust['id']}/data?limit=500", headers=alice["h"]).json()["items"]}
    child_rows = client.get(
        f"/v1/configs/{ords['id']}/data?limit=500", headers=alice["h"]).json()["items"]
    assert child_rows and {r["customer_id"] for r in child_rows} <= parent_ids


def test_the_scheduler_never_grows_a_config_past_its_row_cap(client, su, monkeypatch):
    p = make_provider(client, su, "Cap Carl")
    monkeypatch.setenv("MAX_ROWS_PER_CONFIG", "12")
    made = publish(client, p, customers_cfg("capped", count=10,
                   update_schedule={"new_records": [50, 60]}))
    entry = entry_of(made["id"])
    configs, tables = catalog.scope(entry)
    assert entry.cfg.max_rows == 12
    run_entity_job(engine, entry.physical, tables, configs)
    run_entity_job(engine, entry.physical, tables, configs)
    assert client.get(
        f"/v1/configs/{made['id']}/metrics", headers=p["h"]).json()["row_count"] == 12
