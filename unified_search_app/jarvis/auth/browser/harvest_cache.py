# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""The bridge between a browser auth session and the token store.

``_store_token`` notifies the optional caller-owned ``on_token`` hook and, by
default, writes a harvested value into ``jarvis.auth.store.cache``. The
``_load_*`` / ``_result_from_*`` helpers reconstruct an ``AuthResult`` /
``ServiceAuthResult`` from the cache so a warm session skips the browser entirely.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import ModuleType
from typing import Any

from ..http.cookies import CookieEntry, CookieJar
from ..profiles import ServiceProfile
from .results import AuthResult, ServiceAuthResult

logger = logging.getLogger(__name__)

cache: ModuleType | None
try:
    from ..store import cache
except ImportError:
    cache = None


def _store_token(
    service: str,
    type: str,
    name: str,
    value: str,
    *,
    domain: str = "",
    path: str = "",
    expires_at: float | None = None,
    metadata: dict | None = None,
    on_token: Callable | None = None,
    persist: bool = True,
) -> None:
    """Notify the caller, then optionally store a token in the shared cache.

    ``on_token`` is a caller-owned notification hook, independent from the
    shared cache. It always runs first, including when ``persist`` is false.
    Callback exceptions retain the historical fail-fast behavior and therefore
    prevent a subsequent cache write.
    """
    if on_token:
        on_token(service, type, name, value, expires_at)
    if persist and cache:
        cache.put(
            service,
            type,
            name,
            value,
            domain=domain,
            path=path,
            expires_at=expires_at,
            metadata=metadata,
        )


def _load_cached_session_material(service: str) -> AuthResult:
    result = AuthResult(service=service)
    if not cache or not hasattr(cache, "list_tokens"):
        return result

    try:
        entries = cache.list_tokens(service) or []
    except Exception:
        return result

    if not isinstance(entries, list):
        return result

    cookie_entries: list[CookieEntry] = []
    for entry in entries:
        typ = entry.get("type", "")
        name = entry.get("name", "")
        domain = entry.get("domain", "")
        path = entry.get("path", "")
        if not typ or not name:
            continue
        value = cache.get(service, typ, name, domain, path)
        if not value:
            continue

        expires_at = entry.get("expires_at")

        if typ == "cookie":
            cookie_entries.append(CookieEntry(domain, name, value, path or "/", expires_at))
        elif typ == "ls":
            ls_store_key = f"{domain}::{name}" if domain else name
            result.ls_tokens[ls_store_key] = value
        elif typ == "oauth":
            result.oauth_tokens[name] = value
            result.oauth_expiry[name] = expires_at

    result.cookies = CookieJar(cookie_entries)
    return result


def _result_from_cached_artifacts(
    service: str,
    profile: ServiceProfile,
    artifact_values: dict[str, dict[str, Any]],
) -> ServiceAuthResult:
    cached_raw = _load_cached_session_material(service)
    cookie_entries: list[CookieEntry] = list(cached_raw.cookies.entries())
    oauth_tokens: dict[str, str] = dict(cached_raw.oauth_tokens)
    ls_tokens: dict[str, str] = dict(cached_raw.ls_tokens)
    artifacts: dict[str, str] = {}

    for rule in profile.artifacts:
        if rule.name not in artifact_values:
            continue
        value = artifact_values[rule.name]["value"]
        metadata = artifact_values[rule.name].get("metadata") or {}
        artifacts[rule.name] = value
        if rule.source == "cookie":
            # Override/add the artifact cookie under its rule domain (parent
            # domain if declared); CookieJar dedups on identity, last wins.
            cookie_entries.append(CookieEntry(getattr(rule, "domain", "") or "", rule.key, value))
        elif rule.source == "oauth":
            oauth_tokens[rule.key] = value
        else:
            ls_key = metadata.get("source_key") or (rule.key if rule.source == "ls" else rule.name)
            ls_tokens[ls_key] = value

    return ServiceAuthResult(
        service=service,
        cookies=CookieJar(cookie_entries),
        oauth_tokens=oauth_tokens,
        oauth_expiry=cached_raw.oauth_expiry,
        ls_tokens=ls_tokens,
        artifacts=artifacts,
        source="cache",
    )


def _load_cached_artifacts(
    profile: ServiceProfile,
    required_artifact: str | None = None,
) -> dict[str, dict[str, Any]] | None:
    if not cache:
        return None

    required_names = [rule.name for rule in profile.artifacts if rule.required]
    if required_artifact and required_artifact not in required_names:
        names_to_load = [*required_names, required_artifact]
    elif required_names:
        names_to_load = required_names
    else:
        names_to_load = [rule.name for rule in profile.artifacts]

    if not names_to_load:
        return None

    # Load metadata from all artifact entries for this service
    metadata_by_name: dict[str, dict[str, Any]] = {}
    try:
        entries = cache.list_tokens(profile.service, type="artifact") or []
    except Exception:
        entries = []
    for entry in entries:
        nm = entry.get("name", "")
        meta = entry.get("metadata")
        if nm and isinstance(meta, dict):
            metadata_by_name[nm] = meta

    cached: dict[str, dict[str, Any]] = {}
    for name in names_to_load:
        value = cache.get(profile.service, "artifact", name)
        if not value:
            return None
        cached[name] = {
            "value": value,
            "metadata": metadata_by_name.get(name, {}),
        }

    return cached
