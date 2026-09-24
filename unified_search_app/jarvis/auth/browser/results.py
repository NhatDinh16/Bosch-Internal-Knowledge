# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""Result model for a browser auth session and the artifact-resolution rules.

``AuthResult`` holds everything harvested from a session (cookies, OAuth tokens,
localStorage); ``ServiceAuthResult`` adds the named artifacts a profile declares.
``normalize_artifacts`` turns a raw result into the profile's artifacts via the
per-source ``_resolve_artifact`` rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..http.cookies import CookieJar
from ..jwt_utils import decode_jwt_payload
from ..jwt_utils import jwt_expiry as _jwt_expiry
from ..profiles import ServiceProfile


@dataclass
class AuthResult:
    """Container for all tokens harvested from a browser auth session.

    Cookies are a :class:`CookieJar` keyed on the full ``(domain, path, name)``
    identity - the same model the cache and consumers use - so no step of the
    pipeline collapses same-named cookies across domains/paths. A plain
    ``dict`` passed for ``cookies`` (test doubles, ad-hoc callers) is coerced
    to a jar with unknown provenance.
    """

    cookies: CookieJar = field(default_factory=CookieJar)
    oauth_tokens: dict[str, str] = field(default_factory=dict)
    oauth_expiry: dict[str, float | None] = field(default_factory=dict)
    oauth_minting_client: dict[str, str] = field(
        default_factory=dict
    )  # token key -> minting client
    oauth_from_request: bool = False  # True when access_token was captured from an outgoing request
    ls_tokens: dict[str, str] = field(default_factory=dict)
    response_meta: dict[str, str] = field(default_factory=dict)
    service: str = ""
    page_result: Any = None  # caller-defined return value of an on_page hook, if any

    def __post_init__(self) -> None:
        if isinstance(self.cookies, dict):
            self.cookies = CookieJar.from_flat(self.cookies)

    def get_cookie(self, name: str) -> str | None:
        return self.cookies.get(name)

    def get_oauth(self, key: str) -> str | None:
        return self.oauth_tokens.get(key)

    def get_ls(self, key: str) -> str | None:
        return self.ls_tokens.get(key)

    def get_meta(self, key: str) -> str | None:
        return self.response_meta.get(key)

    @property
    def has_cookies(self) -> bool:
        return bool(self.cookies)

    @property
    def has_oauth(self) -> bool:
        return bool(self.oauth_tokens)

    @property
    def has_ls(self) -> bool:
        return bool(self.ls_tokens)

    def summary(self) -> str:
        parts = []
        if self.cookies:
            parts.append(f"{len(self.cookies)} cookies")  # CookieJar.__len__
        if self.oauth_tokens:
            keys = ", ".join(sorted(self.oauth_tokens.keys()))
            parts.append(f"OAuth2 ({keys})")
        if self.ls_tokens:
            parts.append(f"{len(self.ls_tokens)} localStorage tokens")
        if self.response_meta:
            parts.append(f"{len(self.response_meta)} meta")
        return f"[{self.service}] " + (", ".join(parts) if parts else "no tokens")


@dataclass
class ServiceAuthResult(AuthResult):
    artifacts: dict[str, str] = field(default_factory=dict)
    source: str = "browser"

    def get_artifact(self, name: str) -> str | None:
        return self.artifacts.get(name)


def _resolve_artifact(rule, raw: AuthResult) -> tuple[str | None, str | None]:
    if rule.source == "cookie":
        # The jar carries the full (domain, path, name) identity; resolve the
        # cookie for this rule (rule.domain, if set, is the parent to match on
        # or below). A domain-filtered rule with no match fails strictly.
        entry = raw.cookies.resolve(rule.key, domain=getattr(rule, "domain", None))
        return (entry.value if entry else None), rule.key

    if rule.source == "oauth":
        return raw.oauth_tokens.get(rule.key), rule.key

    if rule.source == "ls":
        return raw.ls_tokens.get(rule.key), rule.key

    if rule.source == "ls_key_contains":
        for ls_key, token_value in raw.ls_tokens.items():
            if rule.key in ls_key:
                return token_value, ls_key
        return None, None

    if rule.source in {"ls_aud_equals", "ls_aud_contains"}:
        for ls_key, token_value in raw.ls_tokens.items():
            payload = decode_jwt_payload(token_value)
            audience = payload.get("aud", "") if payload else ""
            audiences = audience if isinstance(audience, list) else [audience]
            if rule.source == "ls_aud_equals" and rule.key in audiences:
                return token_value, ls_key
            if rule.source == "ls_aud_contains" and any(
                rule.key in str(item) for item in audiences
            ):
                return token_value, ls_key
        return None, None

    raise RuntimeError(f"Unsupported artifact source: {rule.source}")


def normalize_artifacts(
    profile: ServiceProfile,
    raw: AuthResult,
    required_artifact: str | None = None,
) -> dict[str, str]:
    """Resolve the profile's artifact rules against a raw browser harvest.

    Only PROFILE-required rules abort here: a missing profile-optional
    artifact the CALLER asked for (*required_artifact*) must not fail the
    browser pass - ``ensure_credentials`` still runs its post-browser refresh
    strategies, which can mint artifacts the harvest cannot read (e.g. the
    Teams ``mt_token``, stored encrypted in the MSAL cache), and enforces the
    caller's requirement at the end.
    """
    artifacts: dict[str, str] = {}

    for rule in profile.artifacts:
        value, _source_key = _resolve_artifact(rule, raw)

        if value:
            artifacts[rule.name] = value
        elif rule.required:
            raise RuntimeError(f"Missing required artifact: {profile.service}.{rule.name}")

    return artifacts


def _artifact_expiry(rule, raw: AuthResult, value: str) -> float | None:
    if rule.source == "cookie":
        entry = raw.cookies.resolve(rule.key, domain=getattr(rule, "domain", None))
        return entry.expires_at if entry else None
    if rule.source == "oauth":
        return raw.oauth_expiry.get(rule.key) or _jwt_expiry(value)
    return _jwt_expiry(value)
