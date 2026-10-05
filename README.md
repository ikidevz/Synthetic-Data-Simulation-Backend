# Synthetic Data Simulation Backend

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-310%20passing-brightgreen.svg)](#run-the-tests)
[![License](https://img.shields.io/badge/license-see%20repo-lightgrey.svg)](#)

A FastAPI backend that serves **purely synthetic, config-driven data** and **mutates it on its own schedule** (hourly / daily / weekly) — a self-contained “living source system” for building and stress-testing data ingestion pipelines.

![Cover](./assets/cover.png)

---

## Features

- **Config-driven** — an entity is a YAML file, or a `POST /v1/configs`. Adding one needs zero Python changes.
- **Multi-provider** — providers publish their own configs through the API, authenticated by their own API key. Each config’s data is readable by other providers unless the owner marks it `is_only_me`; only the owner can write. A **superuser** key can do everything.
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
- [Providers & publishing configs (`/v1`)](#providers--publishing-configs-v1)
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

310 tests. Default target is SQLite. Point `TEST_DATABASE_URL` at an **empty** PostgreSQL database to run the same suite there (its `public` schema is wiped first):

```bash
TEST_DATABASE_URL=postgresql+psycopg2://synthetic:synthetic@localhost:5433/synthetic_test pytest -v
```

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

Three example entities ship in `configs/`:

| Entity            | Notes                                                                                                 |
| ----------------- | ----------------------------------------------------------------------------------------------------- |
| `customers`       | Base entity; uses `iki-data-generator` for names and emails                                           |
| `orders`          | References `customers`                                                                                |
| `support_tickets` | References `orders` (3-level chain); daily cadence; `int` / `bool` fields; non-zero failure injection |

Each file in `configs/` defines one entity:

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

These routes serve the **built-in YAML entities** and need a **superuser** key. A provider key gets `403`; providers use [`/v1`](#providers--publishing-configs-v1).

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
| **Provider** (issued by superuser)     | One provider (`id` + `full_name`) | `/v1` only: publish configs, read public configs, read/write own                                          |

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

## Providers & publishing configs (`/v1`)

A **provider** publishes configs and owns them. Providers are created by the superuser (no open sign-up). Each authenticates with its own API key.

```bash
SU=<your superuser key>

# 1. Create a provider (key shown once)
curl -X POST localhost:8000/v1/admin/providers \
  -H "X-API-Key: $SU" -H 'Content-Type: application/json' \
  -d '{"full_name": "Acme Data Team"}'
# → { "id": "prov_…", "api_key": "…", … }

KEY=<that api_key>

# 2. Publish a config (JSON or YAML)
curl -X POST localhost:8000/v1/configs \
  -H "X-API-Key: $KEY" -H 'Content-Type: text/yaml' \
  --data-binary @configs/customers.yaml
# → { "id": "cfg_…", "name": "customers", "seeded_rows": 30, … }

# 3. Read data and the change feed
curl -H "X-API-Key: $KEY" "localhost:8000/v1/configs/cfg_…/data?limit=5"
curl -H "X-API-Key: $KEY" "localhost:8000/v1/configs/cfg_…/changes?since=0"
```

Add `is_only_me: true` to keep a config private (default `false`: other providers may **read**, never change).

### Who can do what

| Action                                           | Owner | Other provider                | Superuser |
| ------------------------------------------------ | :---: | ----------------------------- | :-------: |
| Read config, rows, changes, export, DDL, metrics |  yes  | yes — **unless `is_only_me`** |    yes    |
| Edit/delete config; write rows; batch; simulate  |  yes  | **no** (`403`)                |    yes    |
| Config with `is_only_me: true`                   | full  | **`404`** (as if missing)     |   full    |

Private configs return `404`, not `403`, so existence is not revealed. Built-in YAML examples are owned by a system provider: public to read, writable only by the superuser.

### `/v1` routes

| Method                 | Path                                       | Purpose                                              |
| ---------------------- | ------------------------------------------ | ---------------------------------------------------- |
| `GET`                  | `/v1/me`                                   | Who this key is + quotas                             |
| `POST` `GET`           | `/v1/admin/providers`                      | **SU:** create provider (key once; unique name) / list |
| `PATCH`                | `/v1/admin/providers/{id}`                 | **SU:** rename or deactivate                         |
| `POST` `DELETE`        | `/v1/admin/providers/{id}/keys[/{key_id}]` | **SU:** issue / revoke keys                          |
| `POST`                 | `/v1/configs`                              | Publish (JSON or YAML); SU may use `?provider_id=`   |
| `POST`                 | `/v1/configs/validate`                     | Dry-run validation                                   |
| `GET`                  | `/v1/configs?scope=mine\|shared\|all`      | List readable configs                                |
| `GET` `PATCH` `DELETE` | `/v1/configs/{id}`                         | Definition / edit / delete (`?confirm=true`)         |
| `GET` `POST`           | `/v1/configs/{id}/data`                    | List (keyset: `next_after` → `after`) / create row   |
| `GET` `PUT` `DELETE`   | `/v1/configs/{id}/data/{row_id}`           | One row / update / soft-delete                       |
| `GET`                  | `/v1/configs/{id}/changes?since=`          | Change feed                                          |
| `GET`                  | `/v1/configs/{id}/export`                  | Snapshot or `?since=` delta (`csv`, `ndjson`, `sql`) |
| `GET`                  | `/v1/configs/{id}/ddl`                     | `CREATE TABLE` / `INDEX`                             |
| `GET`                  | `/v1/configs/{id}/metrics` · `/runs`       | Row count / job runs                                 |
| `POST`                 | `/v1/configs/{id}/batch`                   | `append` / `replace` / `reset`                       |
| `POST`                 | `/v1/configs/{id}/simulate`                | On-demand inserts, updates, deletes                  |
| `GET`                  | `/v1/export?configs=a,b`                   | Zip: schema + data + manifest                        |

### Config rules (API-published)

- **Names:** `^[a-z][a-z0-9_]{0,31}$` (`schema` and `manifest` reserved); field names up to 40 chars. Unique per provider.
- **Unknown keys rejected** (typos fail loudly).
- **`version` auto-added** if omitted; must be `type: int, auto: version` if declared.
- **Exactly one primary key**, `type: uuid`.
- **`ref` only within your own configs**; no self-reference or cycles.
- **Trial generation** before save (bad `key_label` fails at publish time).
- **YAML anchors/aliases rejected**.

### Quotas (env vars)

| Variable                   | Default | Limits                                            |
| -------------------------- | ------- | ------------------------------------------------- |
| `MAX_CONFIGS_PER_PROVIDER` | `10`    | Configs per provider                              |
| `MAX_FIELDS_PER_CONFIG`    | `40`    | Fields per config                                 |
| `MAX_INITIAL_COUNT`        | `10000` | `seed.initial_count`                              |
| `MAX_ROWS_PER_CONFIG`      | `50000` | Total rows (batch, simulate, scheduler stop here) |
| `MAX_LATENCY_MS`           | `5000`  | `failure_injection.latency_ms`                    |
| `MAX_CONFIG_BYTES`         | `65536` | Config body size                                  |

### Editing and deleting

- `PATCH` merges `seed` / `update_schedule` / `failure_injection`; toggles `is_only_me`; takes effect immediately.
- Changing **`fields`** drops data, restarts the feed, re-seeds — needs `?confirm=true`; blocked if dependents exist.
- **Name cannot change** — delete and recreate.
- `DELETE` drops table + history + job; blocked if dependents exist.
- Built-in YAML configs: only `is_only_me` can be toggled (by superuser); otherwise `409 managed_by_yaml`.

### How isolation works

Each published config gets a table named `d_<10 hex>_<name>`. Two providers’ `orders` never share a table. `ref` fields resolve only within a provider’s own scope. API responses, DDL, exports, manifests and error messages always use the logical names the provider chose — never the internal table names.

---

## Batch generation, DDL export & refresh

### Refresh the dataset

```bash
# Regenerate everything from scratch, reproducibly
curl -X POST localhost:8000/admin/batch -H 'Content-Type: application/json' \
  -d '{"mode":"replace","seed":42,"confirm":true}'

# Bulk-add 50,000 orders in chunks of 5,000
curl -X POST localhost:8000/admin/batch -H 'Content-Type: application/json' \
  -d '{"entities":["orders"],"count":50000,"batch_size":5000}'
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
  -d '{"entities":["orders"],"inserts":100,"updates":500,"deletes":20,"seed":1}'
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
├── configs/                 # one YAML file per built-in entity
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
│   │   └── models.py        # dynamic Core tables + change_log + registry
│   ├── security/
│   │   └── api_keys.py      # global API-key auth, Principal
│   ├── api/
│   │   ├── error_handlers.py
│   │   └── v1/routes.py     # /v1 providers, configs, data, export
│   └── services/
│       ├── catalog.py       # provisioning, scheduler jobs, access rules
│       ├── registry.py      # providers, hashed keys, config rows
│       ├── entity_ops.py    # shared row operations
│       ├── generator.py     # synthetic value generation
│       ├── batch.py         # bulk generation, replace/reset, change batches
│       ├── scheduler.py     # per-entity insert/mutate/soft-delete jobs
│       ├── changefeed.py    # change_log writes + /changes reads
│       ├── export.py        # snapshot / delta / bundle
│       ├── ddl.py           # CREATE TABLE / INDEX
│       └── metrics.py       # /metrics and /scheduler/runs
├── tests/
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
- **One access check** — `catalog.resolve` is the single gate for read/write/private rules.
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
| Sharing model      | All-or-nothing (`is_only_me`); no per-provider grants                                  |
| Superuser          | One role via env vars; no per-admin identity or audit trail                            |
| Not implemented    | Schema-drift injection, cron cadences, migrations (`create_all` only)                  |

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
