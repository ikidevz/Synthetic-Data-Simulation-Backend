"""Database engine/connection setup.

Defaults to a local SQLite file for Levels 1-5 (zero setup). Set the
DATABASE_URL env var to a Postgres URL (e.g. postgresql+psycopg2://...)
to swap databases with no code changes — see Level 5/8 in the blueprint.
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./synthetic.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith(
    "sqlite") else {}

# Serverless Postgres hosts (Neon, Supabase, ...) suspend an idle database and drop its
# connections. Without pre-ping, the first request after an idle spell — or the
# scheduler's next tick — would be handed a dead pooled connection and fail.
# pool_pre_ping tests a connection before use and transparently replaces a dead one;
# pool_recycle retires connections before a typical idle timeout (Neon's is 5 minutes).
engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
    **({} if DATABASE_URL.startswith("sqlite") else {"pool_recycle": 240}),
)
