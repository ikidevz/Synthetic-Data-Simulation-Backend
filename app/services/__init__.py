"""All business logic, one layer below the HTTP surface.

  generator.py    synthetic value generation per field type
  batch.py        bulk generation, replace/reset refresh, change batches
  entity_ops.py   row-level reads/writes shared by both API surfaces
  scheduler.py    per-entity insert / mutate / soft-delete background jobs
  changefeed.py   change_log writes and the /changes cursor reads
  export.py       snapshot / delta / bundle export (csv, ndjson, sql)
  ddl.py          CREATE TABLE / CREATE INDEX export
  metrics.py      /metrics and /scheduler/runs
  registry.py     providers, hashed API keys, config rows (database)
  catalog.py      the live catalog: provisioning, scheduler jobs, access rules

These modules take an engine, a table map and a config map as arguments and
never reach for global state themselves, which is what lets the same engine
serve both the built-in YAML entities and each provider's own configs.
"""