# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
"""JWT utility functions for token inspection and freshness checking.

Used by the shared auth module and by skills that need to inspect tokens.

Public API:
    decode_jwt_payload(token) → dict | None
    jwt_expiry(token) → float | None
    token_is_fresh(token, *, min_remaining_seconds=60) → bool
"""

from __future__ import annotations

import base64
import json
import time


def decode_jwt_payload(token: str) -> dict | None:
    """Decode JWT payload without signature verification."""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        p = parts[1] + "=" * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return None


def _as_epoch(exp: object) -> float | None:
    """Coerce a JWT ``exp`` claim to a Unix timestamp, or None if not numeric."""
    if isinstance(exp, bool):
        return None
    try:
        return float(exp)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def jwt_expiry(token: str) -> float | None:
    """Extract Unix expiry timestamp from a JWT, or None (also None if exp is
    present but not numeric)."""
    payload = decode_jwt_payload(token)
    if payload and "exp" in payload:
        return _as_epoch(payload["exp"])
    return None


def token_expiry(value: str, *, expires_in: object = None) -> float | None:
    """Absolute expiry for a token: its JWT exp claim, else now + ``expires_in``
    seconds (OAuth response), else None."""
    exp = jwt_expiry(value)
    if exp is not None:
        return exp
    secs = _as_epoch(expires_in)
    if secs is not None:
        return time.time() + secs
    return None


def jwt_is_expired(value: str) -> bool:
    """True when ``value`` is a decodable JWT that carries an exp claim and is not
    fresh (expired, or exp present but unusable).

    Opaque / non-JWT values and JWTs without an exp claim return False -- their
    expiry, if any, is tracked in the token cache, not here."""
    payload = decode_jwt_payload(value)
    if payload is None or "exp" not in payload:
        return False
    return not token_is_fresh(value)


def token_is_fresh(
    token: str | None,
    *,
    min_remaining_seconds: int = 60,
) -> bool:
    """Whether a JWT has at least ``min_remaining_seconds`` before expiry.

    True when the token is decodable and either has no exp claim (non-expiring)
    or exp is far enough in the future. False when None/empty, undecodable, or
    when exp is missing-as-numeric/expired.
    """
    if not token:
        return False
    payload = decode_jwt_payload(token)
    if payload is None:
        return False
    exp = payload.get("exp")
    if exp is None:
        return True
    epoch = _as_epoch(exp)
    if epoch is None:
        return False
    return epoch > time.time() + min_remaining_seconds
