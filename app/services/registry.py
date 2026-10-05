"""Database-backed registry: providers, their API keys, and the configs they published.

A *provider* is a publisher of configs (an `id` and a `full_name`). Each provider
authenticates with one or more API keys. Keys are random 256-bit values; only their
SHA-256 is stored (a fast hash is appropriate precisely because the secret is
high-entropy — there is nothing to brute-force), and the key itself is returned
exactly once, when it is issued.

The `API_KEY` / `API_KEYS` environment variables remain the *superuser* keys: they
create providers and can see and change everything. They never touch this table.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Index, delete, func, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.schema import CreateIndex

from ..core.errors import ApiError
from ..db import models
from ..db.engine import engine

logger = logging.getLogger("registry")

SYSTEM_PROVIDER_ID = "prov_system"
SYSTEM_PROVIDER_NAME = "System (built-in examples)"
MAX_FULL_NAME = 100
_LAST_USED_REFRESH = timedelta(minutes=5)

# Characters that render as nothing (byte-order marks, zero-width space/joiners, bidi
# controls, soft hyphen) but are not whitespace, so `str.split()` keeps them and they would
# otherwise make two identical-looking names compare as different. Written as explicit code
# points: the literal characters are invisible in an editor and easy to mangle.
_INVISIBLE = re.compile(
    "["
    "­"  # soft hyphen
    "᠎"  # Mongolian vowel separator
    "-‏"  # zero-width space/non-joiner/joiner, LTR/RTL mark
    "‪-‮"  # bidi embedding/override
    "⁠-⁤"  # word joiner, invisible operators
    "⁦-⁩"  # bidi isolate controls
    "﻿"  # BOM / zero-width no-break space
    "]"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str, nbytes: int = 6) -> str:
    return f"{prefix}_{secrets.token_hex(nbytes)}"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _name_key(name: str) -> str:
    """The comparison key for a provider's name: case- and whitespace-insensitive, so
    '  Alice  Almeida ' and 'alice almeida' are the same provider name. Names are a
    display label, not a key -- `id` is -- but two providers must never share one.

    The normalisation is deliberately aggressive, because anything this function lets
    through is a way to hold the same name twice:
      * NFKC folds compatibility variants, so the "fi" ligature and full-width spellings
        match their ASCII equivalents.
      * Zero-width, BOM and bidi characters are stripped. They render as nothing but
        survive `.strip()`, so a leading BOM would otherwise buy a second "Alice".
      * Runs of whitespace collapse to one space, and the result is case-folded.

    The value produced here is stored in `registry_providers.name_key` and is UNIQUE in
    the database; this function is the single definition of what "the same name" means.
    """
    text = unicodedata.normalize("NFKC", name)
    text = "".join(ch for ch in text if not _INVISIBLE.match(ch))
    return " ".join(text.split()).casefold()


def ensure_name_key_column() -> None:
    """Add and backfill `registry_providers.name_key`, then enforce uniqueness on it.

    There is no migration framework here (see the notes in README/INSTRUCTIONS), and
    `create_all` cannot add a column to a table that already exists -- so an existing
    database needs this one additive step. It is idempotent and safe to run at every
    startup, which is exactly where `create_all` is already called.
    """
    inspector = inspect(engine)
    if "registry_providers" not in inspector.get_table_names():
        return  # create_all has not run yet; the column arrives with the new table
    if "name_key" not in {c["name"] for c in inspector.get_columns("registry_providers")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE registry_providers ADD COLUMN name_key VARCHAR"))
    # Backfill before indexing: the unique index cannot be built while rows hold NULL or
    # colliding keys. This runs on every startup, so it must only touch rows that are
    # actually wrong -- rewriting correct rows would be churn, and rewriting them one by
    # one can transiently collide with a row that is about to be updated.
    with engine.begin() as conn:
        rows = conn.execute(select(models.registry_providers.c.id,
                                  models.registry_providers.c.full_name,
                                  models.registry_providers.c.name_key)).all()
        seen: set = set()
        for pid, full_name, stored in rows:
            key = _name_key(full_name or "")
            if stored == key:
                continue  # already correct
            if key in seen:
                # A pre-existing duplicate. Leave it NULL rather than pick a winner: the
                # unique index treats NULLs as distinct, so this row keeps working and the
                # two providers stay visible for an operator to reconcile.
                logger.warning("registry_providers: '%s' (%s) duplicates the name of another "
                               "provider; leaving it unindexed until it is renamed.", full_name, pid)
                new_key = None
            else:
                seen.add(key)
                new_key = key
            conn.execute(update(models.registry_providers)
                          .where(models.registry_providers.c.id == pid)
                          .values(name_key=new_key))
    index = Index("uq_registry_providers_name_key",
                  models.registry_providers.c.name_key, unique=True)
    try:
        with engine.begin() as conn:
            conn.execute(CreateIndex(index, if_not_exists=True))
    except (OperationalError, ProgrammingError, IntegrityError):
        # Should not normally be reachable: duplicates are NULLed above so the index can
        # build. If it still fails, log loudly and continue rather than blocking startup --
        # the Python-side check still prevents new duplicates.
        logger.warning(
            "could not enforce unique provider names on registry_providers.name_key; new "
            "duplicates are still rejected in application code.", exc_info=True)


def clean_full_name(value: Any) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not name or len(name) > MAX_FULL_NAME:
        raise ApiError("invalid_provider",
                       f"full_name must be 1-{MAX_FULL_NAME} characters", 422)
    return name


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


# ------------------------------------------------------------ providers ----
def _provider_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "is_active": bool(row["is_active"]),
        "is_system": bool(row["is_system"]),
        "created_at": _iso(row["created_at"]),
    }


def ensure_system_provider() -> None:
    """The owner of the built-in YAML examples. It has no API key: only superuser keys act for it."""
    with engine.begin() as conn:
        exists = conn.execute(
            select(models.registry_providers.c.id).where(
                models.registry_providers.c.id == SYSTEM_PROVIDER_ID)
        ).first()
        if not exists:
            conn.execute(
                insert(models.registry_providers).values(
                    id=SYSTEM_PROVIDER_ID, full_name=SYSTEM_PROVIDER_NAME,
                    name_key=_name_key(SYSTEM_PROVIDER_NAME),
                    is_active=True, is_system=True, created_at=_now(),
                )
            )


def provider_by_name(full_name: Any) -> Optional[Dict[str, Any]]:
    """The provider that already owns this name (compared case/whitespace-insensitively), or None."""
    return provider_by_name_key(_name_key(clean_full_name(full_name)))


def create_provider(full_name: Any, on_exists: str = "error") -> Tuple[Dict[str, Any], bool]:
    """Create a provider and return `(provider, created)`.

    A name may only be used once: a duplicate POST is a retry of the same request, and
    answering it by minting a second provider (and a second key) that nobody asked for is
    how you end up with orphaned providers and lost secrets. `on_exists` decides what a
    name clash does:
      "error" (default) -- 409 `already_exists`, naming the id already using the name.
      "reuse"            -- return that provider with `created=False`, creating nothing.

    The check below is only a courtesy to produce a good error message. The rule itself is
    enforced by the UNIQUE index on `name_key`: checking in Python and then inserting is a
    read-then-write race, so two concurrent creates of the same name can both see the name
    as free and both insert. The insert is therefore wrapped, and a lost race (an
    IntegrityError from the index) is re-read and reported exactly like a pre-existing
    clash, so the caller always ends up with one provider.
    """
    if on_exists not in ("error", "reuse"):
        raise ApiError("invalid_request",
                       "on_exists must be 'error' or 'reuse'", 422)
    name = clean_full_name(full_name)
    key = _name_key(name)

    existing = provider_by_name_key(key)
    if existing:
        return _resolve_clash(existing, name, on_exists)

    pid = new_id("prov")
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(models.registry_providers).values(
                    id=pid, full_name=name, name_key=key,
                    is_active=True, is_system=False, created_at=_now(),
                )
            )
            row = conn.execute(select(models.registry_providers).where(
                models.registry_providers.c.id == pid)).mappings().one()
    except IntegrityError:
        # Lost the race, or the name appeared between the check and the insert. The winner
        # owns the name; report that rather than a 500.
        winner = provider_by_name_key(key)
        if not winner:
            raise
        return _resolve_clash(winner, name, on_exists)
    return _provider_dict(dict(row)), True


def provider_by_name_key(key: str) -> Optional[Dict[str, Any]]:
    """The provider holding this already-normalised name key, or None.

    Prefers the indexed `name_key` column and falls back to scanning `full_name`, so a
    database whose backfill has not run yet (or whose index creation was skipped) still
    gets the check rather than silently losing it.
    """
    with engine.connect() as conn:
        row = conn.execute(select(models.registry_providers.c.id).where(
            models.registry_providers.c.name_key == key)).first()
        if row is None:
            for candidate in conn.execute(
                select(models.registry_providers.c.id, models.registry_providers.c.full_name)
            ).mappings():
                if _name_key(candidate["full_name"] or "") == key:
                    row = (candidate["id"],)
                    break
    return get_provider(row[0]) if row else None


def _resolve_clash(existing: Dict[str, Any], name: str, on_exists: str) -> Tuple[Dict[str, Any], bool]:
    """Turn an existing provider holding `name` into the caller's answer: reuse it, or
    refuse with a 409 that names the id already using the name."""
    if on_exists == "reuse":
        return existing, False
    raise ApiError(
        "already_exists",
        f"a provider named '{name}' already exists ({existing['id']}); "
        f"pass ?on_exists=reuse to reuse it instead", 409)


def get_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    with engine.connect() as conn:
        row = conn.execute(
            select(models.registry_providers).where(
                models.registry_providers.c.id == provider_id)
        ).mappings().first()
    return _provider_dict(dict(row)) if row else None


def provider_names() -> Dict[str, str]:
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(select(models.registry_providers.c.id, models.registry_providers.c.full_name))}


def update_provider(provider_id: str, full_name: Any = None, is_active: Optional[bool] = None) -> Dict[str, Any]:
    current = get_provider(provider_id)
    if not current:
        raise ApiError("not_found", f"provider '{provider_id}' not found", 404)
    if current["is_system"]:
        raise ApiError("not_supported",
                       "the built-in system provider can't be modified", 409)
    values: Dict[str, Any] = {}
    if full_name is not None:
        name = clean_full_name(full_name)
        key = _name_key(name)
        owner = provider_by_name_key(key)
        if owner and owner["id"] != provider_id:
            raise ApiError(
                "already_exists",
                f"a provider named '{name}' already exists ({owner['id']})", 409)
        values["full_name"] = name
        values["name_key"] = key
    if is_active is not None:
        values["is_active"] = bool(is_active)
    if values:
        try:
            with engine.begin() as conn:
                conn.execute(
                    update(models.registry_providers).where(
                        models.registry_providers.c.id == provider_id).values(**values)
                )
        except IntegrityError:
            # Two renames onto the same name at once; the index is the arbiter.
            raise ApiError(
                "already_exists",
                f"a provider named '{values.get('full_name')}' already exists", 409)
    return get_provider(provider_id)  # type: ignore[return-value]


def list_providers() -> List[Dict[str, Any]]:
    """Every provider with its keys (never the secrets) and how many configs it owns."""
    with engine.connect() as conn:
        providers = [dict(r) for r in conn.execute(
            select(models.registry_providers).order_by(
                models.registry_providers.c.created_at, models.registry_providers.c.id)
        ).mappings()]
        keys = [dict(r) for r in conn.execute(
            select(models.registry_api_keys).order_by(
                models.registry_api_keys.c.created_at)
        ).mappings()]
        counts = dict(conn.execute(
            select(models.registry_configs.c.provider_id, func.count()
                   ).group_by(models.registry_configs.c.provider_id)
        ).all())
    out = []
    for p in providers:
        item = _provider_dict(p)
        item["config_count"] = counts.get(p["id"], 0)
        item["keys"] = [_key_dict(k)
                        for k in keys if k["provider_id"] == p["id"]]
        out.append(item)
    return out


# ----------------------------------------------------------------- keys ----
def _key_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "key_prefix": row["key_prefix"],
        "label": row["label"],
        "created_at": _iso(row["created_at"]),
        "revoked_at": _iso(row["revoked_at"]),
        "last_used_at": _iso(row["last_used_at"]),
    }


def issue_key(provider_id: str, label: Optional[str] = None) -> Dict[str, Any]:
    """Create a key for a provider. The returned dict is the ONLY place the secret appears."""
    provider = get_provider(provider_id)
    if not provider:
        raise ApiError("not_found", f"provider '{provider_id}' not found", 404)
    if provider["is_system"]:
        raise ApiError(
            "not_supported", "the built-in system provider acts through the superuser keys; it takes no key", 409)
    if label is not None and (not isinstance(label, str) or len(label) > 100):
        raise ApiError("invalid_label",
                       "label must be a string of at most 100 characters", 422)
    secret = secrets.token_urlsafe(32)
    key_id = new_id("key")
    with engine.begin() as conn:
        conn.execute(
            insert(models.registry_api_keys).values(
                id=key_id, provider_id=provider_id, key_hash=hash_key(secret),
                key_prefix=secret[:8], label=label, created_at=_now(),
            )
        )
        row = conn.execute(select(models.registry_api_keys).where(
            models.registry_api_keys.c.id == key_id)).mappings().one()
    return {**_key_dict(dict(row)), "api_key": secret}


def revoke_key(provider_id: str, key_id: str) -> Dict[str, Any]:
    with engine.begin() as conn:
        row = conn.execute(
            select(models.registry_api_keys).where(
                models.registry_api_keys.c.id == key_id, models.registry_api_keys.c.provider_id == provider_id
            )
        ).mappings().first()
        if not row:
            raise ApiError(
                "not_found", f"key '{key_id}' not found for provider '{provider_id}'", 404)
        if row["revoked_at"] is None:
            conn.execute(
                update(models.registry_api_keys).where(
                    models.registry_api_keys.c.id == key_id).values(revoked_at=_now())
            )
        row = conn.execute(select(models.registry_api_keys).where(
            models.registry_api_keys.c.id == key_id)).mappings().one()
    return _key_dict(dict(row))


def authenticate(key: str) -> Optional[Dict[str, Any]]:
    """The active provider that owns this key, or None. Looks the key up by its hash."""
    k, p = models.registry_api_keys, models.registry_providers
    with engine.connect() as conn:
        row = conn.execute(
            select(k.c.id, k.c.last_used_at, p.c.id.label(
                "provider_id"), p.c.full_name)
            .join(p, p.c.id == k.c.provider_id)
            .where(k.c.key_hash == hash_key(key), k.c.revoked_at.is_(None), p.c.is_active.is_(True))
        ).mappings().first()
    if not row:
        return None
    last = row["last_used_at"]
    if last is not None and last.tzinfo is None:
        # SQLite hands back naive datetimes
        last = last.replace(tzinfo=timezone.utc)
    if last is None or _now() - last > _LAST_USED_REFRESH:
        try:  # bookkeeping only: never fail (or block) a request over it
            with engine.begin() as conn:
                conn.execute(update(k).where(
                    k.c.id == row["id"]).values(last_used_at=_now()))
        except Exception:  # pragma: no cover - e.g. a locked SQLite file
            logger.debug("could not update last_used_at", exc_info=True)
    return {"provider_id": row["provider_id"], "full_name": row["full_name"], "key_id": row["id"]}


# -------------------------------------------------------------- configs ----
def list_config_rows(source: Optional[str] = None) -> List[Dict[str, Any]]:
    c = models.registry_configs
    stmt = select(c).order_by(c.c.created_at, c.c.id)
    if source:
        stmt = stmt.where(c.c.source == source)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def insert_config_row(row: Dict[str, Any]) -> None:
    with engine.begin() as conn:
        conn.execute(insert(models.registry_configs).values(**row))


def update_config_row(config_id: str, **values: Any) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(models.registry_configs).where(
                models.registry_configs.c.id == config_id)
            .values(updated_at=_now(), **values)
        )


def delete_config_row(config_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(delete(models.registry_configs).where(
            models.registry_configs.c.id == config_id))


def sync_yaml_rows(rows: List[Dict[str, Any]]) -> None:
    """Make the registry's "yaml" rows mirror the YAML files exactly: the files are the
    source of truth for those configs. `is_only_me` is preserved across restarts."""
    c = models.registry_configs
    wanted = {r["id"] for r in rows}
    with engine.begin() as conn:
        existing = {r[0] for r in conn.execute(
            select(c.c.id).where(c.c.source == "yaml"))}
        for gone in existing - wanted:
            conn.execute(delete(c).where(c.c.id == gone))
        for r in rows:
            if r["id"] in existing:
                conn.execute(update(c).where(c.c.id == r["id"]).values(
                    config_json=r["config_json"], physical_name=r["physical_name"], name=r["name"], updated_at=_now(
                    )
                ))
            else:
                conn.execute(insert(c).values(**r))
