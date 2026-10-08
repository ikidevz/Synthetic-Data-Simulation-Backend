# Synthetic Data Simulation Backend

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-355%20passing-brightgreen.svg)](#run-the-tests)
[![License](https://img.shields.io/badge/license-see%20repo-lightgrey.svg)](#)

A FastAPI backend that serves **purely synthetic, config-driven data** and **mutates it on its own schedule** (hourly / daily / weekly) — a self-contained “living source system” for building and stress-testing data ingestion pipelines.

![Cover](./assets/cover.png)

---

## Features

- **Config-driven** — an entity is a YAML file, or a `POST` to a project. Adding one needs zero Python changes.
- **Projects = schemas** — `provider → project → config → row`. A **project** is a named group of configs, like a schema is a group of tables; a **config** is one table. `ref` fields resolve only inside the project, `replace`/`reset`/batch refresh only that project's tables, and the **whole project** exports as one schema (`GET /v1/projects/{id}/ddl` and `/export`; on PostgreSQL it starts with `CREATE SCHEMA` + `SET search_path`). Visibility (`is_only_me`) belongs to the project, so a shared table can never point at a hidden parent.
- **Multi-provider** — providers create projects and publish configs through the API, authenticated by their own API key. A project (and the configs in it) is readable by other providers unless the owner marks it `is_only_me`; only the owner can write. A **superuser** key can do everything.
- **Living data** — a background scheduler inserts, updates, and soft-deletes rows on each entity’s cadence.
- **Ingestion-ready** — every entity exposes a `/changes?since=<version>` feed (insert / update / delete), the same contract a real incremental source provides.
- **Batch generation + one-call refresh** — bulk-generate rows in chunks, or wipe and regenerate the whole dataset — reproducibly, with a seed.
- **Change batches** — apply inserts, updates and soft-deletes on demand; all recorded in the change feed.
- **Data export** — snapshots and delta files as CSV / NDJSON / SQL, or a zip bundle (schema + data + manifest) you can load into another database.
- **Synthetic generation via [iki-data-generator](https://pypi.org/project/iki-data-generator/)** — any field can opt into a real Iki provider (names, emails, job titles, …) instead of the built-in generic generator, and still stays seed-reproducible.
- **DDL export** — `CREATE TABLE` / `CREATE INDEX` scripts generated from the same configs the API runs on.
- **Testable failure modes** — per-entity `fail_rate` and `latency_ms` injection, with a consistent error envelope.
- **No AI, no third-party services** — FastAPI + SQLAlchemy + APScheduler. SQLite by default, Postgres via one env var.

---

## Table of contents

- [Quickstart](#quickstart)
- [CLI](#cli-no-server-needed)
- [Run the tests](#run-the-tests)
- [Docker](#docker-api--postgres)
- [Configuring entities](#configuring-entities)
- [API (built-in entities)](#api-built-in-entities)
- [Authentication](#authentication)
- [Providers, projects & publishing configs (`/v1`)](#providers-projects--publishing-configs-v1)
- [Batch generation, DDL & refresh](#batch-generation-ddl-export--refresh)
- [Project structure](#project-structure)
- [Design notes](#design-notes)
- [Known limitations](#known-limitations)
- [Deploying](#deploying)
- [Tech stack](#tech-stack)

---

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Generate a superuser API key (recommended before any real use)
python -m app.cli genkey
export API_KEY=<the key>

uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) for interactive API docs.

On first start the app creates `synthetic.db`, creates the tables, and seeds each entity in dependency order (customers → orders → support_tickets).

> **`.env` note:** the app does not load `.env` itself. Export variables, or start with `uvicorn app.main:app --env-file .env`. The sample key in `.env.example` is public — generate your own with `genkey`.

---

## CLI (no server needed)

```bash
python -m app.cli genkey
python -m app.cli create-provider --full-name "Acme Data Team"
python -m app.cli ddl --dialect postgresql --out schema.sql
python -m app.cli batch --counts customers=100 orders=50000 --seed 1
python -m app.cli batch --mode replace --yes --seed 1        # refresh everything
python -m app.cli changes --entities orders --inserts 100 --updates 500 --deletes 20
python -m app.cli export --out-dir ./dump --format sql        # schema + data + manifest
```

The CLI works on the **built-in YAML entities**. Provider-published configs are managed through the `/v1` API.

---

## Run the tests

```bash
pytest -v
```

355 tests. Default target is SQLite. Point `TEST_DATABASE_URL` at an **empty** PostgreSQL database to run the same suite there (its `public` schema is wiped first):

```bash
TEST_DATABASE_URL=postgresql+psycopg2://synthetic:synthetic@localhost:5433/synthetic_test pytest -v
```

**The tests never read `configs/*.yaml`** — those files are examples for people, not fixtures. The built-in entities the legacy-route tests need come from `tests/entity_fixtures.py` (written to a temp directory that `CONFIG_DIR` points at before the app starts), and every other test builds its configs inline. You can edit or delete the examples without touching a test.

---

## Docker (API + Postgres)

```bash
echo "API_KEY=$(python -m app.cli genkey)" >> .env
docker compose up --build
```

`REQUIRE_API_KEY` is set in the compose file, so the API will not start without `API_KEY`.

> **Note:** `Dockerfile` and `docker-compose.yml` follow standard patterns but were **not build-tested** in the authoring environment (no Docker available). Treat the first `docker compose up` as your verification.

---

## Configuring entities

Three **example** entities ship in `configs/`. They are what the server seeds on a fresh start, and a handy body to post into your own project (`--data-binary @configs/customers.yaml`). They appear through `/v1` as one read-only project called `examples`.

| Entity            | Notes                                                                                                 |
| ----------------- | ----------------------------------------------------------------------------------------------------- |
| `customers`       | Base entity; uses `iki-data-generator` for names and emails                                           |
| `orders`          | References `customers`                                                                                |
| `support_tickets` | References `orders` (3-level chain); daily cadence; `int` / `bool` fields; non-zero failure injection |

Each file in `configs/` (and each body you post to a project) defines one entity — one table:

```yaml
entity: orders

fields:
  order_id: { type: uuid, primary_key: true }
  customer_id: { type: ref, ref_entity: customers }
  amount: { type: float, min: 10, max: 500 }
  status: { type: enum, values: [pending, shipped, cancelled] }
  # optional: hand a field to iki-data-generator
  # agent_job_title: { type: string, key_label: job_title }
  created_at: { type: timestamp, auto: created }
  updated_at: { type: timestamp, auto: updated }
  deleted_at: { type: timestamp, nullable: true, auto: soft_delete }
  version: { type: int, auto: version }

seed:
  initial_count: 50

update_schedule:
  cadence: hourly # hourly | daily | weekly
  new_records: [5, 15] # random range inserted per run
  mutate_existing_pct: 5 # % of existing rows updated per run
  soft_delete_pct: 1 # % of remaining rows soft-deleted per run
  jitter_seconds: 30

failure_injection:
  fail_rate: 0.0 # 0.0–1.0 probability of an injected 500
  latency_ms: 0 # artificial delay per request
```

**Field types:** `uuid`, `int`, `float`, `string`, `enum`, `bool`, `timestamp`, `ref`.

**`auto` values:** `created`, `updated`, `soft_delete`, `version`.

Any field can add `key_label: <iki provider name>` (optionally `ik_options: {...}`) to generate values via [iki-data-generator](https://pypi.org/project/iki-data-generator/) instead of the type-based generator.

Every entity needs exactly one `primary_key: true` field. Invalid configs (unknown types, dangling or circular `ref`s, bad cadence) fail at startup with a clear message.

---

## API (built-in entities)

These routes serve the **built-in YAML entities** and need a **superuser** key. A provider key gets `403`; providers use [`/v1`](#providers-projects--publishing-configs-v1).

### Per entity (`/orders` shown)

| Method   | Path                                     | Purpose                                             |
| -------- | ---------------------------------------- | --------------------------------------------------- |
| `GET`    | `/orders?limit=`                         | List (excludes soft-deleted rows)                   |
| `GET`    | `/orders/{id}`                           | Fetch one (404 if soft-deleted)                     |
| `POST`   | `/orders`                                | Create — omitted fields are generated synthetically |
| `PUT`    | `/orders/{id}`                           | Update (bumps `version` and `updated_at`)           |
| `DELETE` | `/orders/{id}`                           | Soft-delete                                         |
| `GET`    | `/orders/changes?since=<version>&limit=` | Change feed                                         |
| `GET`    | `/orders/export`                         | Snapshot or delta export                            |

### System routes

| Method | Path                             | Purpose                             |
| ------ | -------------------------------- | ----------------------------------- |
| `GET`  | `/health`                        | Liveness (no key required)          |
| `GET`  | `/entities`                      | Entity catalog                      |
| `GET`  | `/metrics`                       | Row counts and last job status      |
| `GET`  | `/scheduler/runs?entity=&limit=` | Job history                         |
| `GET`  | `/ddl`                           | DDL for every entity                |
| `GET`  | `/export`                        | Zip bundle                          |
| `POST` | `/admin/batch`                   | Bulk generate / replace / reset     |
| `POST` | `/admin/changes`                 | On-demand inserts, updates, deletes |

### The change feed

```http
GET /orders/changes?since=50&limit=100
```

```json
{
	"changes": [
		{ "order_id": "...", "op": "insert", "version": 51, "...": "..." },
		{ "order_id": "...", "op": "update", "version": 52, "...": "..." },
		{ "order_id": "...", "op": "delete", "version": 53, "deleted_at": "..." }
	],
	"next_cursor": 53,
	"has_more": false
}
```

Poll with `since=<last next_cursor>`. Each entity’s `version` is a monotonic counter used as the cursor (avoids clock-skew and same-millisecond collisions).

### Errors

Every failure — 401, 404, 422, injected 500 — uses the same envelope:

```json
{ "error": { "code": "not_found", "message": "orders 'abc' not found" } }
```

---

## Authentication

**Every route requires an API key except `/health`.** Auth is applied globally: a route added later is protected automatically. You opt a route _out_ (`security.PUBLIC_PATHS`), never _in_.

| Key                                    | Who                               | Capabilities                                                                                              |
| -------------------------------------- | --------------------------------- | --------------------------------------------------------------------------------------------------------- |
| **Superuser** (`API_KEY` / `API_KEYS`) | Operator                          | Everything: all providers’ configs and data (including private), create providers/keys, all legacy routes |
| **Provider** (issued by superuser)     | One provider (`id` + `full_name`) | `/v1` only: create projects, publish configs, read public projects, read/write own                        |

```bash
python -m app.cli genkey
export API_KEY=<the key>
curl -H 'X-API-Key: <the key>' localhost:8000/metrics
```

| Variable               | Purpose                                                     |
| ---------------------- | ----------------------------------------------------------- |
| `API_KEY` / `API_KEYS` | Superuser key(s); comma-separated list supported            |
| `REQUIRE_API_KEY=true` | Refuse to start without a key — **use in every deployment** |
| `ENABLE_DOCS=false`    | Hide `/docs`, `/redoc`, `/openapi.json`                     |

**Highlights**

- OpenAPI security scheme → **Authorize** button in `/docs`
- Constant-time comparison for superuser keys
- Header only (`X-API-Key`) — keys in query strings are rejected
- Rotate superuser keys without downtime: `API_KEYS=new,old` → move clients → drop old
- Provider keys stored as SHA-256, shown once at issue time
- Failed attempts logged (method, path, client) — never the key
- No superuser key configured → auth is off (anonymous superuser); startup logs a warning

---

## Providers, projects & publishing configs (`/v1`)

```
provider  →  project (a schema)  →  config (a table)  →  rows
```

A **provider** owns **projects**; a project holds the **configs** (tables) that belong together. Providers are created by the superuser (no open sign-up) and each authenticates with its own API key.

```bash
SU=<your superuser key>

# 1. Create a provider (key shown once)
curl -X POST localhost:8000/v1/admin/providers \
  -H "X-API-Key: $SU" -H 'Content-Type: application/json' \
  -d '{"full_name": "Acme Data Team"}'
# → { "id": "prov_…", "api_key": "…", … }

KEY=<that api_key>

# 2. Create a project — the schema your tables will live in (name is unique per provider)
curl -X POST "localhost:8000/v1/projects?on_exists=reuse" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"name": "acme", "description": "Acme demo domain"}'
# → { "id": "prj_…", "name": "acme", "config_count": 0, "created": true, … }
#   (run it again: 200, "created": false, the same id — nothing changes)
PRJ=<that id>

# 3. Publish configs (tables) into it — parents first (JSON or YAML)
for f in customers orders support_tickets; do
  curl -X POST "localhost:8000/v1/projects/$PRJ/configs" \
    -H "X-API-Key: $KEY" -H 'Content-Type: text/yaml' \
    --data-binary @configs/$f.yaml
done
# → { "id": "cfg_…", "name": "customers", "project": {"id": "prj_…", "name": "acme", …}, "seeded_rows": 30, … }

# 4. Read data and the change feed (by config id)
curl -H "X-API-Key: $KEY" "localhost:8000/v1/configs/cfg_…/data?limit=5"
curl -H "X-API-Key: $KEY" "localhost:8000/v1/configs/cfg_…/changes?since=0"

# 5. …or act on the whole project as one unit
curl -H "X-API-Key: $KEY" "localhost:8000/v1/projects/$PRJ"                  # its configs
curl -X POST "localhost:8000/v1/projects/$PRJ/batch" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"mode":"replace","confirm":true,"seed":1}'                            # refresh every table
curl -H "X-API-Key: $KEY" "localhost:8000/v1/projects/$PRJ/ddl?dialect=postgresql"   # the whole schema
curl -H "X-API-Key: $KEY" -o acme.zip "localhost:8000/v1/projects/$PRJ/export?format=sql"
```

### What a project is (and isn't)

| Rule | Behaviour |
| --- | --- |
| **Scope of `ref`** | A `ref` field can only point at a config in the **same project** — not another provider's, and not even your own other project's |
| **Scope of refresh** | `replace` / `reset` (which also refresh dependents) and a project batch only reach that project's tables |
| **Names** | A config name is unique **per project** (two projects can both have `orders`); a project name is unique **per provider** |
| **Visibility** | `is_only_me` is a **project** setting and every config inherits it. There is no per-config switch (`is_only_me` in a config body is rejected) |
| **Schema export** | `GET /v1/projects/{id}/ddl` is every table, parents first. On `dialect=postgresql` it opens with `CREATE SCHEMA IF NOT EXISTS <project>` and `SET search_path` |
| **Zip export** | `GET /v1/projects/{id}/export` — `schema.sql`, one file per config (parents first) and a `manifest.json` that names the project |
| **Names in SQL** | A project name is a plain lowercase identifier (`^[a-z][a-z0-9_]{0,31}$`), so it is safe as a schema name |
| **Table names** | Unchanged: each config is still stored as `d_<10 hex>_<name>`; a project never renames a table and never changes the change feed |
| **Built-in examples** | The `configs/*.yaml` entities form one read-only project, `examples` (`prj_system_examples`): you can read it; only the superuser can write to its tables; nobody can add to, rename or delete it through the API |

Add `is_only_me: true` to a project to hide it **and every config inside it** (default `false`: other providers may **read**, never change).

### Who can do what

| Action                                                              | Owner | Other provider                | Superuser |
| ------------------------------------------------------------------- | :---: | ----------------------------- | :-------: |
| Read project, its configs, rows, changes, export, DDL, metrics      |  yes  | yes — **unless `is_only_me`** |    yes    |
| Edit/delete project or config; publish configs; write rows; batch; simulate |  yes  | **no** (`403`)                |    yes    |
| Project (or config in it) with `is_only_me: true`                   | full  | **`404`** (as if missing)     |   full    |

A private project returns `404`, not `403`, so its existence is not revealed. Built-in YAML examples are owned by a system provider: public to read, writable only by the superuser.

### `/v1` routes

| Method                 | Path                                       | Purpose                                                |
| ---------------------- | ------------------------------------------ | ------------------------------------------------------ |
| `GET`                  | `/v1/me`                                   | Who this key is + quotas + how many projects/configs you own |
| `POST` `GET`           | `/v1/admin/providers`                      | **SU:** create provider (key once; unique name) / list |
| `PATCH`                | `/v1/admin/providers/{id}`                 | **SU:** rename or deactivate                           |
| `POST` `DELETE`        | `/v1/admin/providers/{id}/keys[/{key_id}]` | **SU:** issue / revoke keys                            |
| `POST`                 | `/v1/projects`                             | Create a project; `?on_exists=reuse` for a safe retry; SU may use `?provider_id=` |
| `GET`                  | `/v1/projects?scope=mine\|shared\|all`     | List readable projects                                 |
| `GET` `PATCH` `DELETE` | `/v1/projects/{id}`                        | Detail (with its configs) / rename, describe, share / delete project **and all its configs** (`?confirm=true`) |
| `GET`                  | `/v1/projects/{id}/ddl`                    | The whole schema (`CREATE SCHEMA` on PostgreSQL)       |
| `GET`                  | `/v1/projects/{id}/export`                 | Zip of every table: schema + data + manifest           |
| `POST`                 | `/v1/projects/{id}/batch`                  | `append` / `replace` / `reset` for every config in it  |
| `POST`                 | `/v1/projects/{id}/configs`                | Publish a config (JSON or YAML) into the project       |
| `POST`                 | `/v1/projects/{id}/configs/validate`       | Dry-run validation                                     |
| `GET`                  | `/v1/projects/{id}/configs`                | The project's configs                                  |
| `GET`                  | `/v1/configs?scope=…&project_id=`          | List readable configs (optionally one project)         |
| `GET` `PATCH` `DELETE` | `/v1/configs/{id}`                         | Definition / edit / delete (`?confirm=true`)           |
| `GET` `POST`           | `/v1/configs/{id}/data`                    | List (keyset: `next_after` → `after`) / create row     |
| `GET` `PUT` `DELETE`   | `/v1/configs/{id}/data/{row_id}`           | One row / update / soft-delete                         |
| `GET`                  | `/v1/configs/{id}/changes?since=`          | Change feed                                            |
| `GET`                  | `/v1/configs/{id}/export`                  | Snapshot or `?since=` delta (`csv`, `ndjson`, `sql`)   |
| `GET`                  | `/v1/configs/{id}/ddl`                     | `CREATE TABLE` / `INDEX`                               |
| `GET`                  | `/v1/configs/{id}/metrics` · `/runs`       | Row count / job runs                                   |
| `POST`                 | `/v1/configs/{id}/batch`                   | `append` / `replace` / `reset` for one config (+ dependents) |
| `POST`                 | `/v1/configs/{id}/simulate`                | On-demand inserts, updates, deletes                    |
| `GET`                  | `/v1/export?configs=a,b`                   | Zip of chosen configs — all from one project           |

> **Breaking change:** `POST /v1/configs` and `POST /v1/configs/validate` no longer exist — a config is always published *into a project*. Everything addressed by config id (`/v1/configs/{id}/…`) is unchanged.

### Config rules (API-published)

- **Names:** `^[a-z][a-z0-9_]{0,31}$` (`schema` and `manifest` reserved); field names up to 40 chars. Unique per project.
- **Unknown keys rejected** (typos fail loudly) — including `is_only_me`, which is a project setting.
- **`version` auto-added** if omitted; must be `type: int, auto: version` if declared.
- **Exactly one primary key**, `type: uuid`.
- **`ref` only within the same project**; no self-reference or cycles.
- **Trial generation** before save (bad `key_label` fails at publish time).
- **YAML anchors/aliases rejected**.

### Quotas (env vars)

| Variable                     | Default | Limits                                            |
| ---------------------------- | ------- | ------------------------------------------------- |
| `MAX_PROJECTS_PER_PROVIDER`  | `3`     | Projects per provider                             |
| `MAX_CONFIGS_PER_PROJECT`    | `10`    | Configs (tables) in one project                   |
| `MAX_CONFIGS_PER_PROVIDER`   | `10`    | Configs across **all** of a provider's projects (the storage ceiling) |
| `MAX_FIELDS_PER_CONFIG`      | `40`    | Fields per config                                 |
| `MAX_INITIAL_COUNT`          | `10000` | `seed.initial_count`                              |
| `MAX_ROWS_PER_CONFIG`        | `50000` | Total rows (batch, simulate, scheduler stop here) |
| `MAX_LATENCY_MS`             | `5000`  | `failure_injection.latency_ms`                    |
| `MAX_CONFIG_BYTES`           | `65536` | Config/project body size                          |

### Editing and deleting

- **Project `PATCH`:** rename, change `description`, or flip `is_only_me` — takes effect immediately and never touches data.
- **Project `DELETE`** (`?confirm=true`): deletes every config in it and all their data, children first, and frees every quota slot it held.
- **Config `PATCH`** merges `seed` / `update_schedule` / `failure_injection` and takes effect immediately.
- Changing **`fields`** drops data, restarts the feed, re-seeds — needs `?confirm=true`; blocked if dependents exist.
- **A config's name cannot change** — delete and recreate.
- Config `DELETE` drops table + history + job; blocked if dependents exist.
- Built-in YAML configs and the `examples` project: only the project's `is_only_me` can be toggled (by the superuser); anything else is `409 managed_by_yaml`.

### Upgrading an existing database

On startup the app migrates a pre-projects database in place (idempotent, no data moves): each provider that already has configs gets one project called `default` holding them, and the built-in examples go to `examples`. **Privacy is never loosened** — if *any* of a provider's configs was private, its whole `default` project becomes private; re-share it with `PATCH /v1/projects/{id}`. The migration was exercised on SQLite; run it against a copy of your PostgreSQL database first.

### How isolation works

Each published config gets a table named `d_<10 hex>_<name>`. Two projects' `orders` never share a table. `ref` fields resolve only within the project, and every bulk operation is handed only that project's tables. API responses, DDL, exports, manifests and error messages always use the logical names the provider chose — never the internal table names.

---

## Batch generation, DDL export & refresh

### Refresh the dataset

```bash
# Regenerate everything from scratch, reproducibly
curl -X POST localhost:8000/admin/batch -H 'Content-Type: application/json' \
  -d '{"mode":"replace","seed":42,"confirm":true}'

# Bulk-add 50,000 orders in chunks of 5,000
curl -X POST localhost:8000/admin/batch -H 'Content-Type: application/json' \
  -d '{"entities":["orders_sample"],"count":50000,"batch_size":5000}'
```

| Field              | Meaning                                                    |
| ------------------ | ---------------------------------------------------------- |
| `mode`             | `append` (default), `replace`, or `reset`                  |
| `entities`         | Which entities (default: all, or keys of `counts`)         |
| `count` / `counts` | Rows per entity                                            |
| `batch_size`       | Rows per insert chunk (1–10,000, default 1,000)            |
| `seed`             | Same seed + same request → same ids, values, relationships |
| `confirm`          | Required `true` for `replace` / `reset`                    |

| Mode      | Behaviour                                                           | Consumers on `/changes`              |
| --------- | ------------------------------------------------------------------- | ------------------------------------ |
| `append`  | Adds rows                                                           | Unaffected                           |
| `replace` | Wipe + regenerate; deletes logged; versions keep climbing           | Keep working (see deletes + inserts) |
| `reset`   | Truncate rows, change log, scheduler history; versions restart at 1 | Must re-baseline from `since=0`      |

**Worth knowing**

- All-or-nothing (one transaction).
- Dependents come along on `replace`/`reset` (`auto_included`).
- Parents first — empty parent → `409 missing_parent_rows`.
- Serialised with the scheduler (gap-free versions).

### Export the DDL

```bash
curl 'localhost:8000/ddl?dialect=postgresql'
curl 'localhost:8000/ddl?include_system=true'   # also change_log + scheduler_runs
```

`IF NOT EXISTS`, parents-first, generated from the live tables. Dialects: `sqlite`, `postgresql`.

### Apply a batch of changes

```bash
curl -X POST localhost:8000/admin/changes -H 'Content-Type: application/json' \
  -d '{"entities":["orders_sample"],"inserts":100,"updates":500,"deletes":20,"seed":1}'
```

Omit counts → one default scheduler tick. Counts larger than available live rows are clamped and explained under `notes`.

### Export the data

```bash
curl 'localhost:8000/orders/export?format=csv'
curl 'localhost:8000/orders/export?format=sql&dialect=postgresql'
curl 'localhost:8000/orders/export?since=120&format=ndjson'   # delta
curl -o export.zip 'localhost:8000/export?format=sql'         # full bundle
```

|               | Snapshot                                            | Delta (`since=N`)                              |
| ------------- | --------------------------------------------------- | ---------------------------------------------- |
| Contents      | Live rows (`include_deleted=true` for soft-deleted) | One row per insert/update/delete + `op` column |
| Formats       | `csv`, `ndjson`, `sql`                              | `csv`, `ndjson`                                |
| Cursor header | `X-Snapshot-Cursor`                                 | `X-From-Cursor`, `X-To-Cursor`                 |

**Load, then catch up:** load the snapshot, then apply deltas (or poll `/changes`) from the snapshot cursor.

---

## Project structure

```
synthetic-backend/
├── configs/                 # EXAMPLE entities (one YAML file each) — not used by the tests
├── PROJECTS.md              # the projects (schema) model, routes, migration
├── app/
│   ├── main.py              # ASGI entrypoint, lifespan, seeding, legacy routes
│   ├── cli.py               # genkey | create-provider | ddl | batch | changes | export
│   ├── core/
│   │   └── errors.py        # ApiError
│   ├── config/
│   │   ├── entities.py      # YAML → EntityConfig
│   │   └── provider.py      # HTTP config validation + quotas
│   ├── db/
│   │   ├── engine.py        # engine (SQLite default; DATABASE_URL override)
│   │   └── models.py        # dynamic Core tables + change_log + registry (providers, keys, projects, configs)
│   ├── security/
│   │   └── api_keys.py      # global API-key auth, Principal
│   ├── api/
│   │   ├── error_handlers.py
│   │   └── v1/routes.py     # /v1 providers, projects, configs, data, export
│   └── services/
│       ├── catalog.py       # projects, provisioning, scheduler jobs, access rules
│       ├── registry.py      # providers, hashed keys, project + config rows, migration
│       ├── entity_ops.py    # shared row operations
│       ├── generator.py     # synthetic value generation
│       ├── batch.py         # bulk generation, replace/reset, change batches
│       ├── scheduler.py     # per-entity insert/mutate/soft-delete jobs
│       ├── changefeed.py    # change_log writes + /changes reads
│       ├── export.py        # snapshot / delta / bundle
│       ├── ddl.py           # CREATE TABLE / INDEX (+ CREATE SCHEMA for a project)
│       └── metrics.py       # /metrics and /scheduler/runs
├── tests/                   # entity_fixtures.py owns the built-in test entities; conftest builds the rest
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── deployment-guide.md
```

Imports flow downwards: `api/` and `cli.py` → `services/` → `db/`, `config/`, `security/`. Nothing in `services/` imports from `api/`.

---

## Design notes

- **SQLAlchemy Core, not the ORM** — schema is runtime data; Core builds tables dynamically.
- **Explicit `change_log`** — insert vs update can’t be inferred from a row snapshot; each write logs its op.
- **Soft-delete** — hard-deleted rows would leave nothing to report in the feed.
- **`pool_pre_ping` + `pool_recycle=240`** on non-SQLite — handles serverless Postgres idle disconnects (verified on PostgreSQL 16).
- **In-process scheduler** (APScheduler) — simplest to run and demo.
- **Startup seeding is dependency-ordered** — `ref` fields always resolve.
- **Namespaced tables, logical names** — internal `d_<id>_<name>`; external API always uses provider names.
- **A project is a scope, not a prefix** — the engine is handed one project's `(configs, tables)` at a time, so refs, bulk refresh and bundles can't cross projects by construction. Table names stay `d_<id>_<name>` (no real database schema is created at runtime — SQLite has none); the schema appears in the **exports**.
- **Visibility lives on the project** — one switch per schema, so a shared table can never reference a hidden parent.
- **Two access checks** — `catalog.resolve` (configs) and `catalog.resolve_project` (projects) are the only gates for read/write/private rules; a test walks the route table to prove no route skips them.
- **Hashed provider keys** — SHA-256 of 256-bit secrets; lookup by hash.
- **Legacy and `/v1` share `entity_ops.py`** — routes can’t drift apart.

---

## Known limitations

Deliberate simplifications, not bugs:

| Topic              | Detail                                                                                 |
| ------------------ | -------------------------------------------------------------------------------------- |
| Change feed values | Entries carry the row’s _current_ values, not a point-in-time snapshot of that change  |
| Version allocation | `max(version)+1` per entity — correct under a single writer                            |
| Docker             | Build not verified in the authoring environment                                        |
| Free-tier sleep    | In-process scheduler stops when the process sleeps; ping `/health` or accept idle gaps |
| `reset` cursors    | Always re-baseline from `since=0` after a reset                                        |
| Seeds              | Pin ids/values/relationships — not timestamps or version numbers                       |
| Single instance    | Catalog, scheduler and write lock live in one process’s memory                         |
| Field changes      | Drop + re-seed (no `ALTER`); requires `confirm=true`                                   |
| Change log growth  | Never trimmed except by `reset` or deleting the config                                 |
| Sharing model      | All-or-nothing per project (`is_only_me`); no per-config switch, no per-provider grants |
| Cross-project refs | A `ref` can't point outside its project — copy the parent into the project instead      |
| Superuser          | One role via env vars; no per-admin identity or audit trail                            |
| Not implemented    | Schema-drift injection, cron cadences, a migration framework (`create_all` plus one additive, idempotent step for the projects upgrade) |

See the full list in the source documentation for measured sizes, export edge cases, and operational notes.

---

## Deploying

See **[deployment-guide.md](./deployment-guide.md)** for a walkthrough of a free stack (Render + Neon + UptimeRobot) that this project was tested against.

**Minimum production checklist**

1. `python -m app.cli genkey` → set `API_KEY`
2. `REQUIRE_API_KEY=true`
3. Prefer PostgreSQL via `DATABASE_URL`
4. `ENABLE_DOCS=false` if you don’t want public OpenAPI
5. HTTPS in front (API keys travel in headers)
6. Keep the process awake if you rely on the scheduler (or accept idle gaps)

---

## Tech stack

| Layer          | Choice                                      |
| -------------- | ------------------------------------------- |
| API            | FastAPI                                     |
| ORM / DB       | SQLAlchemy 2.0 (Core) · SQLite / PostgreSQL |
| Validation     | Pydantic v2                                 |
| Scheduler      | APScheduler                                 |
| Config         | PyYAML                                      |
| Synthetic data | iki-data-generator                          |
| Tests          | pytest                                      |
| Packaging      | Docker Compose                              |

---

## License

See the repository for license terms.