"""Command-line tools that work without starting the server.

    python -m app.cli genkey
    python -m app.cli create-provider --full-name "Acme Data Team"
    python -m app.cli ddl [--dialect sqlite|postgresql] [--include-system] [--out schema.sql]
    python -m app.cli batch [--mode append|replace|reset] [--entities a b]
                            [--count N] [--counts orders=1000 customers=50]
                            [--batch-size N] [--seed N] [--yes]
    python -m app.cli changes [--entities a b] [--inserts N] [--updates N] [--deletes N]
                              [--batch-size N] [--seed N]
    python -m app.cli export --out-dir DIR [--format csv|ndjson|sql] [--dialect ...]
                             [--entities a b] [--include-deleted] [--since orders=120]

`batch` talks to whatever database DATABASE_URL points at (SQLite file by
default), creating the tables first if they don't exist yet.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from typing import Dict, List, Optional

from .config.entities import get_config_dir, load_entity_configs
from .core.errors import ApiError
from .db import models
from .db.engine import engine
from .services import registry
from .services.batch import DEFAULT_BATCH_SIZE, VALID_MODES, BatchError, run_batch, run_changes
from .services.ddl import DIALECTS, generate_ddl
from .services.export import FORMATS, parse_since, write_bundle


def _parse_counts(pairs: Optional[List[str]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for pair in pairs or []:
        name, sep, value = pair.partition("=")
        if not sep or not value.isdigit():
            raise ValueError(
                f"--counts entries must look like name=N, got '{pair}'")
        counts[name] = int(value)
    return counts


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("genkey", help="print a strong random API key")

    prov = sub.add_parser(
        "create-provider", help="create a provider and print its API key (shown once)")
    prov.add_argument("--full-name", required=True,
                      help="the provider's display name")

    ddl = sub.add_parser(
        "ddl", help="print CREATE TABLE / CREATE INDEX statements")
    ddl.add_argument("--dialect", choices=sorted(DIALECTS),
                     help="default: the dialect of DATABASE_URL")
    ddl.add_argument("--include-system", action="store_true",
                     help="also emit change_log and scheduler_runs")
    ddl.add_argument("--out", help="write to this file instead of stdout")

    batch = sub.add_parser(
        "batch", help="bulk-generate or refresh synthetic data")
    batch.add_argument("--mode", choices=VALID_MODES, default="append")
    batch.add_argument("--entities", nargs="*",
                       help="default: every entity (or the keys of --counts)")
    batch.add_argument(
        "--count", type=int, help="rows per entity (default: seed.initial_count from each config)")
    batch.add_argument("--counts", nargs="*", metavar="NAME=N",
                       help="per-entity row counts")
    batch.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    batch.add_argument("--seed", type=int, help="make the run reproducible")
    batch.add_argument("--yes", action="store_true",
                       help="confirm replace/reset, which delete data")

    changes = sub.add_parser(
        "changes", help="apply a batch of inserts / updates / soft-deletes")
    changes.add_argument("--entities", nargs="*", help="default: every entity")
    changes.add_argument("--inserts", type=int)
    changes.add_argument("--updates", type=int)
    changes.add_argument("--deletes", type=int)
    changes.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    changes.add_argument("--seed", type=int)

    export = sub.add_parser(
        "export", help="write schema + data files + manifest to a directory")
    export.add_argument("--out-dir", required=True)
    export.add_argument("--format", choices=FORMATS, default="csv")
    export.add_argument("--dialect", choices=sorted(DIALECTS),
                        help="default: the dialect of DATABASE_URL")
    export.add_argument("--entities", nargs="*", help="default: every entity")
    export.add_argument("--include-deleted", action="store_true")
    export.add_argument("--since", nargs="*", metavar="NAME=N",
                        help="export that entity's changes after version N instead of a snapshot")
    return parser


def _default_dialect() -> str:
    return engine.dialect.name if engine.dialect.name in DIALECTS else "sqlite"


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "genkey":
        print(secrets.token_urlsafe(32))
        return 0
    if args.command == "create-provider":
        try:
            models.metadata.create_all(engine, tables=models.REGISTRY_TABLES)
            registry.ensure_system_provider()
            provider = registry.create_provider(args.full_name)
            key = registry.issue_key(provider["id"], label="initial")
        except ApiError as exc:
            print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
            return 1
        print(json.dumps({**provider, "api_key": key["api_key"]}, indent=2))
        return 0
    try:
        configs = load_entity_configs(get_config_dir())
        tables = models.build_tables(configs)

        if args.command == "ddl":
            dialect = args.dialect or _default_dialect()
            text = generate_ddl(tables, dialect=dialect,
                                include_system=args.include_system)
            if args.out:
                with open(args.out, "w") as fh:
                    fh.write(text)
            else:
                sys.stdout.write(text)
            return 0

        if args.command == "export":
            os.makedirs(args.out_dir, exist_ok=True)

            def add_file(name, chunks):
                with open(os.path.join(args.out_dir, name), "w", encoding="utf-8", newline="") as fh:
                    for chunk in chunks:
                        fh.write(chunk)

            manifest = write_bundle(
                engine, tables, configs, add_file, fmt=args.format,
                dialect=args.dialect or _default_dialect(), entities=args.entities or None,
                include_deleted=args.include_deleted, since=parse_since(
                    args.since),
            )
            print(json.dumps(manifest, indent=2))
            return 0

        models.metadata.create_all(engine)
        if args.command == "changes":
            print(json.dumps(run_changes(
                engine, tables, configs, entities=args.entities, inserts=args.inserts,
                updates=args.updates, deletes=args.deletes, batch_size=args.batch_size, seed=args.seed,
            ), indent=2))
            return 0

        report = run_batch(
            engine, tables, configs,
            mode=args.mode, entities=args.entities, counts=_parse_counts(
                args.counts),
            count=args.count, batch_size=args.batch_size, seed=args.seed, confirm=args.yes,
        )
        print(json.dumps(report, indent=2))
        return 0
    except BatchError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
