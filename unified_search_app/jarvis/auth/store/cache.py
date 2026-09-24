# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""
Shared token cache backed by SQLite.

Stores OAuth2 tokens, MSAL tokens, cookies, and other auth artifacts
in a single SQLite database at ~/.config/jarvis/auth/token_cache.db.

Public API:

    get(service, type, name, domain="", path="", min_remaining_seconds=0)
        → str | None    (value if present and at least min_remaining_seconds from expiry)

    get_expiry(service, type, name, domain="", path="")
        → float | None  (the stored expiry, returned even when already past)

    put(service, type, name, value, domain="", path="", expires_at=None, metadata=None)
        → None          (upsert)

    delete(service, type=None, name=None, domain=None, path=None)
        → int           (number of rows deleted)

    delete_if_value(service, type, name, expected_value, domain="", path="")
        → bool          (delete the exact row only while its value still matches)

    list_tokens(service=None, type=None)
        → list[dict]    (all matching rows with expiry status)

Schema v2 columns - IDENTITY lives in the primary key, everything a cookie or
token merely *has* (attributes) belongs in the metadata JSON, so future
attribute needs never break the schema again:
    service     The skill name (e.g. "gemini", "teams")
    type        Token category: "cookie", "oauth", "ls", "artifact"
    domain      Cookie domain, localStorage origin, or "" for non-scoped types
    path        Cookie path ("/" for most cookies); "" for non-cookie types.
                Part of the identity: a browser keys cookies on
                (domain, path, name) - two same-named cookies with different
                paths coexist.
    name        Token name (cookie name, artifact name, oauth key, ls key)
    value       The token/cookie value (encrypted at rest)
    expires_at  Unix timestamp when the token expires (NULL = never)
    metadata    JSON blob with extra info
    updated_at  Unix timestamp of last write

Versioned via ``PRAGMA user_version``. An older-schema file is DROPPED and
recreated on first open (services simply re-authenticate) - a deliberate
pre-1.0 choice instead of migration code.

Thread-safe: uses WAL mode and short-lived connections.
"""

import json
import logging
import os
import secrets
import sqlite3
import time
from typing import Any

from jarvis.config import config_root

from . import _encryption

logger = logging.getLogger(__name__)

_DB_DIR = str(config_root() / "auth")
_DB_PATH = os.path.join(_DB_DIR, "token_cache.db")

_plaintext_migrated = False

_SCHEMA_VERSION = 2

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tokens (
    service    TEXT NOT NULL,
    type       TEXT NOT NULL,
    domain     TEXT NOT NULL DEFAULT '',
    path       TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL,
    value      TEXT NOT NULL,
    expires_at REAL,
    metadata   TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (service, type, domain, path, name)
)
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(_DB_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    _migrate_plaintext_rows(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the v2 schema; DROP an older-schema file instead of migrating.

    Pre-1.0 and no external consumers: a schema bump costs one cold
    re-authentication per service (the warm browser profiles make that mostly
    silent), which beats carrying migration code forever."""
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    if version < _SCHEMA_VERSION:
        had_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tokens'"
        ).fetchone()
        if had_table:
            conn.execute("DROP TABLE tokens")
            logger.info(
                "Token cache reset to schema v%d (was v%d) - services will "
                "re-authenticate on next use",
                _SCHEMA_VERSION,
                version,
            )
        conn.execute(_CREATE_TABLE)
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
    else:
        conn.execute(_CREATE_TABLE)


def _migrate_plaintext_rows(conn: sqlite3.Connection) -> None:
    """Re-encrypt legacy plaintext rows in place, once per process.

    Rows written before encryption existed (or under
    ``JARVIS_AUTH_ALLOW_PLAINTEXT``) lack the ``enc:1:`` prefix; as soon as
    the encryption layer is usable again no credential may stay plaintext at
    rest. Best-effort: a failure only defers the migration to the next
    process, it never blocks the cache operation itself."""
    global _plaintext_migrated
    if _plaintext_migrated:
        return
    _plaintext_migrated = True
    try:
        rows = conn.execute(
            "SELECT service, type, domain, path, name, value FROM tokens WHERE value NOT LIKE ?",
            (_encryption._PREFIX + "%",),
        ).fetchall()
        if not rows or not _encryption.available():
            return
        for service, type_, domain, path, name, value in rows:
            conn.execute(
                "UPDATE tokens SET value = ? "
                "WHERE service = ? AND type = ? AND domain = ? AND path = ? AND name = ?",
                (_encryption.encrypt_value(value), service, type_, domain, path, name),
            )
        conn.commit()
        logger.info("Re-encrypted %d legacy plaintext token-cache rows", len(rows))
    except Exception as exc:
        logger.warning("Plaintext token-cache migration skipped: %s", exc)


def get(
    service: str,
    type: str,
    name: str,
    domain: str = "",
    path: str = "",
    min_remaining_seconds: float = 0,
) -> str | None:
    """Get a token value by its full identity. Returns None if missing or expired.

    ``min_remaining_seconds`` treats a token that expires within that window as already
    gone - a proactive skew so a near-expired token routes to a refresh/re-auth instead of
    being handed out. A NULL stored expiry never expires. Applies uniformly to any token
    type, so an opaque token with a stamped lifetime (e.g. an SPA refresh token) is gated
    the same way a JWT is.

    Cookies are keyed (domain, path, name) - read the exact path from a
    ``list_tokens`` row. Non-cookie types store ``path = ""`` (the default).
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value, expires_at FROM tokens "
            "WHERE service = ? AND type = ? AND domain = ? AND path = ? AND name = ?",
            (service, type, domain, path, name),
        ).fetchone()
        if row is None:
            return None
        value, expires_at = row
        if expires_at is not None and expires_at <= time.time() + min_remaining_seconds:
            return None
        return _encryption.decrypt_value(value)
    finally:
        conn.close()


def get_expiry(
    service: str, type: str, name: str, domain: str = "", path: str = ""
) -> float | None:
    """The stored expiry (Unix ts) for a token, or None if the row or its expiry is absent.

    Returned even when already past, so a rotated token can carry its predecessor's session
    expiry forward - an SPA refresh token's lifetime is anchored at sign-in and must not be
    reset by rotation."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT expires_at FROM tokens "
            "WHERE service = ? AND type = ? AND domain = ? AND path = ? AND name = ?",
            (service, type, domain, path, name),
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


def get_metadata(
    service: str, type: str, name: str, domain: str = "", path: str = ""
) -> dict[str, Any] | None:
    """The metadata JSON stored with a token, or None. Returned even when expired."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT metadata FROM tokens "
            "WHERE service = ? AND type = ? AND domain = ? AND path = ? AND name = ?",
            (service, type, domain, path, name),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            meta = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None
        return meta if isinstance(meta, dict) else None
    finally:
        conn.close()


def put(
    service: str,
    type: str,
    name: str,
    value: str,
    domain: str = "",
    path: str = "",
    expires_at: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Store or update a token (encrypted at rest)."""
    encrypted_value = _encryption.encrypt_value(value)
    meta_json = json.dumps(metadata) if metadata is not None else None
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO tokens
                   (service, type, domain, path, name, value, expires_at, metadata, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(service, type, domain, path, name)
               DO UPDATE SET value = excluded.value,
                             expires_at = excluded.expires_at,
                             metadata = excluded.metadata,
                             updated_at = excluded.updated_at""",
            (
                service,
                type,
                domain,
                path,
                name,
                encrypted_value,
                expires_at,
                meta_json,
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete(
    service: str,
    type: str | None = None,
    name: str | None = None,
    domain: str | None = None,
    path: str | None = None,
) -> int:
    """Delete tokens. Filters narrow the deletion scope."""
    conn = _connect()
    try:
        clauses = ["service = ?"]
        params: list = [service]
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        if domain is not None:
            clauses.append("domain = ?")
            params.append(domain)
        if path is not None:
            clauses.append("path = ?")
            params.append(path)
        cur = conn.execute(
            f"DELETE FROM tokens WHERE {' AND '.join(clauses)}",
            params,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def delete_if_value(
    service: str,
    type: str,
    name: str,
    expected_value: str,
    domain: str = "",
    path: str = "",
) -> bool:
    """Delete an exact token only if its decrypted value still matches."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM tokens "
            "WHERE service = ? AND type = ? AND domain = ? AND path = ? AND name = ?",
            (service, type, domain, path, name),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False

        encrypted_value = row[0]
        value = _encryption.decrypt_value(encrypted_value)
        if value is None or not secrets.compare_digest(
            value.encode("utf-8"), expected_value.encode("utf-8")
        ):
            conn.rollback()
            return False

        cur = conn.execute(
            "DELETE FROM tokens "
            "WHERE service = ? AND type = ? AND domain = ? AND path = ? AND name = ? "
            "AND value = ?",
            (service, type, domain, path, name, encrypted_value),
        )
        conn.commit()
        return cur.rowcount == 1
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_tokens(
    service: str | None = None,
    type: str | None = None,
) -> list[dict]:
    """List tokens with expiry status."""
    conn = _connect()
    try:
        clauses = []
        params: list = []
        if service is not None:
            clauses.append("service = ?")
            params.append(service)
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT service, type, domain, path, name, expires_at, metadata, updated_at "
            f"FROM tokens{where} ORDER BY service, type, domain, path, name",
            params,
        ).fetchall()

        now = time.time()
        result = []
        for svc, typ, dom, pth, nm, exp, meta, updated in rows:
            entry: dict[str, Any] = {
                "service": svc,
                "type": typ,
                "domain": dom,
                "path": pth,
                "name": nm,
                "updated_at": updated,
            }
            if exp is not None:
                entry["expires_at"] = exp
                entry["expired"] = exp <= now
                entry["remaining_min"] = max(0, (exp - now) / 60)
            else:
                entry["expired"] = False
            if meta:
                try:
                    entry["metadata"] = json.loads(meta)
                except (json.JSONDecodeError, ValueError):
                    entry["metadata"] = meta
            result.append(entry)
        return result
    finally:
        conn.close()
