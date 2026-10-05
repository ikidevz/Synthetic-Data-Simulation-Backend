"""How we connect to the database, and what the schema is.

    engine.py  The SQLAlchemy engine. SQLite by default; set DATABASE_URL to a
               Postgres URL to swap databases with no code changes.
    models.py  Dynamic SQLAlchemy Core Table objects built from EntityConfig,
               plus the system tables (change_log, scheduler_runs) and the
               provider registry tables.

Nothing above this layer should need to build a connection or define a table.
"""