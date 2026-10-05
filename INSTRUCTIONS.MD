# INSTRUCTIONS — Hands-on Test Guide for the Synthetic Data Simulation Backend

A step-by-step manual for a **human tester** to exercise every feature of the backend: API keys, superuser vs provider privileges, publishing configs (YAML / JSON over the API), CRUD, change feed, batch generation, simulation, the scheduler, failure injection, exports, DDL, metrics, sharing/isolation, the CLI, and the error envelope.

Everything here was derived from the source code and README. Where something is _my reading of the code_ rather than something the README states, it is marked **(observed in code)**.

**All interactive examples below use Python (`requests`) instead of curl/bash.**

---

## Table of contents

- [Quick start (5 minutes)](#quick-start-5-minutes)
- [0. Before you start](#0-before-you-start)
- [1. Install, configure, run](#1-install-configure-run)
- [2. Who can do what (roles)](#2-who-can-do-what-roles)
- [3. Authentication tests](#3-authentication-tests)
- [4. Provider & API-key management (superuser)](#4-provider--api-key-management-superuser)
- [5. Configs: create, validate, list, edit, delete](#5-configs-create-validate-list-edit-delete)
- [6. CRUD on rows](#6-crud-on-rows)
- [7. The change feed](#7-the-change-feed)
- [8. Batch generation (append / replace / reset)](#8-batch-generation-append--replace--reset)
- [9. Simulate (inserts + updates + deletes on demand)](#9-simulate-inserts--updates--deletes-on-demand)
- [10. The scheduler](#10-the-scheduler)
- [11. Failure injection](#11-failure-injection)
- [12. Export (snapshot, delta, zip bundle)](#12-export-snapshot-delta-zip-bundle)
- [13. DDL export](#13-ddl-export)
- [14. Metrics and job runs](#14-metrics-and-job-runs)
- [15. Sharing & isolation between providers](#15-sharing--isolation-between-providers)
- [16. Superuser-only legacy routes (built-in entities)](#16-superuser-only-legacy-routes-built-in-entities)
- [17. CLI (no server needed)](#17-cli-no-server-needed)
- [18. Error reference](#18-error-reference)
- [19. Restart, Postgres, Docker, pytest](#19-restart-postgres-docker-pytest)
- [20. One-shot smoke-test script](#20-one-shot-smoke-test-script)
- [21. Master checklist](#21-master-checklist)
- [22. Gotchas worth knowing](#22-gotchas-worth-knowing)

---

## Quick start (5 minutes)

The shortest path from nothing to "I created an API key, published a YAML config and read generated data".

```python
# 1. Install (run once in a terminal)
#    python -m venv .venv && source .venv/bin/activate
#    pip install -r requirements.txt
#    pip install requests   # for the examples in this guide

# 2. Make the SUPERUSER key and start the server (terminal)
#    import subprocess, os
#    key = subprocess.check_output(["python", "-m", "app.cli", "genkey"], text=True).strip()
#    os.environ["API_KEY"] = key
#    print("SUPERUSER KEY:", key)   # keep it
#    # then: uvicorn app.main:app --reload

import json
from pathlib import Path
import requests

BASE = "http://127.0.0.1:8000"
SU = "<the superuser key from step 2>"   # replace

# 3. Create a PROVIDER + its API key  -> shown ONCE
r = requests.post(
    f"{BASE}/v1/admin/providers",
    headers={"X-API-Key": SU, "Content-Type": "application/json"},
    json={"full_name": "Acme Data Team"},
)
r.raise_for_status()
provider = r.json()
print(json.dumps(provider, indent=2))
KEY = provider["api_key"]   # shown once — store it

# 4. Publish a config (YAML straight from the configs/ folder)
yaml_body = Path("configs/customers.yaml").read_text()
r = requests.post(
    f"{BASE}/v1/configs",
    headers={"X-API-Key": KEY, "Content-Type": "text/yaml"},
    data=yaml_body,
)
r.raise_for_status()
config = r.json()
print(json.dumps(config, indent=2))
CID = config["id"]

# 5. Read the generated data and its change feed
data = requests.get(
    f"{BASE}/v1/configs/{CID}/data",
    headers={"X-API-Key": KEY},
    params={"limit": 5},
).json()
print(json.dumps(data, indent=2))

changes = requests.get(
    f"{BASE}/v1/configs/{CID}/changes",
    headers={"X-API-Key": KEY},
    params={"since": 0, "limit": 5},
).json()
print(json.dumps(changes, indent=2))
```

If step 4 returned `"seeded_rows": 30` you are done with the basics. The rest of this document tests everything else.

---

## 0. Before you start

**Tools:** Python 3.10+, `requests`. Optional: `sqlite3` CLI, `psql`, a zip library (stdlib `zipfile`). You can still use the Swagger UI (see [3.4](#34-swagger-ui-authorize-button)).

**Placeholders:** `<like-this>` means "replace me". `BASE`, `SU`, `KEY`, `KEY2`, `CID`, `OID` are variables used throughout.

**Paste this helper block once** — it keeps every later example short:

```python
import json
from pathlib import Path
from typing import Any, Optional

import requests

BASE = "http://127.0.0.1:8000"
SU = "<superuser key>"          # from API_KEY / API_KEYS
KEY = "<provider A key>"        # filled in section 4
KEY2 = "<provider B key>"       # filled in section 4

JSON = {"Content-Type": "application/json"}
YAML = {"Content-Type": "text/yaml"}


def _headers(key: str, extra: Optional[dict] = None) -> dict:
    h = {"X-API-Key": key}
    if extra:
        h.update(extra)
    return h


def su(method: str, path: str, **kwargs) -> requests.Response:
    """Act as the SUPERUSER."""
    return requests.request(method, f"{BASE}{path}", headers=_headers(SU, kwargs.pop("headers", None)), **kwargs)


def pv(method: str, path: str, **kwargs) -> requests.Response:
    """Act as PROVIDER A."""
    return requests.request(method, f"{BASE}{path}", headers=_headers(KEY, kwargs.pop("headers", None)), **kwargs)


def pv2(method: str, path: str, **kwargs) -> requests.Response:
    """Act as PROVIDER B."""
    return requests.request(method, f"{BASE}{path}", headers=_headers(KEY2, kwargs.pop("headers", None)), **kwargs)


def st(method: str, path: str, key: Optional[str] = None, **kwargs) -> int:
    """Print/return ONLY the HTTP status code."""
    headers = kwargs.pop("headers", {}) or {}
    if key:
        headers = {**_headers(key), **headers}
    r = requests.request(method, f"{BASE}{path}", headers=headers, **kwargs)
    print(r.status_code)
    return r.status_code


def pp(obj: Any) -> None:
    """Pretty-print JSON."""
    print(json.dumps(obj, indent=2, default=str))
```

Usage pattern:

```python
r = pv("POST", "/v1/configs", headers=YAML, data=Path("file.yaml").read_text())
pp(r.json())
st("GET", "/v1/me", key=KEY)   # prints status only
```

**Role tags** used in headings below:

| Tag        | Meaning                                              |
| ---------- | ---------------------------------------------------- |
| **[SU]**   | needs the **superuser** key (`API_KEY` / `API_KEYS`) |
| **[P]**    | works with a **provider** key (owner of the config)  |
| **[SU/P]** | either                                               |
| **[none]** | no key needed (`/health` only)                       |

---

## 1. Install, configure, run

### 1.1 Install and start

```python
# Terminal (once):
#   python -m venv .venv && source .venv/bin/activate
#   pip install -r requirements.txt
#   pip install requests

import os
import subprocess

key = subprocess.check_output(["python", "-m", "app.cli", "genkey"], text=True).strip()
os.environ["API_KEY"] = key
print("SUPERUSER KEY:", key)

# Then start the server in a separate process/terminal:
#   uvicorn app.main:app --reload
```

> **`.env` gotcha:** the app itself does **not** read `.env` (no dotenv code in `app/`). `cp .env.example .env` only helps if you load it: either `export` the variables, or start with `uvicorn app.main:app --env-file .env` (uvicorn's `[standard]` extra includes python-dotenv). `docker compose` _does_ read `.env` for `${API_KEY}`.
> The sample key in `.env.example` is **public** — never use it for anything real.

**On first start the app:** creates `synthetic.db` (SQLite) → creates all tables → seeds `customers` (30) → `orders` (50) → `support_tickets` (20), in dependency order → starts one background job per entity.

### 1.2 Verify it is alive

```python
r = requests.get(f"{BASE}/health")          # [none]
pp(r.json())                                # {"status": "ok"}

r = su("GET", "/entities")                  # [SU]
pp(r.json())                                # customers, orders, support_tickets

r = su("GET", "/metrics")                   # [SU]
pp(r.json())                                # row_count 30 / 50 / 20
```

### 1.3 Environment variables (complete list)

| Variable                   | Default                    | Effect                                                                                                                                  |
| -------------------------- | -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `API_KEY` / `API_KEYS`     | _(none)_                   | **Superuser** key(s). `API_KEYS` is comma-separated; both are read together, trimmed and de-duplicated. **No key = authentication OFF** |
| `REQUIRE_API_KEY`          | off                        | `true` → refuse to start without a key                                                                                                  |
| `ENABLE_DOCS`              | `true`                     | `false` → `/docs`, `/redoc`, `/openapi.json` return 404                                                                                 |
| `DATABASE_URL`             | `sqlite:///./synthetic.db` | e.g. `postgresql+psycopg2://user:pw@host:5432/db`                                                                                       |
| `CONFIG_DIR`               | `./configs`                | folder of built-in YAML entities                                                                                                        |
| `MAX_CONFIGS_PER_PROVIDER` | `10`                       | configs a provider can hold                                                                                                             |
| `MAX_FIELDS_PER_CONFIG`    | `40`                       | fields per config                                                                                                                       |
| `MAX_INITIAL_COUNT`        | `10000`                    | `seed.initial_count`                                                                                                                    |
| `MAX_ROWS_PER_CONFIG`      | `50000`                    | total rows per config (soft-deleted included)                                                                                           |
| `MAX_LATENCY_MS`           | `5000`                     | `failure_injection.latency_ms`                                                                                                          |
| `MAX_CONFIG_BYTES`         | `65536`                    | size of a config body                                                                                                                   |
| `TEST_DATABASE_URL`        | _(none)_                   | pytest only; the target DB's `public` schema is **wiped**                                                                               |

All of these are read when the process starts; **restart after changing any of them.** A non-integer `MAX_*` value stops start-up with a clear message (test it in [19.1](#191-startup-failure-modes)).

---

## 2. Who can do what (roles)

There are three kinds of caller.

| Caller                  | How they authenticate                                | Notes                                                                  |
| ----------------------- | ---------------------------------------------------- | ---------------------------------------------------------------------- |
| **Superuser**           | key in `API_KEY` / `API_KEYS` (environment)          | one role, no per-admin identity or audit trail                         |
| **Provider**            | key issued by the superuser, stored hashed in the DB | has an `id` (`prov_…`) and `full_name`; many keys per provider allowed |
| **Anonymous superuser** | _no key configured at all_                           | auth is OFF; everyone is superuser; provider keys have no effect       |

| Capability                                                                                                                                                  |           Superuser           |  Provider (owner)   |             Provider (not owner)             |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------- | :---------------------------: | :-----------------: | :------------------------------------------: |
| Create/list/rename/deactivate providers, issue/revoke keys (`/v1/admin/*`)                                                                                  |              yes              |       **403**       |                   **403**                    |
| Publish configs (`POST /v1/configs`)                                                                                                                        | yes (can add `?provider_id=`) | yes (for self only) |                      —                       |
| Read a config, rows, changes, export, DDL, metrics                                                                                                          |   yes (all, incl. private)    |         yes         | yes — **unless `is_only_me`** (then **404**) |
| Edit/delete config; add/update/delete rows; batch; simulate                                                                                                 |              yes              |         yes         |       **403** (or **404** if private)        |
| Legacy routes: `/orders`, `/customers`, `/support_tickets`, `/entities`, `/metrics`, `/scheduler/runs`, `/ddl`, `/export`, `/admin/batch`, `/admin/changes` |              yes              |       **403**       |                   **403**                    |
| `GET /health`                                                                                                                                               |              yes              |         yes         |           yes (**no key needed**)            |

The built-in YAML entities are owned by a system provider (`prov_system`, ids `cfg_sys_customers`, `cfg_sys_orders`, `cfg_sys_support_tickets`). They are **public** (everyone can read) and **writable only by the superuser**.

---

## 3. Authentication tests

### 3.1 Public route

```python
r = requests.get(f"{BASE}/health")   # 200 with NO key (the only unauthenticated API route)
print(r.status_code, r.json())
```

### 3.2 Missing / wrong key → 401 with the standard envelope

```python
for headers in ({}, {"X-API-Key": "nope"}, {"X-API-Key": ""}):
    r = requests.get(f"{BASE}/v1/me", headers=headers)
    print(r.status_code, r.headers.get("WWW-Authenticate"), r.json())
# Expect: 401, WWW-Authenticate: ApiKey,
# body {"error":{"code":"unauthorized","message":"invalid or missing API key"}}
```

### 3.3 The key must travel in the `X-API-Key` header — nothing else works

```python
st("GET", f"/v1/me?api_key={SU}")                              # 401 (keys in URLs are refused)
st("GET", "/v1/me", headers={"Authorization": f"Bearer {SU}"}) # 401
st("GET", "/v1/me", key=SU[:-1])                               # 401 (near-miss key)
st("GET", "/v1/me", key=SU)                                    # 200
```

Failed attempts are logged by the server (method, path, client address) — **never the key**. Check the uvicorn console.

### 3.4 Swagger UI Authorize button

Open `{BASE}/docs` → **Authorize** → paste a key → _Authorize_. Every "Try it out" now sends `X-API-Key`. Use the superuser key to try everything, or a provider key to see 403s on admin/legacy routes. (`/docs` itself is public unless `ENABLE_DOCS=false`.)

### 3.5 Auth OFF mode (local dev)

Start the server with **no** `API_KEY`/`API_KEYS`:

```python
# Terminal: unset API_KEY API_KEYS; uvicorn app.main:app

r = requests.get(f"{BASE}/v1/me")
pp(r.json())
# role "superuser", full_name "anonymous (authentication disabled)"

r = requests.get(f"{BASE}/v1/me", headers={"X-API-Key": "anything"})
pp(r.json())
# still superuser: keys are ignored
```

**Expect** a startup warning that authentication is **DISABLED**. Provider keys only take effect once a superuser key is set.

### 3.6 `REQUIRE_API_KEY` fails fast

```python
# Terminal:
#   unset API_KEY API_KEYS
#   REQUIRE_API_KEY=true uvicorn app.main:app
# Expect: the process exits with an error mentioning REQUIRE_API_KEY and genkey.
```

### 3.7 Rotate the superuser key without downtime

```python
import subprocess

new_key = subprocess.check_output(["python", "-m", "app.cli", "genkey"], text=True).strip()
# 1. Restart with both: API_KEYS=<new>,<old> uvicorn app.main:app
# 2. Verify both work:
st("GET", "/v1/me", key="<old>")   # 200
st("GET", "/v1/me", key=new_key)   # 200
# 3. Move clients to new; restart with API_KEYS=<new> only
# 4. Verify old returns 401
st("GET", "/v1/me", key="<old>")   # 401
```

### 3.8 Turn docs off

```python
# Terminal: ENABLE_DOCS=false API_KEY=$SU uvicorn app.main:app
st("GET", "/docs")           # 404
st("GET", "/openapi.json")   # 404
st("GET", "/health")         # 200
```

### 3.9 A key shorter than 24 characters

Start with `API_KEY=short`: the server starts but logs a warning recommending `genkey`.

---

## 4. Provider & API-key management (superuser)

A **provider** is a publisher of configs (an `id` + `full_name`). There is **no open sign-up** — only the superuser can create providers. Providers **cannot be deleted** (there is no route); they can only be **deactivated**.

### 4.1 Create a provider and its first key **[SU]**

```python
r = su("POST", "/v1/admin/providers", headers=JSON, json={"full_name": "Alice Almeida"})
r.raise_for_status()
alice = r.json()
pp(alice)
KEY = alice["api_key"]
A_ID = alice["id"]
A_KEYID = alice["key"]["id"]

r = su("POST", "/v1/admin/providers", headers=JSON, json={"full_name": "Bob Bautista"})
r.raise_for_status()
bob = r.json()
pp(bob)
KEY2 = bob["api_key"]
B_ID = bob["id"]
```

**Expect `201`** with: `id` (`prov_…`), `full_name`, `is_active: true`, `is_system: false`, `api_key` (the secret — **shown exactly once**), `key` (`id`, `key_prefix` = first 8 chars of the secret, `label: "initial"`), and a `note` telling you to store it now.
`full_name` is trimmed and must be 1–100 characters; it is **not unique** (two "Twin" providers get different ids and keys).

**Negative tests**

```python
for body in (
    {"full_name": ""},
    {"full_name": "   "},
    {"full_name": "x" * 101},
    {},
):
    r = su("POST", "/v1/admin/providers", headers=JSON, json=body)
    print(r.status_code, r.json())   # 422 invalid_provider / validation_error
```

### 4.2 Create a provider from the CLI (no server needed) **[operator]**

```python
# Terminal (or subprocess):
#   python -m app.cli create-provider --full-name "Hana Hidalgo"
# Prints JSON with id and api_key (once). Writes straight to DATABASE_URL.
```

### 4.3 Who am I? **[SU/P]**

```python
pp(pv("GET", "/v1/me").json())   # role "provider", id, full_name, configs_owned, limits{...}
pp(su("GET", "/v1/me").json())   # role "superuser", id null
```

`limits` shows the live quotas (`max_configs`, `max_fields`, `max_initial_count`, `max_rows`, `max_latency_ms`, `max_body_bytes`).

### 4.4 List providers **[SU]**

```python
pp(su("GET", "/v1/admin/providers").json())
```

Each provider shows `config_count` and its `keys[]` (`id`, `key_prefix`, `label`, `created_at`, `revoked_at`, `last_used_at`). **Secrets are never listed again.** The built-in `prov_system` appears with `is_system: true` and no keys.

### 4.5 `last_used_at`

Before first use `last_used_at` is `null`. Call `pv("GET", "/v1/me")`, then list providers again → it is filled in. (It is refreshed at most every 5 minutes.)

### 4.6 Rename, deactivate, reactivate **[SU]**

```python
pp(su("PATCH", f"/v1/admin/providers/{A_ID}", headers=JSON, json={"full_name": "Alice A."}).json())
pp(su("PATCH", f"/v1/admin/providers/{A_ID}", headers=JSON, json={"is_active": False}).json())
st("GET", "/v1/me", key=KEY)   # 401 — keys of an inactive provider are locked out
pp(su("PATCH", f"/v1/admin/providers/{A_ID}", headers=JSON, json={"is_active": True}).json())
st("GET", "/v1/me", key=KEY)   # 200 — restored
```

> A deactivated provider's **public configs stay readable and keep updating** — deactivation only locks its keys out.

### 4.7 Issue a second key and rotate **[SU]**

```python
r = su("POST", f"/v1/admin/providers/{A_ID}/keys", headers=JSON, json={"label": "ci"})
r.raise_for_status()
newkey = r.json()
pp(newkey)
KEY_NEW = newkey["api_key"]
st("GET", "/v1/me", key=KEY_NEW)   # 200 — both keys work now

# revoke the old one
pp(su("DELETE", f"/v1/admin/providers/{A_ID}/keys/{A_KEYID}").json())
st("GET", "/v1/me", key=KEY)       # 401 — old key is dead
st("GET", "/v1/me", key=KEY_NEW)   # 200 — new key keeps working
KEY = KEY_NEW                      # carry on with the new key
```

The body is optional (`POST …/keys` with no body works; `label` ≤ 100 chars → otherwise `422 invalid_label`). Revoking twice is harmless.

**Negative tests**

```python
print(su("DELETE", f"/v1/admin/providers/{A_ID}/keys/key_nope").status_code)          # 404
print(su("DELETE", f"/v1/admin/providers/{B_ID}/keys/{A_KEYID}").status_code)         # 404
print(su("POST", "/v1/admin/providers/prov_nope/keys").status_code)                   # 404
print(su("PATCH", "/v1/admin/providers/prov_system", headers=JSON, json={"is_active": False}).status_code)  # 409
print(su("POST", "/v1/admin/providers/prov_system/keys").status_code)                 # 409
```

### 4.8 Providers cannot escalate **[P → expect 403]**

```python
specs = [
    ("POST", "/v1/admin/providers"),
    ("GET", "/v1/admin/providers"),
    ("PATCH", f"/v1/admin/providers/{A_ID}"),
    ("POST", f"/v1/admin/providers/{A_ID}/keys"),
    ("DELETE", f"/v1/admin/providers/{A_ID}/keys/{A_KEYID}"),
]
for method, path in specs:
    r = pv(method, path, headers=JSON, json={"full_name": "x"})
    print(f"{method:7} {path:55} {r.status_code}")
# Expect 403 on every line, body {"error":{"code":"forbidden","message":"this route needs a superuser API key"}}
```

### 4.9 Keys are stored hashed

Only a SHA-256 of each key is stored (look at the `registry_api_keys` table: `key_hash`, `key_prefix`).

```python
import hashlib
import sqlite3

conn = sqlite3.connect("synthetic.db")
rows = conn.execute("SELECT id, key_prefix, substr(key_hash,1,12), revoked_at FROM registry_api_keys").fetchall()
for row in rows:
    print(row)
print(hashlib.sha256(KEY.encode()).hexdigest())  # must match the full key_hash of that key
```

---

## 5. Configs: create, validate, list, edit, delete

A **config** describes one entity (a table + its generation rules + its update schedule). Publishing one creates a real table, seeds it, and registers a recurring background job. Nothing in Python changes.

### 5.1 Anatomy of a config (annotated)

```yaml
entity: acme_customers # or `name:`. 1-32 chars: ^[a-z][a-z0-9_]{0,31}$ ; "schema" and "manifest" are reserved
is_only_me: false # false (default) = other providers may READ it; true = private (404 to everyone but owner/superuser)

fields:
  customer_id: { type: uuid, primary_key: true } # exactly ONE pk, and it must be uuid
  full_name: { type: string, key_label: full_name } # real-looking names via iki-data-generator
  email: { type: string, key_label: email_address } # another iki provider
  segment: { type: enum, values: [retail, wholesale, enterprise] }
  lifetime_value: { type: float, min: 0, max: 10000 }
  loyalty_points: { type: int, min: 0, max: 5000 }
  is_vip: { type: bool }
  signup_at: { type: timestamp } # plain timestamp (no auto)
  notes: { type: string, nullable: true }
  created_at: { type: timestamp, auto: created }
  updated_at: { type: timestamp, auto: updated }
  deleted_at: { type: timestamp, nullable: true, auto: soft_delete }
  # `version: {type: int, auto: version}` is added for you if you omit it

seed:
  initial_count: 25 # rows generated at publish time (0 .. MAX_INITIAL_COUNT)

update_schedule:
  cadence: hourly # hourly | daily | weekly
  new_records: [1, 5] # random number inserted per run: [low, high], 0..1000
  mutate_existing_pct: 5 # % of live rows rewritten per run (0..100)
  soft_delete_pct: 1 # % of the remaining rows soft-deleted per run (0..100)
  jitter_seconds: 30 # random start offset, 0..3600

failure_injection:
  fail_rate: 0.0 # 0.0..1.0 probability of an injected HTTP 500
  latency_ms: 0 # artificial delay per row request, 0..MAX_LATENCY_MS
```

**Field types:** `uuid`, `int`, `float`, `string`, `enum`, `bool`, `timestamp`, `ref`.

| Field attribute                             | Rules                                                                                                                                                                            |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `type`                                      | required; one of the eight above                                                                                                                                                 |
| `primary_key`                               | exactly one per config; must be `type: uuid` with no `auto`                                                                                                                      |
| `nullable`                                  | allows `null` on writes (always true for the soft-delete field)                                                                                                                  |
| `min` / `max`                               | range for `int` / `float` generation (default 0–1000); `min > max` is rejected                                                                                                   |
| `values`                                    | **enum only**: 1–50 distinct, non-empty strings, each ≤ 64 chars                                                                                                                 |
| `ref_entity`                                | **ref only**: must be one of **your own** configs; no self-reference; no cycles                                                                                                  |
| `auto: created` / `updated` / `soft_delete` | need `type: timestamp`; only **one** `soft_delete` field allowed                                                                                                                 |
| `auto: version`                             | field must be named `version`, `type: int`; it is auto-added if missing                                                                                                          |
| `key_label`                                 | (≤ 64 chars) hand the field to **iki-data-generator** (e.g. `full_name`, `email_address`, `job_title`); the config is trial-generated at publish time so a bad label fails _now_ |
| `ik_options`                                | passed through unchanged to Iki; **requires** `key_label`; ≤ 2000 bytes of JSON                                                                                                  |

Other rules: field names `^[a-z][a-z0-9_]{0,39}$` · unknown keys anywhere are **rejected** (typos fail loudly) · YAML anchors/aliases are **rejected** · a config name is unique per provider (two providers may each have an `orders`).
`failure_injection.drift` is accepted by the parser but **has no effect** (schema-drift injection is listed as not implemented).

### 5.2 Save three example configs (a 3-level chain)

```python
ACME_CUSTOMERS_YAML = """\
entity: acme_customers
fields:
  customer_id:    { type: uuid, primary_key: true }
  full_name:      { type: string, key_label: full_name }
  email:          { type: string, key_label: email_address }
  segment:        { type: enum, values: [retail, wholesale, enterprise] }
  lifetime_value: { type: float, min: 0, max: 10000 }
  loyalty_points: { type: int, min: 0, max: 5000 }
  is_vip:         { type: bool }
  signup_at:      { type: timestamp }
  notes:          { type: string, nullable: true }
  created_at:     { type: timestamp, auto: created }
  updated_at:     { type: timestamp, auto: updated }
  deleted_at:     { type: timestamp, nullable: true, auto: soft_delete }
seed: { initial_count: 25 }
update_schedule: { cadence: hourly, new_records: [1, 5], mutate_existing_pct: 5, soft_delete_pct: 1, jitter_seconds: 30 }
failure_injection: { fail_rate: 0.0, latency_ms: 0 }
"""

ACME_ORDERS_YAML = """\
entity: acme_orders
fields:
  order_id:    { type: uuid, primary_key: true }
  customer_id: { type: ref, ref_entity: acme_customers }
  amount:      { type: float, min: 10, max: 500 }
  status:      { type: enum, values: [pending, shipped, cancelled] }
  note:        { type: string, nullable: true }
  created_at:  { type: timestamp, auto: created }
  updated_at:  { type: timestamp, auto: updated }
  deleted_at:  { type: timestamp, nullable: true, auto: soft_delete }
seed: { initial_count: 40 }
update_schedule: { cadence: hourly, new_records: [3, 8], mutate_existing_pct: 5, soft_delete_pct: 1, jitter_seconds: 30 }
failure_injection: { fail_rate: 0.0, latency_ms: 0 }
"""

ACME_TICKETS_YAML = """\
entity: acme_tickets
fields:
  ticket_id:       { type: uuid, primary_key: true }
  order_id:        { type: ref, ref_entity: acme_orders }
  agent_job_title: { type: string, key_label: job_title }
  category:        { type: enum, values: [billing, shipping, product_defect, account, other] }
  priority:        { type: int, min: 1, max: 5 }
  is_escalated:    { type: bool }
  created_at:      { type: timestamp, auto: created }
  updated_at:      { type: timestamp, auto: updated }
  deleted_at:      { type: timestamp, nullable: true, auto: soft_delete }
seed: { initial_count: 15 }
update_schedule: { cadence: daily, new_records: [1, 4], mutate_existing_pct: 10, soft_delete_pct: 3, jitter_seconds: 60 }
failure_injection: { fail_rate: 0.0, latency_ms: 0 }
"""

Path("acme_customers.yaml").write_text(ACME_CUSTOMERS_YAML)
Path("acme_orders.yaml").write_text(ACME_ORDERS_YAML)
Path("acme_tickets.yaml").write_text(ACME_TICKETS_YAML)
```

### 5.3 Ways to get a config into the system

| Way                                      | Who                   | How                                                      | Notes                                                                                                                                                                                                                                                                                                        |
| ---------------------------------------- | --------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **A. Built-in YAML file**                | operator              | drop `configs/<name>.yaml`, **restart** the server       | owned by `prov_system`, id `cfg_sys_<name>`, public, **writable only by the superuser**, no row quota. Invalid refs/types/cycles fail at **startup**. Per-entity routes `/<name>` appear for the superuser. File configs are **not** checked for unknown keys the way API configs are **(observed in code)** |
| **B. API — YAML body**                   | provider or superuser | `POST /v1/configs` with `Content-Type: text/yaml`        | a file from `configs/` posts as-is                                                                                                                                                                                                                                                                           |
| **C. API — JSON body**                   | provider or superuser | `POST /v1/configs` with `Content-Type: application/json` | any content-type that isn't JSON is parsed as YAML (YAML ⊇ JSON)                                                                                                                                                                                                                                             |
| **D. Superuser on behalf of a provider** | superuser             | `POST /v1/configs?provider_id=prov_…`                    | without `provider_id` the config is owned by the system provider                                                                                                                                                                                                                                             |

### 5.4 Dry-run validation (saves nothing) **[SU/P]**

```python
r = pv("POST", "/v1/configs/validate", headers=YAML, data=Path("acme_customers.yaml").read_text())
pp(r.json())
# Expect 200: {"valid": true, "name_available": true, "is_only_me": false, "config": {…normalized, with version added…}}
```

### 5.5 Publish **[SU/P]**

```python
# parents first
r = pv("POST", "/v1/configs", headers=YAML, data=Path("acme_customers.yaml").read_text())
r.raise_for_status()
c = r.json()
pp({k: c[k] for k in ("id", "name", "owner", "is_only_me", "source", "can_write", "cadence", "seeded_rows") if k in c})
CID = c["id"]

r = pv("POST", "/v1/configs", headers=YAML, data=Path("acme_orders.yaml").read_text())
r.raise_for_status()
OID = r.json()["id"]
print("orders seeded_rows:", r.json()["seeded_rows"])

r = pv("POST", "/v1/configs", headers=YAML, data=Path("acme_tickets.yaml").read_text())
r.raise_for_status()
TID = r.json()["id"]
print("tickets seeded_rows:", r.json()["seeded_rows"])
```

**Expect `201`** each: `id` (`cfg_<16 hex>`), `name`, `owner{id,full_name}`, `is_only_me`, `source: "api"`, `can_write: true`, `cadence`, `fields[]`, `created_at`, `updated_at`, `config{…}`, and `seeded_rows` (25 / 40 / 15).

Same thing as JSON:

```python
r = pv("POST", "/v1/configs", headers=JSON, json={
    "entity": "scratch",
    "fields": {
        "id": {"type": "uuid", "primary_key": True},
        "label": {"type": "string"},
    },
    "seed": {"initial_count": 6},
})
r.raise_for_status()
SID = r.json()["id"]
print("scratch seeded_rows:", r.json()["seeded_rows"])
```

Check the table namespacing (SQLite): internal tables look like `d_<10 hex>_acme_customers`. **You never see those names in API responses, DDL, exports or error messages.**

### 5.6 List and read configs **[SU/P]**

```python
items = pv("GET", "/v1/configs").json()["items"]
for i in items:
    print(i["id"], i["name"], i["owner"]["full_name"], i.get("source"), i.get("can_write"))

print("mine:", [i["name"] for i in pv("GET", "/v1/configs", params={"scope": "mine"}).json()["items"]])
print("shared:", [i["name"] for i in pv("GET", "/v1/configs", params={"scope": "shared"}).json()["items"]])
pp(pv("GET", f"/v1/configs/{CID}").json())   # full definition under "config"
```

The list is a summary (no `config` key). `can_write` tells you whether _you_ may change it. `scope` other than `mine|shared|all` → `422`.

### 5.7 Edit a config (`PATCH`) **[SU/P — owner]**

| Key you may send                               | Behaviour                                                                                                                                                                                                |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `is_only_me`                                   | toggles privacy; nothing else changes                                                                                                                                                                    |
| `seed`, `update_schedule`, `failure_injection` | each is **merged** into the existing object; takes effect **immediately** and the background job is **rescheduled**                                                                                      |
| `fields`                                       | **replaced wholesale** (not merged!). Drops the table, **deletes all data**, restarts the change feed at version 1 and re-seeds. Needs `?confirm=true`; refused while another config references this one |
| `name` / `entity`                              | **cannot change** (delete and recreate)                                                                                                                                                                  |

```python
# merge schedule + failure settings
r = pv("PATCH", f"/v1/configs/{CID}", headers=JSON, json={
    "update_schedule": {"cadence": "daily", "new_records": [2, 6]},
    "failure_injection": {"latency_ms": 100},
})
body = r.json()
print(body.get("data_reset"), body["config"]["update_schedule"], body["config"]["failure_injection"])
# -> data_reset: False; cadence daily; new_records [2,6]; mutate/soft_delete/jitter unchanged (merged)

# put things back
pv("PATCH", f"/v1/configs/{CID}", headers=JSON, json={
    "update_schedule": {"cadence": "hourly", "new_records": [1, 5]},
    "failure_injection": {"latency_ms": 0},
})

# privacy toggle
print(pv("PATCH", f"/v1/configs/{CID}", headers=JSON, json={"is_only_me": True}).json()["is_only_me"])
print(pv("PATCH", f"/v1/configs/{CID}", headers=JSON, json={"is_only_me": False}).json()["is_only_me"])

# seed.initial_count alone does NOT add rows; it sets the default for batch/re-seed
pp(pv("PATCH", f"/v1/configs/{SID}", headers=JSON, json={"seed": {"initial_count": 8}}).json())
```

**Destructive: replace `fields`** (use the scratch config):

```python
NEWF = {
    "fields": {
        "id": {"type": "uuid", "primary_key": True},
        "label": {"type": "string"},
        "qty": {"type": "int", "min": 1, "max": 9},
    },
    "seed": {"initial_count": 3},
}
r = pv("PATCH", f"/v1/configs/{SID}", headers=JSON, json=NEWF)
print(r.status_code, r.json().get("error"))   # 400 confirmation_required (data untouched)

r = pv("PATCH", f"/v1/configs/{SID}", params={"confirm": "true"}, headers=JSON, json=NEWF)
print(r.json().get("data_reset"), r.json().get("seeded_rows"))   # True, 3
pp(pv("GET", f"/v1/configs/{SID}/data").json()["items"][0])     # now has "qty"
versions = [c["version"] for c in pv("GET", f"/v1/configs/{SID}/changes", params={"since": 0}).json()["changes"]]
print(versions)   # [1, 2, 3] — the feed restarted
```

> Consumers polling the old feed must re-baseline from `since=0` after a field change.

**PATCH negative tests**

```python
cases = [
    {},
    {"name": "renamed"},
    {"entity": "renamed"},
    {"nope": 1},
    {"is_only_me": "yes"},
    {"seed": "x"},
    {"failure_injection": {"fail_rate": 3}},
]
for body in cases:
    r = pv("PATCH", f"/v1/configs/{CID}", headers=JSON, json=body)
    print(r.status_code, body)
```

**Built-in configs** (superuser):

```python
r = su("PATCH", "/v1/configs/cfg_sys_orders", headers=JSON, json={"failure_injection": {"latency_ms": 1}})
print(r.status_code, r.json().get("error"))   # 409 managed_by_yaml

r = su("PATCH", "/v1/configs/cfg_sys_support_tickets", headers=JSON, json={"is_only_me": True})
print(r.json()["is_only_me"])   # allowed
su("PATCH", "/v1/configs/cfg_sys_support_tickets", headers=JSON, json={"is_only_me": False})
```

### 5.8 Delete a config **[SU/P — owner]**

```python
r = pv("DELETE", f"/v1/configs/{SID}")
print(r.json().get("error"))   # 400 confirmation_required

r = pv("DELETE", f"/v1/configs/{SID}", params={"confirm": "true"})
pp(r.json())   # {"deleted": true, "id": "cfg_…", "name": "scratch"}
st("GET", f"/v1/configs/{SID}", key=KEY)   # 404
```

Deleting drops the table **and** its change history, unschedules its job, and frees a quota slot and the name. Built-in configs: `409 managed_by_yaml`.

### 5.9 Dependencies (parents, children, cycles)

```python
# 1. a child can't reference a config you don't have
lonely = ACME_ORDERS_YAML.replace("acme_customers", "no_such_parent").replace(
    "entity: acme_orders", "entity: lonely_orders"
)
r = pv("POST", "/v1/configs/validate", headers=YAML, data=lonely)
print(r.json()["error"]["message"])   # "…not one of your configs…"

# 2. a parent with ZERO rows can't seed a child
ep = pv("POST", "/v1/configs", headers=JSON, json={
    "entity": "empty_parent",
    "fields": {"id": {"type": "uuid", "primary_key": True}},
    "seed": {"initial_count": 0},
}).json()
r = pv("POST", "/v1/configs", headers=JSON, json={
    "entity": "starved_child",
    "fields": {
        "id": {"type": "uuid", "primary_key": True},
        "p": {"type": "ref", "ref_entity": "empty_parent"},
    },
    "seed": {"initial_count": 3},
})
print(r.status_code, r.json().get("error"))   # 409 missing_parent_rows

# give the parent rows, then the same child POST succeeds
pv("POST", f"/v1/configs/{ep['id']}/batch", headers=JSON, json={"count": 3})
# ...now publish starved_child. Clean up later with DELETE ?confirm=true (child first).

# 3. a parent that is referenced can't be deleted or have its fields changed
r = pv("DELETE", f"/v1/configs/{CID}", params={"confirm": "true"})
print(r.status_code, r.json().get("error"))   # 409 has_dependents (lists acme_orders)
# delete children first: tickets -> orders -> customers
```

### 5.10 Bad-config gauntlet (every message should be clear)

```python
def try_cfg(label: str, body: dict) -> None:
    r = pv("POST", "/v1/configs/validate", headers=JSON, json=body)
    msg = r.json().get("error", {}).get("message", "VALID (unexpected!)")
    if isinstance(msg, list):
        msg = str(msg)
    print(f"{label:22} {str(msg)[:120]}")

PK = {"a": {"type": "uuid", "primary_key": True}}

try_cfg("bad name",          {"entity": "Bad Name", "fields": PK})
try_cfg("reserved name",     {"entity": "manifest", "fields": PK})
try_cfg("unknown top key",   {"entity": "t1", "update_schedul": {}, "fields": PK})
try_cfg("no fields",         {"entity": "t1", "fields": {}})
try_cfg("no primary key",    {"entity": "t1", "fields": {"a": {"type": "string"}}})
try_cfg("two primary keys",  {"entity": "t1", "fields": {**PK, "b": {"type": "uuid", "primary_key": True}}})
try_cfg("pk not uuid",       {"entity": "t1", "fields": {"a": {"type": "int", "primary_key": True}}})
try_cfg("unsupported type",  {"entity": "t1", "fields": {**PK, "b": {"type": "decimal"}}})
try_cfg("enum w/o values",   {"entity": "t1", "fields": {**PK, "b": {"type": "enum"}}})
try_cfg("values on string",  {"entity": "t1", "fields": {**PK, "b": {"type": "string", "values": ["x"]}}})
try_cfg("ref to nothing",    {"entity": "t1", "fields": {**PK, "b": {"type": "ref", "ref_entity": "nope"}}})
try_cfg("self reference",    {"entity": "t1", "fields": {**PK, "b": {"type": "ref", "ref_entity": "t1"}}})
try_cfg("min > max",         {"entity": "t1", "fields": {**PK, "b": {"type": "int", "min": 5, "max": 1}}})
try_cfg("unknown field key", {"entity": "t1", "fields": {"a": {"type": "uuid", "primary_key": True, "bogus": 1}}})
try_cfg("bad version",       {"entity": "t1", "fields": {**PK, "version": {"type": "string"}}})
try_cfg("auto on string",    {"entity": "t1", "fields": {**PK, "b": {"type": "string", "auto": "created"}}})
try_cfg("two soft_delete",   {"entity": "t1", "fields": {
    **PK,
    "d1": {"type": "timestamp", "auto": "soft_delete"},
    "d2": {"type": "timestamp", "auto": "soft_delete"},
}})
try_cfg("bad key_label",     {"entity": "t1", "fields": {**PK, "b": {"type": "string", "key_label": "no_such_provider"}}})
try_cfg("ik_options alone",  {"entity": "t1", "fields": {**PK, "b": {"type": "string", "ik_options": {"x": 1}}}})
try_cfg("cadence minutely",  {"entity": "t1", "update_schedule": {"cadence": "minutely"}, "fields": PK})
try_cfg("new_records 9>1",   {"entity": "t1", "update_schedule": {"new_records": [9, 1]}, "fields": PK})
try_cfg("new_records big",   {"entity": "t1", "update_schedule": {"new_records": [0, 5000]}, "fields": PK})
try_cfg("pct 150",           {"entity": "t1", "update_schedule": {"mutate_existing_pct": 150}, "fields": PK})
try_cfg("fail_rate 1.5",     {"entity": "t1", "failure_injection": {"fail_rate": 1.5}, "fields": PK})
try_cfg("latency 1e6",       {"entity": "t1", "failure_injection": {"latency_ms": 1_000_000}, "fields": PK})
try_cfg("initial_count -1",  {"entity": "t1", "seed": {"initial_count": -1}, "fields": PK})
try_cfg("is_only_me yes",    {"entity": "t1", "is_only_me": "yes", "fields": PK})
try_cfg("entity != name",    {"entity": "t1", "name": "t2", "fields": PK})
```

Each must come back **`422 invalid_config`** and — importantly — **create nothing**.

**Body-level problems**

```python
r = pv("POST", "/v1/configs/validate", headers=JSON, data=b"{not json")
print(r.json()["error"]["message"])

r = pv("POST", "/v1/configs/validate", headers=YAML, data=b"- a\n- list\n")
print(r.json()["error"]["message"])

r = pv("POST", "/v1/configs/validate", headers=YAML, data=b"a: &a [1,2,3]\nb: [*a,*a]\n")
print(r.json()["error"]["message"])   # anchors not allowed

r = pv("POST", "/v1/configs/validate", headers=YAML, data=("x: " + "y" * 70_000).encode())
print(r.status_code, r.json().get("error"))   # 413 payload_too_large
```

**Duplicate name:** publish `acme_customers.yaml` again → `409 already_exists`.

### 5.11 Quotas — test them by starting the server with small limits

Use a **fresh database**:

```python
# Terminal:
#   DATABASE_URL=sqlite:///./quota_test.db \
#   MAX_CONFIGS_PER_PROVIDER=2 MAX_FIELDS_PER_CONFIG=9 MAX_INITIAL_COUNT=20 \
#   MAX_LATENCY_MS=50 MAX_ROWS_PER_CONFIG=30 \
#   API_KEY=$SU uvicorn app.main:app --port 8001
# Then set BASE = "http://127.0.0.1:8001"
```

| Try                                                 | Expect                                         |
| --------------------------------------------------- | ---------------------------------------------- |
| a 3rd config for one provider                       | `409 quota_exceeded` (delete one → slot freed) |
| a config with > 9 fields                            | `422` "too many fields"                        |
| `initial_count: 21`                                 | `422`                                          |
| `latency_ms: 51`                                    | `422`                                          |
| batch/simulate that would pass 30 rows (see §8, §9) | `422 quota_exceeded`                           |
| scheduler tick near the cap                         | grows only up to the cap, never past it        |

> Tip: `pv("GET", "/v1/me").json()["limits"]` always shows the limits the server actually runs with. Row caps apply to **provider configs** only; built-in YAML configs have no cap.

### 5.12 Superuser publishing for someone else

```python
r = su("POST", "/v1/configs", params={"provider_id": B_ID}, headers=JSON, json={
    "entity": "bobs_things",
    "fields": {"id": {"type": "uuid", "primary_key": True}, "label": {"type": "string"}},
    "seed": {"initial_count": 4},
})
pp({"id": r.json()["id"], "owner": r.json()["owner"]})
print([i["name"] for i in pv2("GET", "/v1/configs", params={"scope": "mine"}).json()["items"]])

r = pv("POST", "/v1/configs", params={"provider_id": B_ID}, headers=JSON, json={})
print(r.status_code, r.json().get("error"))   # Alice -> 403

r = su("POST", "/v1/configs", params={"provider_id": "prov_nope"}, headers=JSON, json={})
print(r.status_code, r.json().get("error"))   # 404
```

---

## 6. CRUD on rows

Rows live under `/v1/configs/{config_id}/data`. **Reads:** owner, superuser, or anyone if the config is public. **Writes:** owner or superuser only. Examples use `$OID` (`acme_orders`).

### 6.1 Create **[P/SU]**

```python
# a) completely generated row (empty body)
r = pv("POST", f"/v1/configs/{OID}/data", headers=JSON, json={})
pp(r.json())

# b) you set some fields, the rest are generated
r = pv("POST", f"/v1/configs/{OID}/data", headers=JSON, json={
    "amount": 99.5, "status": "pending", "note": "hello",
})
r.raise_for_status()
row = r.json()
RID = row["order_id"]
pp(row)

# c) pin the relationship to a real parent
cust = pv("GET", f"/v1/configs/{CID}/data", params={"limit": 1}).json()["items"][0]["customer_id"]
r = pv("POST", f"/v1/configs/{OID}/data", headers=JSON, json={"customer_id": cust, "amount": 10})
pp({"order_id": r.json()["order_id"], "customer_id": r.json()["customer_id"], "amount": r.json()["amount"]})
```

**Expect `201`** and the full row: generated `order_id`, `version` (next table-wide number), `created_at`/`updated_at` set, `deleted_at: null`.

### 6.2 List + keyset pagination **[SU/P]**

```python
seen = []
after = None
for _ in range(20):
    params = {"limit": 10}
    if after:
        params["after"] = after
    page = pv("GET", f"/v1/configs/{OID}/data", params=params).json()
    seen.extend(r["order_id"] for r in page["items"])
    after = page.get("next_after")
    if after is None:
        break
print(len(seen), len(set(seen)))   # every live row exactly once
```

`limit` 1–500 (default 50). Soft-deleted rows are hidden.

```python
print(pv("GET", f"/v1/configs/{OID}/data", params={"limit": 501}).status_code)  # 422
print(pv("GET", f"/v1/configs/{OID}/data", params={"limit": 0}).status_code)    # 422
```

### 6.3 Get one row **[SU/P]**

```python
pp(pv("GET", f"/v1/configs/{OID}/data/{RID}").json())
r = pv("GET", f"/v1/configs/{OID}/data/nope")
print(r.status_code, r.json()["error"]["message"])   # 404 — uses your config name
```

### 6.4 Update **[P/SU]**

```python
r = pv("PUT", f"/v1/configs/{OID}/data/{RID}", headers=JSON, json={"status": "shipped"})
print(r.json()["status"], r.json()["version"])   # shipped, version bumped
```

### 6.5 Soft delete **[P/SU]**

```python
r = pv("DELETE", f"/v1/configs/{OID}/data/{RID}")
pp(r.json())   # {"deleted": true, "order_id": "…"}
print(pv("GET", f"/v1/configs/{OID}/data/{RID}").status_code)   # 404
```

Soft-deleted rows reappear with `include_deleted=true` on export (see §12).

### 6.6 Row validation matrix

```python
def try_write(label: str, payload: dict) -> None:
    r = pv("POST", f"/v1/configs/{OID}/data", headers=JSON, json=payload)
    code = r.json().get("error", {}).get("code", f"status={r.status_code}")
    print(f"{label:30} {code}")

try_write("unknown field",          {"bogus": 1})
try_write("set primary key",        {"order_id": "mine"})
try_write("set version",            {"version": 5})
try_write("set deleted_at",         {"deleted_at": "2026-01-01T00:00:00Z"})
try_write("bad amount type",        {"amount": "lots"})
try_write("bool for float",         {"amount": True})
try_write("bad enum",               {"status": "teleported"})
try_write("null non-nullable",      {"amount": None})
try_write("int for ref",            {"customer_id": 7})
try_write("ref to missing parent",  {"customer_id": "no-such-customer"})
try_write("null on nullable (ok)",  {"note": None, "amount": 40})
try_write("int for float (ok)",     {"amount": 40})
```

All failures are `422`. Type rules: `string/uuid/ref` → string · `int` → integer (not bool) · `float` → number (not bool) · `bool` → true/false · `timestamp` → ISO-8601 string · `enum` → one of `values`.
A `ref` value must point to an **existing row in your own parent config**.

> **Legacy routes are looser.** `POST/PUT /orders` (superuser, built-in entities) don't validate field names, types or references — use `/v1` when you want validation.

### 6.7 Permissions on rows

```python
print(len(pv2("GET", f"/v1/configs/{OID}/data", params={"limit": 1}).json()["items"]))  # Bob can READ
r = pv2("POST", f"/v1/configs/{OID}/data", headers=JSON, json={})
print(r.status_code, r.json().get("error"))   # 403 forbidden
r = su("POST", f"/v1/configs/{OID}/data", headers=JSON, json={})
print(r.json().get("order_id"))               # superuser CAN write
```

---

## 7. The change feed

Every insert / update / soft-delete — from the API, the scheduler, batch, or simulate — writes one entry to an append-only log. The cursor is the per-config monotonic **`version`** (not a timestamp).

### 7.1 Read it

```python
pp(pv("GET", f"/v1/configs/{OID}/changes", params={"since": 0, "limit": 3}).json())
```

`since` ≥ 0, `limit` 1–500 (default 100). Poll with `since=<last next_cursor>`.

### 7.2 Drain it and resume later

```python
cursor = 0
while True:
    page = pv("GET", f"/v1/configs/{OID}/changes", params={"since": cursor, "limit": 100}).json()
    for c in page["changes"]:
        print(c["version"], c["op"], c.get("order_id"))
    cursor = page["next_cursor"]
    if not page["has_more"]:
        break
print(f"resume later with since={cursor}")
```

### 7.3 Prove inserts, updates and deletes are all recorded, in order

```python
cur = pv("GET", f"/v1/configs/{OID}/changes", params={"since": 0, "limit": 1}).json()["latest_version"]
row = pv("POST", f"/v1/configs/{OID}/data", headers=JSON, json={"note": "feed test"}).json()
rid = row["order_id"]
pv("PUT", f"/v1/configs/{OID}/data/{rid}", headers=JSON, json={"status": "shipped"})
pv("DELETE", f"/v1/configs/{OID}/data/{rid}")
changes = pv("GET", f"/v1/configs/{OID}/changes", params={"since": cur}).json()["changes"]
for c in changes:
    print(c["version"], c["op"])
# 3 consecutive versions: insert, update, delete
```

### 7.4 Stale cursor

```python
r = pv("GET", f"/v1/configs/{OID}/changes", params={"since": 99999999})
pp(r.json().get("error"))
# 409 cursor_ahead_of_source — restart from since=0
```

### 7.5 Things to be aware of

- Each entry carries the row's **current** values, not a point-in-time snapshot.
- A row wiped by `replace`/`reset` appears with **only its primary key**.
- The legacy equivalent for the built-in entities is `GET /orders/changes?since=&limit=` **[SU]**.

---

## 8. Batch generation (append / replace / reset)

`POST /v1/configs/{id}/batch` **[P-owner/SU]** bulk-creates rows, or refreshes the dataset.

| Body field   | Meaning                                                                                                             |
| ------------ | ------------------------------------------------------------------------------------------------------------------- |
| `mode`       | `append` (default) · `replace` · `reset`                                                                            |
| `count`      | rows to generate. Omitted → the config's `seed.initial_count`. Range 0 – 1,000,000                                  |
| `batch_size` | rows per insert chunk, 1 – 10,000 (default 1,000)                                                                   |
| `seed`       | integer; same seed + same request = same ids, values and relationships (timestamps and versions are **not** pinned) |
| `confirm`    | must be `true` for `replace` and `reset` (they delete data)                                                         |

### 8.1 Append

```python
r = pv("POST", f"/v1/configs/{OID}/batch", headers=JSON, json={"count": 5, "seed": 123})
pp(r.json())
# entities[0].inserted == 5
```

### 8.2 Same seed ⇒ identical data

```python
# publish two empty configs, batch both with seed=42 → identical names
```

Reusing a seed on a live table that already has matching PKs → `409 duplicate_primary_key`.

### 8.3 Replace (needs confirm; refreshes dependents)

```python
r = pv("POST", f"/v1/configs/{CID}/batch", headers=JSON, json={"mode": "replace"})
print(r.status_code, r.json().get("error"))   # 400 confirmation_required

r = pv("POST", f"/v1/configs/{CID}/batch", headers=JSON, json={
    "mode": "replace", "confirm": True, "count": 3,
})
pp(r.json())
# auto_included includes child config names; total_rows reflect new counts
```

### 8.4 Reset (restarts versions)

```python
r = pv("POST", f"/v1/configs/{OID}/batch", headers=JSON, json={
    "mode": "reset", "confirm": True, "count": 2,
})
feed = pv("GET", f"/v1/configs/{OID}/changes", params={"since": 0}).json()["changes"]
print(len(feed), min(c["version"] for c in feed))   # 2, 1
```

### 8.5 Validation / quotas

```python
print(pv("POST", f"/v1/configs/{OID}/batch", headers=JSON, json={"mode": "bogus"}).status_code)   # 422
print(pv("POST", f"/v1/configs/{OID}/batch", headers=JSON, json={"count": -1}).status_code)       # 422
```

---

## 9. Simulate (inserts + updates + deletes on demand)

```python
r = pv("POST", f"/v1/configs/{OID}/simulate", headers=JSON, json={
    "inserts": 3, "updates": 2, "deletes": 1, "seed": 7,
})
pp(r.json())
# entities[0]: inserted/updated/deleted match the request (clamped to available rows)

# empty body → one default scheduler tick using the config's update_schedule
r = pv("POST", f"/v1/configs/{OID}/simulate", headers=JSON, json={})
pp(r.json())
```

Notes: clamping is explained in `notes[]` with your config names. Configs without soft-delete reject `deletes`. Row-cap quotas apply (`422 quota_exceeded`).

---

## 10. The scheduler

Each published config gets a background job. Cadence comes from `update_schedule.cadence` (hourly/daily/weekly). PATCH that merges and **reschedules live**.

```python
# After publishing, metrics eventually show last_status
pp(pv("GET", f"/v1/configs/{OID}/metrics").json())
pp(pv("GET", f"/v1/configs/{OID}/runs", params={"limit": 10}).json())
```

To force a tick without waiting: use `simulate` with an empty body, or the internal test helper pattern from the suite.

---

## 11. Failure injection

```python
# hard-fail every row read
pv("PATCH", f"/v1/configs/{CID}", headers=JSON, json={"failure_injection": {"fail_rate": 1.0}})
r = pv("GET", f"/v1/configs/{CID}/data", params={"limit": 1})
print(r.status_code, r.json().get("error"))   # 500 injected_failure

# change feed & export are unaffected
print(pv("GET", f"/v1/configs/{CID}/changes", params={"since": 0, "limit": 1}).status_code)  # 200

# recover
pv("PATCH", f"/v1/configs/{CID}", headers=JSON, json={"failure_injection": {"fail_rate": 0.0}})
print(pv("GET", f"/v1/configs/{CID}/data", params={"limit": 1}).status_code)  # 200
```

Probabilistic:

```python
pv("PATCH", f"/v1/configs/{CID}", headers=JSON, json={"failure_injection": {"fail_rate": 0.3}})
from collections import Counter
codes = Counter(
    requests.get(f"{BASE}/v1/configs/{CID}/data", headers=_headers(KEY), params={"limit": 1}).status_code
    for _ in range(100)
)
print(codes)   # ~70 × 200, ~30 × 500
pv("PATCH", f"/v1/configs/{CID}", headers=JSON, json={"failure_injection": {"fail_rate": 0.0}})
```

Built-in `support_tickets` ships with `fail_rate: 0.02` — occasional 500s on legacy `/support_tickets` are expected.

---

## 12. Export (snapshot, delta, zip bundle)

### 12.1 Snapshots

```python
r = pv("GET", f"/v1/configs/{OID}/export")
print(r.headers.get("content-disposition"), r.headers.get("x-snapshot-cursor"))
print(r.text.splitlines()[:3])

r = pv("GET", f"/v1/configs/{OID}/export", params={"format": "ndjson"})
print(r.text.splitlines()[0])

r = pv("GET", f"/v1/configs/{OID}/export", params={"format": "sql", "dialect": "postgresql"})
print(r.text.splitlines()[:5])

r = pv("GET", f"/v1/configs/{OID}/export", params={"include_deleted": "true"})
print("with deleted lines:", len(r.text.splitlines()))
```

### 12.2 Delta files

```python
snap = pv("GET", f"/v1/configs/{OID}/export", params={"format": "ndjson"})
cur = snap.headers["x-snapshot-cursor"]
pv("POST", f"/v1/configs/{OID}/simulate", headers=JSON, json={
    "inserts": 2, "updates": 2, "deletes": 1, "seed": 11,
})
delta = pv("GET", f"/v1/configs/{OID}/export", params={"since": cur, "format": "csv"})
print(delta.headers.get("content-disposition"))
print(delta.headers.get("x-from-cursor"), delta.headers.get("x-to-cursor"))
```

Errors:

```python
print(pv("GET", f"/v1/configs/{OID}/export", params={"since": 99999999}).json()["error"]["code"])  # cursor_ahead_of_source
print(pv("GET", f"/v1/configs/{OID}/export", params={"since": 0, "format": "sql"}).json()["error"]["code"])  # unsupported_format
print(pv("GET", f"/v1/configs/{OID}/export", params={"format": "xml"}).status_code)  # 422
```

### 12.3 Zip bundle

```python
import io
import zipfile

r = pv("GET", "/v1/export", params={
    "configs": f"{CID},{OID},{TID}",
    "format": "sql",
    "include_deleted": "true",
})
r.raise_for_status()
zf = zipfile.ZipFile(io.BytesIO(r.content))
print(sorted(zf.namelist()))
manifest = json.loads(zf.read("manifest.json"))
pp(manifest)

# Load into SQLite with FKs to prove order
import sqlite3
conn = sqlite3.connect(":memory:")
conn.execute("PRAGMA foreign_keys=ON")
conn.executescript(zf.read("schema.sql").decode())
for f in manifest["files"]:
    conn.executescript(zf.read(f["file"]).decode())
```

**Bundle errors**

| Request                                    | Expected                 |
| ------------------------------------------ | ------------------------ |
| no `configs` param                         | `422 validation_error`   |
| empty `configs`                            | `422 invalid_request`    |
| two different owners                       | `422 mixed_owners`       |
| `since=<id>=5` where id isn't in `configs` | `422 since_not_targeted` |
| `format=sql` with any `since`              | `422 unsupported_format` |
| a config you can't read                    | `404 not_found`          |

---

## 13. DDL export

```python
r = pv("GET", f"/v1/configs/{OID}/ddl")
print(r.headers.get("content-type"))
print(r.text)   # CREATE TABLE IF NOT EXISTS acme_customers … then acme_orders

r = pv("GET", f"/v1/configs/{OID}/ddl", params={"include_parents": "false"})
assert "acme_customers" not in r.text

r = pv("GET", f"/v1/configs/{OID}/ddl", params={"dialect": "postgresql"})
print("TIMESTAMP" in r.text)

import re
assert not re.search(r"\bd_[0-9a-f]{10}_", r.text)   # no internal names leak

print(pv("GET", f"/v1/configs/{OID}/ddl", params={"dialect": "mysql"}).status_code)  # 422
```

Legacy (superuser): `su("GET", "/ddl", params={"dialect": "postgresql", "include_system": "true"})`.

---

## 14. Metrics and job runs

```python
pp(pv("GET", f"/v1/configs/{OID}/metrics").json())
# {id, name, row_count, last_run, last_status, max_rows}

pp(pv("GET", f"/v1/configs/{OID}/runs", params={"limit": 10}).json())
pp(su("GET", "/metrics").json())
pp(su("GET", "/scheduler/runs", params={"entity": "orders", "limit": 5}).json())
```

- `row_count` counts **all** rows including soft-deleted.
- `max_rows` is the per-config cap for provider configs, `null` for built-ins.
- Legacy `/scheduler/runs` **requires** `entity`.

---

## 15. Sharing & isolation between providers

### 15.1 Public config: Bob reads, Bob cannot write

```python
shared = pv2("GET", "/v1/configs", params={"scope": "shared"}).json()["items"]
for i in shared:
    if "Alice" in i["owner"]["full_name"]:
        print(i["name"], i["can_write"])   # can_write: false

print(pv2("GET", f"/v1/configs/{OID}/data", params={"limit": 1}).status_code)  # 200

def sweep(key: str, config_id: str) -> None:
    specs = [
        ("GET", f"/v1/configs/{config_id}"),
        ("GET", f"/v1/configs/{config_id}/data"),
        ("GET", f"/v1/configs/{config_id}/data/x"),
        ("GET", f"/v1/configs/{config_id}/changes"),
        ("GET", f"/v1/configs/{config_id}/export"),
        ("GET", f"/v1/configs/{config_id}/ddl"),
        ("GET", f"/v1/configs/{config_id}/metrics"),
        ("GET", f"/v1/configs/{config_id}/runs"),
        ("POST", f"/v1/configs/{config_id}/data"),
        ("PUT", f"/v1/configs/{config_id}/data/x"),
        ("DELETE", f"/v1/configs/{config_id}/data/x"),
        ("PATCH", f"/v1/configs/{config_id}"),
        ("DELETE", f"/v1/configs/{config_id}?confirm=true"),
        ("POST", f"/v1/configs/{config_id}/batch"),
        ("POST", f"/v1/configs/{config_id}/simulate"),
    ]
    for method, path in specs:
        path_only, _, qs = path.partition("?")
        params = dict(p.split("=") for p in qs.split("&") if p) if qs else None
        r = requests.request(
            method, f"{BASE}{path_only}",
            headers={**_headers(key), **JSON},
            json={},
            params=params,
        )
        print(f"{method:7} {path:58} {r.status_code}")

sweep(KEY2, OID)
# GETs → 200 (except missing row 404); writes → 403
```

### 15.2 Private config: invisible to everyone but owner and superuser

```python
pv("PATCH", f"/v1/configs/{OID}", headers=JSON, json={"is_only_me": True})
sweep(KEY2, OID)   # EVERY line -> 404
print(any(i["name"] == "acme_orders" for i in pv2("GET", "/v1/configs", params={"scope": "all"}).json()["items"]))  # False
sweep(SU, OID)     # superuser still sees it
pv("PATCH", f"/v1/configs/{OID}", headers=JSON, json={"is_only_me": False})
```

### 15.3 Tenants never meet

```python
# same name, two providers, separate data — allowed
pv("POST", "/v1/configs", headers=JSON, json={
    "entity": "shared_name",
    "fields": {"id": {"type": "uuid", "primary_key": True}},
    "seed": {"initial_count": 3},
})
pv2("POST", "/v1/configs", headers=JSON, json={
    "entity": "shared_name",
    "fields": {"id": {"type": "uuid", "primary_key": True}},
    "seed": {"initial_count": 9},
})

# a config can't reference another provider's config
r = pv2("POST", "/v1/configs/validate", headers=YAML, data=ACME_ORDERS_YAML)
print(r.json()["error"]["message"])

# a ROW can't point at another provider's parent
pv2("POST", "/v1/configs", headers=JSON, json={
    "entity": "b_parent",
    "fields": {"id": {"type": "uuid", "primary_key": True}},
    "seed": {"initial_count": 2},
})
bc = pv2("POST", "/v1/configs", headers=JSON, json={
    "entity": "b_child",
    "fields": {
        "id": {"type": "uuid", "primary_key": True},
        "parent": {"type": "ref", "ref_entity": "b_parent"},
    },
    "seed": {"initial_count": 2},
}).json()["id"]
alice_cust = pv("GET", f"/v1/configs/{CID}/data", params={"limit": 1}).json()["items"][0]["customer_id"]
r = pv2("POST", f"/v1/configs/{bc}/data", headers=JSON, json={"parent": alice_cust})
print(r.status_code, r.json().get("error", {}).get("code"))   # invalid_reference
```

Also check that error messages, DDL, exports never show `d_<hex>_` names:

```python
import re
NAMESPACED = re.compile(r"\bd_[0-9a-f]{10}_")
text = pv("GET", f"/v1/configs/{OID}/ddl").text + pv("GET", f"/v1/configs/{OID}/export", params={"format": "sql"}).text
assert not NAMESPACED.search(text)
```

### 15.4 Built-in examples as seen by a provider

```python
for i in pv("GET", "/v1/configs", params={"scope": "shared"}).json()["items"]:
    if i.get("source") == "yaml":
        print(i["id"], i["name"], i["owner"]["id"], i["can_write"])

print(pv("GET", "/v1/configs/cfg_sys_orders/data", params={"limit": 3}).status_code)  # 200
print(pv("POST", "/v1/configs/cfg_sys_orders/data", headers=JSON, json={}).status_code)  # 403
print(su("POST", "/v1/configs/cfg_sys_orders/data", headers=JSON, json={}).status_code)  # 201
```

### 15.5 Superuser powers over provider data

```python
print(len(su("GET", "/v1/configs", params={"scope": "all"}).json()["items"]))
su("POST", f"/v1/configs/{OID}/simulate", headers=JSON, json={"inserts": 1})
su("PATCH", f"/v1/configs/{OID}", headers=JSON, json={"update_schedule": {"cadence": "weekly"}})
pv("PATCH", f"/v1/configs/{OID}", headers=JSON, json={"update_schedule": {"cadence": "hourly"}})  # restore
```

### 15.6 Deactivated provider

```python
su("PATCH", f"/v1/admin/providers/{B_ID}", headers=JSON, json={"is_active": False})
st("GET", "/v1/me", key=KEY2)   # 401
# Bob's PUBLIC configs are still listed/readable and keep updating
su("PATCH", f"/v1/admin/providers/{B_ID}", headers=JSON, json={"is_active": True})
```

---

## 16. Superuser-only legacy routes (built-in entities)

These serve the three **built-in YAML entities** and global admin tools. They need a **superuser** key; a provider key gets **`403`** on every one.

### 16.1 CRUD on a built-in entity

```python
pp(su("GET", "/orders", params={"limit": 3}).json())
r = su("POST", "/orders", headers=JSON, json={"amount": 42.5})
OID_L = r.json()["order_id"]
pp(su("GET", f"/orders/{OID_L}").json())
pp(su("PUT", f"/orders/{OID_L}", headers=JSON, json={"amount": 250, "status": "shipped"}).json())
pp(su("DELETE", f"/orders/{OID_L}").json())
st("GET", f"/orders/{OID_L}", key=SU)   # 404
pp(su("GET", "/orders/changes", params={"since": 0, "limit": 3}).json())
print(list(su("GET", "/entities").json().keys()))
```

### 16.2 Admin batch

```python
pp(su("POST", "/admin/batch", headers=JSON, json={
    "entities": ["orders"], "count": 100, "batch_size": 50,
}).json())

pp(su("POST", "/admin/batch", headers=JSON, json={
    "counts": {"customers": 10, "orders": 20},
}).json())

r = su("POST", "/admin/batch", headers=JSON, json={"mode": "replace", "seed": 42})
print(r.status_code, r.json().get("error"))   # 400 confirmation_required

pp(su("POST", "/admin/batch", headers=JSON, json={
    "mode": "replace", "seed": 42, "confirm": True,
}).json())
```

### 16.3 Admin changes

```python
pp(su("POST", "/admin/changes", headers=JSON, json={
    "entities": ["orders"], "inserts": 10, "updates": 5, "deletes": 2, "seed": 1,
}).json())
pp(su("POST", "/admin/changes", headers=JSON, json={}).json())  # one default tick for every entity
```

### 16.4 Legacy export & DDL

```python
print(su("GET", "/orders/export", params={"format": "csv"}).text.splitlines()[:3])
print(su("GET", "/orders/export", params={"format": "sql", "dialect": "postgresql"}).text.splitlines()[:3])
r = su("GET", "/export", params={"format": "sql"})
Path("export.zip").write_bytes(r.content)
print(su("GET", "/ddl", params={"dialect": "postgresql", "include_system": "true"}).text[:500])
```

### 16.5 Provider keys are locked out of all of it

```python
for p in ["/entities", "/metrics", "/scheduler/runs?entity=orders", "/orders?limit=1", "/customers", "/ddl", "/export"]:
    path, _, qs = p.partition("?")
    params = dict(x.split("=") for x in qs.split("&") if x) if qs else None
    code = requests.get(f"{BASE}{path}", headers=_headers(KEY), params=params).status_code
    print(f"{p:34} {code}")   # every line: 403

for method, path in [("POST", "/admin/batch"), ("POST", "/admin/changes")]:
    code = requests.request(method, f"{BASE}{path}", headers={**_headers(KEY), **JSON}, json={}).status_code
    print(method, path, code)  # 403

st("GET", "/health")  # 200
```

---

## 17. CLI (no server needed)

The CLI talks **directly to the database** named by `DATABASE_URL`. It works on the **built-in YAML entities**; provider-published configs are managed only through `/v1`. It bypasses HTTP auth entirely.

```python
import subprocess

def cli(*args: str) -> str:
    return subprocess.check_output(["python", "-m", "app.cli", *args], text=True)

print(cli("genkey"))
print(cli("create-provider", "--full-name", "Acme Data Team"))

cli("ddl", "--dialect", "postgresql", "--out", "schema.sql")
cli("batch", "--counts", "customers=100", "orders=500", "--seed", "1")
cli("batch", "--count", "10", "--entities", "customers", "--batch-size", "500")
cli("batch", "--mode", "replace", "--yes", "--seed", "1")
cli("batch", "--mode", "reset", "--yes")
cli("changes", "--entities", "orders", "--inserts", "10", "--updates", "5", "--deletes", "2", "--seed", "3")
cli("changes")
cli("export", "--out-dir", "./dump", "--format", "sql")
cli("export", "--out-dir", "./dump2", "--format", "csv", "--entities", "orders", "customers", "--include-deleted")
cli("export", "--out-dir", "./dump3", "--format", "ndjson", "--since", "orders=1")
```

Checks:

- `replace`/`reset` **without** `--yes` → prints `error [confirmation_required]: …` and exits `1`.
- Unknown entity → exit `1`; malformed `--counts` → exit `2`.
- After `create-provider`, the printed key works against the running server (needs a superuser key set on the server).

---

## 18. Error reference

Every failure uses one envelope: `{"error":{"code":"…","message":"…"}}` (a validation error's `message` is a list).

| Code                                                                                               |      HTTP       | When                                                                                                                                                 |
| -------------------------------------------------------------------------------------------------- | :-------------: | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `unauthorized`                                                                                     |       401       | missing / wrong / revoked key, inactive provider (adds `WWW-Authenticate: ApiKey`)                                                                   |
| `forbidden`                                                                                        |       403       | provider key on a superuser/legacy route · write to someone else's config · creating a config for another provider                                   |
| `not_found`                                                                                        |       404       | unknown **or private** config · row missing or soft-deleted · unknown provider/key · unknown entity on `/scheduler/runs`                             |
| `validation_error`                                                                                 |       422       | bad query/body shape (e.g. `limit=0`, `mode=bogus`, `format=xml`, missing `entity`)                                                                  |
| `invalid_config`                                                                                   |       422       | a config (or body) failed validation — the message says exactly why                                                                                  |
| `invalid_request`                                                                                  |       422       | bad `PATCH` body · empty `configs` in a bundle                                                                                                       |
| `invalid_provider` / `invalid_label`                                                               |       422       | bad `full_name` / key label                                                                                                                          |
| `payload_too_large`                                                                                |       413       | config body over `MAX_CONFIG_BYTES`                                                                                                                  |
| `already_exists`                                                                                   |       409       | config name already used by this owner                                                                                                               |
| `quota_exceeded`                                                                                   |    409 / 422    | too many configs (**409**) · too many rows (**422**)                                                                                                 |
| `managed_by_yaml`                                                                                  |       409       | edit/delete of a built-in config                                                                                                                     |
| `has_dependents`                                                                                   |       409       | change fields of / delete a config others reference                                                                                                  |
| `confirmation_required`                                                                            |       400       | delete config · change fields · `replace`/`reset` without confirm                                                                                    |
| `missing_parent_rows`                                                                              |       409       | a child has no parent rows to point at                                                                                                               |
| `busy`                                                                                             |       409       | another bulk write held the lock > 30 s                                                                                                              |
| `not_supported`                                                                                    | 400 / 409 / 422 | row delete on a config with no soft-delete field (400) · modifying the system provider (409) · `deletes` in simulate with no soft-delete field (422) |
| `unknown_field` · `read_only_field` · `invalid_value` · `invalid_reference`                        |       422       | row write validation (`/v1`)                                                                                                                         |
| `cursor_ahead_of_source`                                                                           |       409       | `since` is beyond the latest version (usually after `reset`)                                                                                         |
| `duplicate_primary_key`                                                                            |       409       | a seeded insert generated a key that already exists — use a different seed                                                                           |
| `unsupported_format` · `invalid_since` · `since_not_targeted` · `mixed_owners`                     |       422       | export request problems                                                                                                                              |
| `unknown_entity` · `counts_not_targeted` · `invalid_mode` · `invalid_batch_size` · `invalid_count` |       422       | legacy `/admin/*` & CLI request problems                                                                                                             |
| `injected_failure`                                                                                 |       500       | failure injection fired                                                                                                                              |

---

## 19. Restart, Postgres, Docker, pytest

### 19.1 Startup failure modes

| Start with                                                                 | Expect                                                 |
| -------------------------------------------------------------------------- | ------------------------------------------------------ |
| `REQUIRE_API_KEY=true` and no key                                          | exits, message mentions `REQUIRE_API_KEY` and `genkey` |
| `MAX_ROWS_PER_CONFIG=lots`                                                 | exits: `MAX_ROWS_PER_CONFIG must be an integer`        |
| no key at all                                                              | starts, logs "authentication is DISABLED"              |
| a YAML in `configs/` with an unknown type, dangling `ref` or circular refs | exits with a clear message naming the file / entity    |

### 19.2 Everything survives a restart

1. Create a provider, publish a **private** config (`is_only_me: true`), note the key and config id.
2. Stop the server, start it again (same `DATABASE_URL`, same superuser key).
3. Verify: `st("GET", "/v1/me", key=KEY)` → `200` · rows intact · superuser still reads the private config · unknown key still → `401` · writes still work.
4. Providers, hashed keys, configs and data persist. The config's scheduler job is re-registered.

### 19.3 PostgreSQL

```python
# Terminal:
#   export DATABASE_URL=postgresql+psycopg2://synthetic:synthetic@localhost:5432/synthetic
#   uvicorn app.main:app
# Repeat the smoke test (§20).
```

### 19.4 Docker (API + Postgres)

```python
# Terminal:
#   echo "API_KEY=$(python -m app.cli genkey)" >> .env
#   docker compose up --build
#   requests.get("http://localhost:8000/health")
```

### 19.5 Automated tests (310 tests)

```python
# Terminal:
#   pytest -v
#   pytest -v tests/test_v1_access.py
#   pytest -v -k "scheduler"
#   TEST_DATABASE_URL=postgresql+psycopg2://… pytest -v
```

> ⚠ `TEST_DATABASE_URL` must point at an **empty** database — its `public` schema is **wiped**.

---

## 20. One-shot smoke-test script

Save as `smoke_test.py`, then run: `SU=<superuser key> python smoke_test.py`. Requires `requests` and a server **with a superuser key set**. It creates two providers (providers can't be deleted — the script deactivates them at the end) and cleans up its own configs.

```python
#!/usr/bin/env python3
"""One-shot smoke test for the Synthetic Data Simulation Backend."""
from __future__ import annotations

import os
import sys

import requests

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")
SU = os.environ.get("SU")
if not SU:
    sys.exit("export SU=<superuser key> first")

FAIL = 0
JSON = {"Content-Type": "application/json"}
YAML = {"Content-Type": "text/yaml"}
SUH = {"X-API-Key": SU}


def status(method: str, url: str, **kwargs) -> int:
    return requests.request(method, url, **kwargs).status_code


def check(what: str, want: int, method: str, path: str, **kwargs) -> None:
    global FAIL
    got = status(method, f"{BASE}{path}", **kwargs)
    if got == want:
        print(f"PASS  {what} ({got})")
    else:
        print(f"FAIL  {what} (wanted {want}, got {got})")
        FAIL = 1


print("== auth ==")
check("health is public", 200, "GET", "/health")
check("no key -> 401", 401, "GET", "/v1/me")
check("wrong key -> 401", 401, "GET", "/v1/me", headers={"X-API-Key": "nope"})
check("key in query string -> 401", 401, "GET", f"/v1/me?api_key={SU}")
check("superuser /v1/me", 200, "GET", "/v1/me", headers=SUH)

print("== providers ==")
A = requests.post(f"{BASE}/v1/admin/providers", headers={**SUH, **JSON}, json={"full_name": "Smoke Alice"}).json()
B = requests.post(f"{BASE}/v1/admin/providers", headers={**SUH, **JSON}, json={"full_name": "Smoke Bob"}).json()
A_ID, A_KEY, A_KID = A["id"], A["api_key"], A["key"]["id"]
B_ID, B_KEY = B["id"], B["api_key"]
AH, BH = {"X-API-Key": A_KEY}, {"X-API-Key": B_KEY}

check("provider A /v1/me", 200, "GET", "/v1/me", headers=AH)
check("provider cannot list providers", 403, "GET", "/v1/admin/providers", headers=AH)
check("provider cannot use legacy /entities", 403, "GET", "/entities", headers=AH)
check("provider cannot use /admin/batch", 403, "POST", "/admin/batch", headers={**AH, **JSON}, json={})

print("== configs ==")
CUST = requests.post(
    f"{BASE}/v1/configs",
    headers={**AH, **YAML},
    data="""\
entity: smoke_customers
fields:
  customer_id: { type: uuid, primary_key: true }
  name:        { type: string, key_label: full_name }
  tier:        { type: enum, values: [free, pro, enterprise] }
  deleted_at:  { type: timestamp, nullable: true, auto: soft_delete }
seed: { initial_count: 10 }
""",
).json()
C_ID = CUST["id"]
ORD = requests.post(
    f"{BASE}/v1/configs",
    headers={**AH, **YAML},
    data="""\
entity: smoke_orders
fields:
  order_id:    { type: uuid, primary_key: true }
  customer_id: { type: ref, ref_entity: smoke_customers }
  amount:      { type: float, min: 10, max: 500 }
  status:      { type: enum, values: [pending, shipped, cancelled] }
  updated_at:  { type: timestamp, auto: updated }
  deleted_at:  { type: timestamp, nullable: true, auto: soft_delete }
seed: { initial_count: 20 }
""",
).json()
O_ID = ORD["id"]
if CUST.get("seeded_rows") == 10 and ORD.get("seeded_rows") == 20:
    print("PASS  both configs seeded")
else:
    print("FAIL  seeding")
    FAIL = 1

check("duplicate name -> 409", 409, "POST", "/v1/configs", headers={**AH, **JSON},
      json={"entity": "smoke_customers", "fields": {"id": {"type": "uuid", "primary_key": True}}})
check("bad config -> 422", 422, "POST", "/v1/configs", headers={**AH, **JSON},
      json={"entity": "Bad Name", "fields": {"id": {"type": "uuid", "primary_key": True}}})
check("validate dry run", 200, "POST", "/v1/configs/validate", headers={**AH, **JSON},
      json={"entity": "dry_run", "fields": {"id": {"type": "uuid", "primary_key": True}}})
check("parent delete blocked", 409, "DELETE", f"/v1/configs/{C_ID}?confirm=true", headers=AH)
check("delete w/o confirm -> 400", 400, "DELETE", f"/v1/configs/{O_ID}", headers=AH)

print("== CRUD ==")
ROW = requests.post(f"{BASE}/v1/configs/{O_ID}/data", headers={**AH, **JSON},
                    json={"amount": 99.5, "status": "pending"}).json()
R_ID = ROW["order_id"]
check("get row", 200, "GET", f"/v1/configs/{O_ID}/data/{R_ID}", headers=AH)
check("update row", 200, "PUT", f"/v1/configs/{O_ID}/data/{R_ID}", headers={**AH, **JSON},
      json={"status": "shipped"})
check("unknown field -> 422", 422, "POST", f"/v1/configs/{O_ID}/data", headers={**AH, **JSON}, json={"bogus": 1})
check("set pk -> 422", 422, "POST", f"/v1/configs/{O_ID}/data", headers={**AH, **JSON}, json={"order_id": "x"})
check("bad ref -> 422", 422, "POST", f"/v1/configs/{O_ID}/data", headers={**AH, **JSON},
      json={"customer_id": "nope"})
check("soft delete row", 200, "DELETE", f"/v1/configs/{O_ID}/data/{R_ID}", headers=AH)
check("deleted row -> 404", 404, "GET", f"/v1/configs/{O_ID}/data/{R_ID}", headers=AH)
check("limit=501 -> 422", 422, "GET", f"/v1/configs/{O_ID}/data?limit=501", headers=AH)

print("== change feed / simulate / batch ==")
CUR = requests.get(f"{BASE}/v1/configs/{O_ID}/changes", headers=AH, params={"since": 0, "limit": 1}).json()["latest_version"]
SIM = requests.post(f"{BASE}/v1/configs/{O_ID}/simulate", headers={**AH, **JSON},
                    json={"inserts": 3, "updates": 2, "deletes": 1, "seed": 7}).json()
ops = requests.get(f"{BASE}/v1/configs/{O_ID}/changes", headers=AH,
                   params={"since": CUR, "limit": 100}).json()["changes"]
from collections import Counter
counts = Counter(c["op"] for c in ops)
if counts.get("insert") == 3 and counts.get("update") == 2 and counts.get("delete") == 1:
    print("PASS  simulate recorded 3/2/1 in the feed")
else:
    print(f"FAIL  feed ops = {dict(counts)}")
    FAIL = 1
check("stale cursor -> 409", 409, "GET", f"/v1/configs/{O_ID}/changes?since=99999999", headers=AH)
check("batch append", 200, "POST", f"/v1/configs/{O_ID}/batch", headers={**AH, **JSON},
      json={"count": 5, "seed": 123})
check("replace w/o confirm -> 400", 400, "POST", f"/v1/configs/{O_ID}/batch", headers={**AH, **JSON},
      json={"mode": "replace"})
check("metrics", 200, "GET", f"/v1/configs/{O_ID}/metrics", headers=AH)
check("runs", 200, "GET", f"/v1/configs/{O_ID}/runs", headers=AH)

print("== export / ddl ==")
check("export csv", 200, "GET", f"/v1/configs/{O_ID}/export", headers=AH)
check("export ndjson", 200, "GET", f"/v1/configs/{O_ID}/export?format=ndjson", headers=AH)
check("export sql (postgres)", 200, "GET", f"/v1/configs/{O_ID}/export?format=sql&dialect=postgresql", headers=AH)
check("delta export", 200, "GET", f"/v1/configs/{O_ID}/export?since={CUR}", headers=AH)
check("delta as sql -> 422", 422, "GET", f"/v1/configs/{O_ID}/export?since=0&format=sql", headers=AH)
check("ddl", 200, "GET", f"/v1/configs/{O_ID}/ddl", headers=AH)
check("bundle zip", 200, "GET", f"/v1/export?configs={C_ID},{O_ID}&format=csv", headers=AH)
check("bundle mixed owners -> 422", 422, "GET", f"/v1/export?configs={C_ID},cfg_sys_orders", headers=AH)

print("== sharing & isolation ==")
check("Bob reads Alice (public)", 200, "GET", f"/v1/configs/{O_ID}/data?limit=1", headers=BH)
check("Bob writes Alice -> 403", 403, "POST", f"/v1/configs/{O_ID}/data", headers={**BH, **JSON}, json={})
check("Bob batch Alice -> 403", 403, "POST", f"/v1/configs/{O_ID}/batch", headers={**BH, **JSON}, json={})
check("Alice goes private", 200, "PATCH", f"/v1/configs/{O_ID}", headers={**AH, **JSON},
      json={"is_only_me": True})
check("Bob reads private -> 404", 404, "GET", f"/v1/configs/{O_ID}/data?limit=1", headers=BH)
check("Bob writes private -> 404", 404, "POST", f"/v1/configs/{O_ID}/data", headers={**BH, **JSON}, json={})
check("superuser reads private", 200, "GET", f"/v1/configs/{O_ID}/data?limit=1", headers=SUH)
check("superuser writes private", 201, "POST", f"/v1/configs/{O_ID}/data", headers={**SUH, **JSON}, json={})
check("Alice goes public again", 200, "PATCH", f"/v1/configs/{O_ID}", headers={**AH, **JSON},
      json={"is_only_me": False})

print("== failure injection ==")
check("enable fail_rate=1", 200, "PATCH", f"/v1/configs/{O_ID}", headers={**AH, **JSON},
      json={"failure_injection": {"fail_rate": 1.0}})
check("row read now fails", 500, "GET", f"/v1/configs/{O_ID}/data?limit=1", headers=AH)
check("change feed unaffected", 200, "GET", f"/v1/configs/{O_ID}/changes?since=0&limit=1", headers=AH)
check("disable fail_rate", 200, "PATCH", f"/v1/configs/{O_ID}", headers={**AH, **JSON},
      json={"failure_injection": {"fail_rate": 0.0}})
check("row read recovers", 200, "GET", f"/v1/configs/{O_ID}/data?limit=1", headers=AH)

print("== key rotation & cleanup ==")
check("delete child config", 200, "DELETE", f"/v1/configs/{O_ID}?confirm=true", headers=AH)
check("delete parent config", 200, "DELETE", f"/v1/configs/{C_ID}?confirm=true", headers=AH)
NEW = requests.post(f"{BASE}/v1/admin/providers/{A_ID}/keys", headers={**SUH, **JSON},
                    json={"label": "rotated"}).json()["api_key"]
check("revoke old key", 200, "DELETE", f"/v1/admin/providers/{A_ID}/keys/{A_KID}", headers=SUH)
check("old key now 401", 401, "GET", "/v1/me", headers=AH)
check("new key works", 200, "GET", "/v1/me", headers={"X-API-Key": NEW})
check("deactivate Alice", 200, "PATCH", f"/v1/admin/providers/{A_ID}", headers={**SUH, **JSON},
      json={"is_active": False})
check("deactivate Bob", 200, "PATCH", f"/v1/admin/providers/{B_ID}", headers={**SUH, **JSON},
      json={"is_active": False})
check("inactive provider -> 401", 401, "GET", "/v1/me", headers={"X-API-Key": NEW})

print()
if FAIL == 0:
    print("ALL CHECKS PASSED")
else:
    print("SOME CHECKS FAILED")
sys.exit(FAIL)
```

---

## 21. Master checklist

Tick each box as you verify it. **Section** points back to the details above.

### Setup & auth

- [ ] Server starts, seeds 30 / 50 / 20 rows, `/health` is open (§1)
- [ ] Missing / wrong / empty key → 401 envelope + `WWW-Authenticate` (§3.2)
- [ ] Key in query string and `Authorization: Bearer` are rejected (§3.3)
- [ ] Swagger **Authorize** works (§3.4)
- [ ] Auth OFF mode behaves as anonymous superuser + warning (§3.5)
- [ ] `REQUIRE_API_KEY=true` blocks startup without a key (§3.6)
- [ ] Superuser key rotation with `API_KEYS=new,old` (§3.7)
- [ ] `ENABLE_DOCS=false` hides docs, `/health` stays open (§3.8)

### Providers & keys

- [ ] Create provider via API; key shown once; name validation (§4.1)
- [ ] Create provider via CLI; key works on the running server (§4.2)
- [ ] `/v1/me` for superuser and provider (§4.3)
- [ ] List providers shows no secrets; `last_used_at` fills in (§4.4, 4.5)
- [ ] Rename / deactivate / reactivate (§4.6)
- [ ] Issue second key, revoke first, rotation (§4.7)
- [ ] Provider key gets 403 on every `/v1/admin/*` route (§4.8)
- [ ] Only a SHA-256 of keys is stored (§4.9)

### Configs

- [ ] Validate dry-run saves nothing (§5.4)
- [ ] Publish via **YAML** body, via **JSON** body, and as superuser for a provider (§5.5, 5.12)
- [ ] List scopes `mine` / `shared` / `all`; `can_write` flag (§5.6)
- [ ] PATCH: merge schedule/failure/seed; toggle `is_only_me`; replace `fields` with confirm (§5.7)
- [ ] PATCH negatives; built-in configs → `managed_by_yaml` (§5.7)
- [ ] Delete needs `confirm=true`; frees name and quota slot (§5.8)
- [ ] Parent/child rules: missing parent rows, `has_dependents`, cycles (§5.9)
- [ ] Whole bad-config gauntlet returns clear 422s and creates nothing (§5.10)
- [ ] Body problems: bad JSON, non-object, anchors, > 64 KB → 413 (§5.10)
- [ ] Quotas enforced (§5.11)
- [ ] Built-in YAML: add `configs/<name>.yaml`, restart, it appears as `cfg_sys_<name>` (§5.3-A)

### Rows (CRUD)

- [ ] Create: empty / partial / pinned ref (§6.1)
- [ ] List + keyset pagination covers every row once; `limit` bounds (§6.2)
- [ ] Get one; 404 envelope uses your config name (§6.3)
- [ ] Update bumps `version` and `updated_at` (§6.4)
- [ ] Soft delete hides the row but `include_deleted` shows it (§6.5)
- [ ] Row validation matrix (unknown / read-only / type / enum / null / ref) (§6.6)
- [ ] Non-owner read OK, write 403; superuser writes anywhere (§6.7)

### Feed, generation, scheduler

- [ ] Change feed: insert → update → delete in order; paging; resume cursor (§7)
- [ ] Stale cursor → 409 (§7.4, 8.4)
- [ ] Batch append, chunks, default count (§8.1)
- [ ] Same seed ⇒ identical data; reused seed on live table ⇒ 409 (§8.2)
- [ ] `replace` needs confirm, brings dependents, consumers keep working (§8.3)
- [ ] `reset` restarts versions; old cursor → 409 (§8.4)
- [ ] Simulate exact counts, default tick, clamping notes, errors (§9)
- [ ] Scheduler tick via snippet writes a run row; schedule edit is live (§10)
- [ ] `metrics` / `runs` show `last_status` (§10.3, §14)

### Failure injection, export, DDL

- [ ] `fail_rate=1` → 500 `injected_failure`; `/changes` & `/export` unaffected; recover (§11.1)
- [ ] Probabilistic failures; built-in `support_tickets` 2 % (§11.2, 11.3)
- [ ] Snapshot CSV / NDJSON / SQL (sqlite + postgresql); headers; file names (§12.1)
- [ ] Delta export + `X-From/To-Cursor`; delta errors (§12.2)
- [ ] Zip bundle loads into SQLite with FKs on; mixed snapshot + delta bundle (§12.3)
- [ ] Bundle error cases (§12.3)
- [ ] DDL: parents first, `include_parents=false`, postgres dialect, no internal names (§13)

### Isolation & superuser

- [ ] Bob: public config → reads 200 / writes 403 on every route (§15.1)
- [ ] Private config → 404 everywhere for Bob; owner + superuser unaffected (§15.2)
- [ ] Same config name for two providers; no cross-provider refs (§15.3)
- [ ] Built-ins readable by providers, writable only by superuser (§15.4)
- [ ] Superuser can read/write/patch anything (§15.5)
- [ ] Deactivated provider: keys dead, public configs still served (§15.6)
- [ ] Every legacy route → 403 for provider keys (§16.5)
- [ ] Legacy CRUD, `/admin/batch`, `/admin/changes`, `/export`, `/ddl` (§16)

### CLI, ops

- [ ] All CLI commands incl. `--yes` guard and exit codes (§17)
- [ ] Restart persistence (§19.2); PostgreSQL run (§19.3); Docker run (§19.4)
- [ ] `pytest -v` → 310 passing (§19.5)
- [ ] `smoke_test.py` → ALL CHECKS PASSED (§20)

---

## 22. Gotchas worth knowing

**From the README (deliberate limitations)**

- **Change-feed and delta rows carry the row's _current_ values**, not a point-in-time snapshot.
- **Run a single instance.** The catalog, scheduler and write lock live in one process's memory; a second worker wouldn't see configs created on the first until restart.
- **`reset` staleness detection is a heuristic** — always re-baseline from `since=0` after a reset.
- **Seeds pin ids, values and relationships — not timestamps or version numbers.**
- **Changing a config's `fields` deletes its data** (drop + re-seed), behind `confirm=true`.
- **Readers share the owner's `failure_injection`.**
- **The change log is never trimmed** except by `reset` or deleting the config.
- **No rate limiting on failed auth attempts; one superuser role with no audit trail.** Put the API behind HTTPS — a header key is readable on plain HTTP.
- **No migrations** (`create_all` only) and **no cron-expression cadences**; `drift` injection is not implemented.
- **`version` = `max(version)+1`** — safe with the serialised writers here, not with concurrent ones.

**From reading the code (not called out in the README)**

- The app **does not load `.env`** itself — `export` variables or use `uvicorn --env-file .env`.
- An interval job's **first run is one full interval after (re)start**; `simulate` and the batch routes are the way to get changes immediately, and `simulate` does not log to `/runs`.
- Child rows chosen by `POST`, `batch` and the scheduler can reference a parent that has since been **soft-deleted** (parents are picked from all rows); `simulate` picks live parents only. Add `include_deleted=true` when loading snapshots with FKs enforced.
- `DELETE …/data/{id}` on an **already soft-deleted** row still returns `200` and logs another `delete` event; there is **no undelete** route (`PUT` on a deleted row → 404).
- `next_after` / `has_more` are `true` whenever a page is exactly `limit` long, so the **last page can be followed by one empty page**.
- **PATCH `fields` replaces the whole `fields` map** (it is not a merge), and PATCHing `seed.initial_count` alone adds no rows.
- **Failure injection only affects the row routes** (`/data…`, legacy entity CRUD) — not `/changes`, `/export`, metrics, batch or simulate.
- `GET /v1/configs/{id}` returns the **full definition** (including `failure_injection`) to any provider who can read the config.
- Unknown keys in a built-in `configs/*.yaml` file are not rejected the way they are for API-published configs.
- The **`MAX_ROWS_PER_CONFIG` row cap applies to provider configs only** — built-in YAML configs have none (`max_rows: null` in their metrics).
