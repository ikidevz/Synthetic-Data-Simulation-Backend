"""Synthetic Data Simulation Backend.

A config-driven, self-mutating data source for building and stress-testing
data-ingestion pipelines. Entities are YAML files; the engine turns them into
SQLAlchemy tables, seeds them, mutates them on a schedule, and serves CRUD,
a change feed, exports and DDL.

Layout
------
Imports flow strictly downwards, one layer at a time:

    api/  --.            HTTP surface: routers and the error envelope.
           +-> services/  All business logic: generation, batch, scheduling,
    cli.py --'            the change feed, exports, DDL, providers, catalog.

    main.py and cli.py are the two entrypoints and sit at the package root:
        uvicorn app.main:app        python -m app.cli

    core/      Framework-free primitives (ApiError).
    security/  API-key authentication and the Principal it resolves to.
    db/        How we connect (engine) and what the schema is (models).
    config/    What an entity is: YAML -> EntityConfig, and the stricter
               validation + quotas applied to configs submitted over HTTP.

Directory guide
---------------
    main.py                 ASGI entrypoint: app factory, lifespan, seeding and
                            the config-generated legacy (superuser-only) routes.
    cli.py                  genkey|create-provider|ddl|batch|changes|export
    core/errors.py          ApiError -- BatchError subclasses it.
    config/entities.py      EntityConfig/FieldConfig + the YAML loader.
    config/provider.py      Validation, naming rules and quotas for HTTP configs.
    db/engine.py            SQLAlchemy engine (SQLite default, DATABASE_URL).
    db/models.py            Dynamic Core tables + change_log, scheduler_runs,
                            registry tables.
    security/api_keys.py    Global header-only API-key auth, Principal, superuser.
    api/error_handlers.py   The one {"error": {code, message}} envelope.
    api/v1/routes.py        /v1: providers, configs, data, changes, export.
    services/generator.py   Synthetic value generation per field type.
    services/batch.py       Bulk generation, replace/reset refresh, change batches.
    services/entity_ops.py  Row-level reads/writes shared by both API surfaces.
    services/scheduler.py   Per-entity insert/mutate/soft-delete jobs.
    services/changefeed.py  change_log writes and /changes reads.
    services/export.py      Snapshot / delta / bundle export (csv, ndjson, sql).
    services/ddl.py         CREATE TABLE / INDEX export.
    services/metrics.py     /metrics and /scheduler/runs.
    services/registry.py    Providers, hashed API keys, config rows (database).
    services/catalog.py     Live catalog: provisioning, jobs, access rules.
"""
