# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
"""Authentication entry point: ``ensure_credentials(service)``.

Usage::

    from jarvis.auth import ensure_credentials

    creds = ensure_credentials("teams")
    graph_token = creds.get("graph_token")

Resolution order:

    1. **Cache** -- return immediately if all required artifacts are fresh
    2. **Refresh** -- use the profile's refresh_strategy (OAuth2, cookie exchange, etc.)
    3. **Browser** -- launch a browser, complete SSO, harvest tokens

Results are cached automatically in the SQLite token cache.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any

# Use the Windows certificate store (via truststore) so that requests/urllib3
# trust Bosch-internal CAs without needing verify=False in every skill.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from .http.cookies import CookieJar
from .jwt_utils import token_expiry
from .profiles import (
    CookieExchangeStrategy,
    OAuthRefreshStrategy,
    SapisidhashStrategy,
    ServiceProfile,
    get_profile,
    refresh_token_expiry,
)

logger = logging.getLogger(__name__)

# Proactive skew: a cached token within this many seconds of its stored expiry is treated
# as already gone, so a refresh or browser re-auth runs before a request would fail. Read
# uniformly from the stored expiry for every token type - a JWT access token AND an opaque
# SPA refresh token stamped with its 24h session lifetime.
_FRESH_SKEW_SECONDS = 60

try:
    from .store import cache
except ImportError:
    cache = None  # type: ignore[assignment]


# ── Public data class ───────────────────────────────────────────────────────


@dataclass
class ServiceCredentials:
    """Container for a complete set of credentials for a service.

    Artifacts are the named credential values defined in the service profile
    (e.g. "graph_token", "access_token", "session_cookie").

    Use ``.get(name)`` for safe access, ``[name]`` for required access.
    """

    service: str
    artifacts: dict[str, str] = field(default_factory=dict)
    # Domain-aware jar keyed on (domain, name). Consumers MUST pick the subset
    # for their target host via cookies.for_host(host) - blindly sending the
    # whole SSO jar overflows gateway header limits and leaks credentials to
    # unrelated hosts, and a flat dict would collapse same-named cookies
    # across domains (google.com vs google.de).
    cookies: CookieJar = field(default_factory=CookieJar)
    source: str = ""  # "cache", "refresh", "browser"

    def get(self, name: str) -> str | None:
        """Get an artifact by name, or None if missing."""
        return self.artifacts.get(name)

    def __getitem__(self, name: str) -> str:
        value = self.artifacts.get(name)
        if value is None:
            raise KeyError(f"Artifact '{name}' not found for service '{self.service}'")
        return value

    def __contains__(self, name: str) -> bool:
        return name in self.artifacts

    def summary(self) -> str:
        names = ", ".join(sorted(self.artifacts.keys()))
        return f"[{self.service}] {len(self.artifacts)} artifacts ({names}) via {self.source}"


# ── Cache helpers ───────────────────────────────────────────────────────────


def _load_cookie_jar(service: str) -> CookieJar:
    """Build the service's browser-fidelity cookie jar from the token cache.

    The cache keys every cookie by ``(service, type, domain, path, name)`` -
    the browser's own identity - so this is lossless: same-named cookies from
    different domains (or paths) stay distinct. Browser auth persists every
    harvested cookie with its domain+path (``browser_auth._store_token``), so
    the cache, refresh, and browser paths all read the same source.
    """
    if not cache:
        return CookieJar()
    rows = cache.list_tokens(service, type="cookie") or []
    entries = []
    for row in rows:
        name = row.get("name", "")
        domain = row.get("domain") or ""
        stored_path = row.get("path", "")
        value = cache.get(service, "cookie", name, domain, stored_path)
        if name and value:
            entries.append((domain, name, value, stored_path or "/"))
    return CookieJar(entries)


def _find_cached_cookie(service: str, cookie_name: str, domain: str | None = None) -> str | None:
    """Find a cookie in the cache by name, optionally scoped to a domain.

    Goes through the same ``CookieJar.resolve()`` semantics as the browser and
    artifact paths - exact domain match first, then the most specific
    subdomain - so a same-named cookie on ``accounts.google.com`` can never
    shadow the ``google.com`` value a rule asked for. First-match-by-storage-
    order is NOT acceptable here: the cookie feeds the cookie-to-token
    exchange, and the wrong domain's value 401s it.
    """
    entry = _load_cookie_jar(service).resolve(cookie_name, domain=domain)
    return entry.value if entry else None


def _load_cached_artifacts(
    profile: ServiceProfile,
    required_artifact: str | None = None,
) -> dict[str, str] | None:
    """Load all required artifacts from cache.

    Returns None if any *required* artifact is missing or expired.
    """
    if not cache:
        return None

    required_names = {r.name for r in profile.artifacts if r.required}
    if required_artifact:
        required_names.add(required_artifact)

    artifacts: dict[str, str] = {}
    for rule in profile.artifacts:
        value = cache.get(
            profile.service, "artifact", rule.name, min_remaining_seconds=_FRESH_SKEW_SECONDS
        )
        if value:
            artifacts[rule.name] = value
        elif rule.name in required_names:
            return None

    if not artifacts:
        return None

    return artifacts


# ── Refresh logic ───────────────────────────────────────────────────────────


def _oauth_strategies(profile: ServiceProfile) -> list:
    """All OAuth2 refresh strategies for a profile: primary + extras."""
    candidates = [profile.refresh_strategy, *getattr(profile, "extra_refresh_strategies", ())]
    return [s for s in candidates if isinstance(s, OAuthRefreshStrategy)]


def _can_silently_refresh(profile: ServiceProfile, artifact_name: str | None) -> bool:
    """True if an OAuth2 refresh strategy can produce *artifact_name* (or, when
    None, any artifact). Used to decide whether a forced re-auth can try a
    browserless refresh before launching the browser."""
    oauth = _oauth_strategies(profile)
    if not oauth:
        return False
    if artifact_name is None:
        return True
    return any(s.artifact_name == artifact_name for s in oauth)


def _load_fresh_artifacts(profile: ServiceProfile) -> dict[str, str]:
    """All currently-fresh cached artifacts (never fails; skips any token past its
    stored expiry within the freshness skew, regardless of token type).

    Unlike :func:`_load_cached_artifacts` this has no notion of "required" and
    never returns None -- it's used to PRESERVE the tokens that are still valid
    when refreshing only one of a multi-token service, so the refreshed result
    stays complete (a partial result would blank the other tokens for callers
    that rebuild their whole token set, e.g. Teams)."""
    if not cache:
        return {}
    out: dict[str, str] = {}
    for rule in profile.artifacts:
        value = cache.get(
            profile.service, "artifact", rule.name, min_remaining_seconds=_FRESH_SKEW_SECONDS
        )
        if not value:
            continue
        out[rule.name] = value
    return out


def _try_refresh(
    profile: ServiceProfile,
    cached_artifacts: dict[str, str] | None,
    required_artifact: str | None = None,
) -> dict[str, str] | None:
    """Try to refresh tokens using the profile's refresh strategy/strategies.

    For OAuth2 (possibly several strategies for one refresh token, one per
    audience), refresh the strategy whose ``artifact_name`` matches
    *required_artifact*; if none is specifically needed, use the primary.
    """
    oauth = _oauth_strategies(profile)
    if oauth:
        chosen = None
        if required_artifact:
            chosen = next((s for s in oauth if s.artifact_name == required_artifact), None)
        chosen = chosen or oauth[0]
        result = _try_oauth2_refresh(profile, chosen, cached_artifacts)
        if result and (required_artifact is None or required_artifact in result):
            return result
        # Fall back to any other OAuth strategy that yields the needed artifact.
        if required_artifact:
            for s in oauth:
                if s is chosen:
                    continue
                result = _try_oauth2_refresh(profile, s, cached_artifacts)
                if result and required_artifact in result:
                    return result
        return result

    strategy = profile.refresh_strategy
    if isinstance(strategy, CookieExchangeStrategy):
        return _try_cookie_exchange(profile, strategy, cached_artifacts)
    if isinstance(strategy, SapisidhashStrategy):
        return _try_sapisidhash(profile, strategy, cached_artifacts)

    return None


_SHARED_FRT_SVC = "_shared-frt"


def _read_shared_frt(group: str, min_remaining_seconds: float = 0) -> dict[str, str] | None:
    """Read a group's shared refresh_token slot: {refresh_token, client_id, origin}.

    ``min_remaining_seconds`` skew is for the CONSUMPTION read (re-minting from the slot),
    so a near-expired shared token routes to re-auth rather than a doomed exchange; the
    publish/reseed reads keep the default 0 - they intentionally want to see a near-expiry
    slot to decide whether to overwrite or re-seed it."""
    if not cache:
        return None
    raw = cache.get(_SHARED_FRT_SVC, "oauth", group, min_remaining_seconds=min_remaining_seconds)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if data.get("refresh_token") and data.get("client_id"):
        return data
    return None


def _write_shared_frt(
    group: str,
    refresh_token: str,
    client_id: str,
    origin: str | None,
    source_service: str,
    expires_at: float | None = None,
) -> None:
    """Publish a refresh_token + the client it is bound to into a group's shared slot.

    ``source_service`` is the service that can re-harvest this token via a browser (the broad
    publisher, e.g. teams). It is recorded so a consumer whose re-mint hits a rotated-out
    ``invalid_grant`` can re-seed the slot from that source and retry, instead of failing.
    A consumer updating the slot (it does not own a browser-harvestable token) preserves the
    existing source rather than naming itself.
    """
    if not cache or not refresh_token or not client_id:
        return
    existing = _read_shared_frt(group)
    source = source_service
    if existing and existing.get("source") and existing.get("client_id") == client_id:
        source = existing["source"]
    cache.put(
        _SHARED_FRT_SVC,
        "oauth",
        group,
        json.dumps(
            {
                "refresh_token": refresh_token,
                "client_id": client_id,
                "origin": origin,
                "source": source,
            }
        ),
        expires_at=expires_at,  # carried SPA session expiry; None when unknown
        metadata={"source": "shared_frt"},
    )


def _reseed_shared_and_retry(
    profile: ServiceProfile,
    strategy: OAuthRefreshStrategy,
    dead_refresh_token: str,
    from_shared: bool,
    exc: Exception,
) -> tuple[dict[str, Any], OAuthRefreshStrategy] | None:
    """Self-heal a shared-group re-mint whose token AAD rotated out (``invalid_grant``).

    That token is dead for every consumer, so re-seed the group slot from its recorded source
    service (which can browser-harvest a fresh one) and retry the exchange once. Returns
    ``(oauth2 result, effective strategy)`` on success, or ``None`` when it cannot recover -
    the caller then falls through to its own browser auth. Only a shared-slot ``invalid_grant``
    is retried; any other failure just returns None so the normal fallback runs.
    """
    from .refresh import oauth2_refresh

    if not (from_shared and strategy.shared_group and "invalid_grant" in str(exc)):
        logger.warning("[%s] OAuth2 refresh failed: %s", profile.service, exc)
        return None
    shared = _read_shared_frt(strategy.shared_group)
    source = (shared or {}).get("source")
    if not source or source == profile.service:
        logger.warning(
            "[%s] shared '%s' token dead, no source to re-seed from: %s",
            profile.service,
            strategy.shared_group,
            exc,
        )
        return None
    logger.info(
        "[%s] shared '%s' token rotated out; re-seeding from '%s' and retrying",
        profile.service,
        strategy.shared_group,
        source,
    )
    try:
        ensure_credentials(source, force_browser=True)
    except Exception as reseed_exc:
        logger.warning(
            "[%s] re-seed of shared '%s' from '%s' failed: %s",
            profile.service,
            strategy.shared_group,
            source,
            reseed_exc,
        )
        return None
    fresh = _read_shared_frt(strategy.shared_group)
    if not fresh or fresh["refresh_token"] == dead_refresh_token:
        return None
    retry_strategy = replace(strategy, client_id=fresh["client_id"], origin=fresh.get("origin"))
    try:
        return oauth2_refresh(retry_strategy, fresh["refresh_token"]), retry_strategy
    except Exception as retry_exc:
        logger.warning("[%s] retry after re-seed failed: %s", profile.service, retry_exc)
        return None


def _try_oauth2_refresh(
    profile: ServiceProfile,
    strategy: OAuthRefreshStrategy,
    cached_artifacts: dict[str, str] | None,
) -> dict[str, str] | None:
    """Try OAuth2 refresh_token → access_token exchange.

    A refresh_token is bound to the client that minted it. When this service has no
    refresh_token of its own but its strategy names a ``shared_group``, re-mint from the
    group's shared token by redeeming with the STORED client (varying only the scope). Any
    refresh here is published back to the group so sibling services reuse it.
    """
    from .refresh import oauth2_refresh

    # Find a refresh_token -- own artifacts, then own cache, then the shared group slot.
    refresh_token = None
    eff_strategy = strategy
    if cached_artifacts:
        refresh_token = cached_artifacts.get("refresh_token")
    if not refresh_token and cache:
        refresh_token = cache.get(
            profile.service, "artifact", "refresh_token", min_remaining_seconds=_FRESH_SKEW_SECONDS
        ) or cache.get(
            profile.service, "oauth", "refresh_token", min_remaining_seconds=_FRESH_SKEW_SECONDS
        )
    if refresh_token and cache:
        # Redeem with the client that MINTED this token (recorded at harvest/refresh),
        # not the strategy's declared client -- a mismatched pairing is AADSTS70000.
        minted_by = None
        minted_origin = None
        for row_type in ("artifact", "oauth"):
            meta = cache.get_metadata(profile.service, row_type, "refresh_token")
            if meta and meta.get("client_id"):
                minted_by = meta["client_id"]
                minted_origin = meta.get("origin")
                break
        if minted_by and minted_by != strategy.client_id:
            logger.info(
                "[%s] refresh_token was minted by client %s (strategy declares %s) - "
                "redeeming with the minting client and its origin %s",
                profile.service,
                minted_by,
                strategy.client_id,
                minted_origin or "<none recorded>",
            )
            # The declared origin belongs to the declared client; pairing it with a
            # different client is rejected, so the minting client's own origin travels
            # with it (and none is sent when the harvest recorded none).
            eff_strategy = replace(strategy, client_id=minted_by, origin=minted_origin)
    from_shared = False
    if not refresh_token and strategy.shared_group:
        shared = _read_shared_frt(strategy.shared_group, min_remaining_seconds=_FRESH_SKEW_SECONDS)
        if shared:
            refresh_token = shared["refresh_token"]
            eff_strategy = replace(
                strategy, client_id=shared["client_id"], origin=shared.get("origin")
            )
            from_shared = True
            logger.info(
                "[%s] re-minting via shared '%s' refresh_token (client %s)",
                profile.service,
                strategy.shared_group,
                shared["client_id"],
            )

    if not refresh_token:
        logger.debug("[%s] No refresh_token available for OAuth2 refresh", profile.service)
        return None

    # Carry the source refresh token's session expiry forward across rotation: an SPA refresh
    # token's fixed lifetime is anchored at the interactive sign-in and cannot be reset by a
    # silent rotation, so a rotated token inherits its predecessor's expiry rather than a fresh
    # now+lifetime clock.
    carried_expiry: float | None = None
    if cache:
        if from_shared and strategy.shared_group:
            carried_expiry = cache.get_expiry(_SHARED_FRT_SVC, "oauth", strategy.shared_group)
        else:
            carried_expiry = cache.get_expiry(
                profile.service, "artifact", "refresh_token"
            ) or cache.get_expiry(profile.service, "oauth", "refresh_token")
    rt_session_expiry = refresh_token_expiry(eff_strategy, carried=carried_expiry)

    try:
        result = oauth2_refresh(eff_strategy, refresh_token)
    except Exception as exc:
        healed = _reseed_shared_and_retry(profile, strategy, refresh_token, from_shared, exc)
        if healed is None:
            return None
        result, eff_strategy = healed

    # Build artifacts from response
    artifacts: dict[str, str] = {}
    new_access = result.get("access_token")
    new_refresh = result.get("refresh_token")

    target_name = strategy.artifact_name
    if new_access:
        artifacts[target_name] = new_access
        _cache_artifact(
            profile.service,
            target_name,
            new_access,
            source="oauth2_refresh",
            expires_in=result.get("expires_in"),
        )

    if new_refresh:
        artifacts["refresh_token"] = new_refresh
        # A consumer redeeming the shared slot stays on the shared rotation chain and does NOT
        # keep its own copy, so a single lineage is maintained; only genuine owners persist.
        if cache and not from_shared:
            cache.put(
                profile.service,
                "artifact",
                "refresh_token",
                new_refresh,
                expires_at=rt_session_expiry,  # carried SPA session expiry; None when unknown
                # Record the minting client so the pairing survives rotation.
                metadata={"source": "oauth2_refresh", "client_id": eff_strategy.client_id},
            )

    # Publish to the shared group slot so sibling services can re-mint from it, recording the
    # client it is bound to and the source that can re-harvest it. Only the broad publisher
    # writes: a service redeeming the shared token keeps that chain current (from_shared), an
    # empty slot is seeded once, and a service refreshing its OWN narrow-client token must NOT
    # clobber a broader publisher's token (its client may not be authorized for every resource
    # a consumer needs - e.g. the outlook client cannot mint SharePoint).
    if strategy.shared_group:
        existing = _read_shared_frt(strategy.shared_group)
        may_publish = (
            from_shared or not existing or existing.get("client_id") == eff_strategy.client_id
        )
        if may_publish:
            _write_shared_frt(
                strategy.shared_group,
                new_refresh or refresh_token,
                eff_strategy.client_id,
                eff_strategy.origin,
                profile.service,
                expires_at=rt_session_expiry,
            )

    # Preserve other cached artifacts that are still valid
    if cached_artifacts:
        for key, value in cached_artifacts.items():
            if key not in artifacts:
                artifacts[key] = value

    return artifacts if target_name in artifacts else None


def _try_cookie_exchange(
    profile: ServiceProfile,
    strategy: CookieExchangeStrategy,
    cached_artifacts: dict[str, str] | None,
) -> dict[str, str] | None:
    """Try cookie → JWT/Bearer exchange.

    Cookie exchange requires valid session cookies, which come from
    the browser auth step. If we have cached cookies, we can try the
    exchange without launching a browser.
    """
    from .refresh import cookie_exchange

    # Load cached cookies
    cookies: dict[str, str] = {}
    if cache:
        for rule in profile.artifacts:
            if rule.source == "cookie":
                cached = _find_cached_cookie(
                    profile.service, rule.key, getattr(rule, "domain", None)
                )
                if cached:
                    cookies[rule.key] = cached

    if not cookies:
        return None

    try:
        token = cookie_exchange(strategy, cookies)
    except Exception as exc:
        logger.warning("[%s] Cookie exchange failed: %s", profile.service, exc)
        return None

    # Build result artifacts -- include the exchange result + cookies
    artifacts: dict[str, str] = {}
    if cached_artifacts:
        artifacts.update(cached_artifacts)

    # Store the exchanged token under the header name as artifact
    artifact_name = strategy.token_header_name.lower().replace("-", "_")
    artifacts[artifact_name] = token
    _cache_artifact(profile.service, artifact_name, token, source="cookie_exchange")

    return artifacts


def _try_sapisidhash(
    profile: ServiceProfile,
    strategy: SapisidhashStrategy,
    cached_artifacts: dict[str, str] | None,
) -> dict[str, str] | None:
    """Compute SAPISIDHASH from cached SAPISID cookie."""
    from .refresh import compute_sapisidhash

    sapisid = None
    if cache:
        sapisid = _find_cached_cookie(profile.service, strategy.cookie_name)
    if cached_artifacts and not sapisid:
        sapisid = cached_artifacts.get("sapisid_cookie")

    if not sapisid:
        return None

    try:
        auth_header = compute_sapisidhash(strategy, {strategy.cookie_name: sapisid})
    except Exception as exc:
        logger.warning("[%s] SAPISIDHASH computation failed: %s", profile.service, exc)
        return None

    artifacts = dict(cached_artifacts or {})
    artifacts["authorization_header"] = auth_header
    return artifacts


def _cache_artifact(
    service: str,
    name: str,
    value: str,
    *,
    source: str = "",
    expires_in: object = None,
) -> None:
    """Store an artifact in cache, resolving its expiry from the JWT exp
    claim or the OAuth ``expires_in``."""
    if not cache:
        return
    cache.put(
        service,
        "artifact",
        name,
        value,
        expires_at=token_expiry(value, expires_in=expires_in),
        metadata={"source": source},
    )


# ── Public API ──────────────────────────────────────────────────────────────


def invalidate(service: str) -> None:
    """Delete all cached artifacts for a service, forcing re-authentication."""
    if cache:
        deleted = cache.delete(service)
        logger.info("[%s] Invalidated cache (%d entries)", service, deleted)


def _resolve_browser_headless_mode(headless: bool | str) -> bool | str:
    """Apply the global browser auth override used by smoke tests."""
    override = os.environ.get("BROWSER_AUTH_HEADLESS")
    if override is None:
        return headless

    normalized = override.strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized == "auto":
        return "auto"

    logger.warning(
        "Ignoring invalid BROWSER_AUTH_HEADLESS=%r; expected true, false, or auto",
        override,
    )
    return headless


def ensure_credentials(
    service: str,
    *,
    required_artifact: str | None = None,
    headless: bool | str = "auto",
    profile: ServiceProfile | None = None,
    force_browser: bool = False,
) -> ServiceCredentials:
    """Get valid credentials for a service.

    Resolution order:

    1. **Cache** -- return immediately if all required artifacts are fresh.
    2. **Refresh** -- use the profile's ``refresh_strategy``
       (OAuth2 token exchange, cookie exchange, SAPISIDHASH computation).
    3. **Browser** -- launch a headless Chrome browser, complete SSO,
       harvest cookies/tokens/localStorage.

    If *force_browser* is True, steps 1 and 2 are skipped and the cache
    is invalidated before launching the browser.  Use this when cached
    artifacts are known to be stale (e.g. after a 401 response).

    Args:
        service: Service name matching a profile in ``profiles.py``
            (e.g. ``"teams"``, ``"powerbi"``, ``"jira"``).
        required_artifact: Name of a specific artifact that MUST be
            present in the result.  If the cache has all other artifacts
            but this one is missing, a refresh/browser flow is triggered.
        headless: Browser mode if browser auth is needed.
            ``"auto"`` tries headless first, falls back to headed.
        profile: Override the default profile for this service.
            Useful for dynamic services like LeanIX where the profile
            is configured at runtime.
        force_browser: Skip cache/refresh and go straight to browser
            auth.  Also invalidates the cache before re-authenticating.

    Returns:
        :class:`ServiceCredentials` with ``.get(name)`` access to
        individual artifacts.

    Raises:
        KeyError: If no profile is registered for *service*.
        TimeoutError: If browser auth does not complete in time.
        RuntimeError: If a required artifact cannot be obtained.
    """
    active_profile = profile or get_profile(service)
    resolved_headless = _resolve_browser_headless_mode(headless)

    if force_browser:
        # A forced re-auth (typically after a 401) usually just means an expired
        # JWT -- which a refresh_token renews WITHOUT a browser. Try that first;
        # the browser SSO session may itself be unavailable (expired/MFA/locked),
        # and a silent refresh is cheaper and more reliable when it applies.
        # Only skip straight to the browser for artifacts no strategy can produce
        # (e.g. an opaque, exp-less cookie like the Teams chatsvc token).
        if _can_silently_refresh(active_profile, required_artifact):
            # Preserve the tokens still valid in cache so the refreshed result is
            # complete (callers like Teams rebuild their whole token set from it).
            refreshed = _try_refresh(
                active_profile, _load_fresh_artifacts(active_profile), required_artifact
            )
            if refreshed and (required_artifact is None or required_artifact in refreshed):
                logger.info("[%s] Silently refreshed on forced re-auth (no browser)", service)
                return ServiceCredentials(
                    service=service,
                    artifacts=refreshed,
                    cookies=_load_cookie_jar(service),
                    source="refresh",
                )
        invalidate(service)
    else:
        # Step 1: Cache
        cached = _load_cached_artifacts(active_profile, required_artifact)
        if cached and (required_artifact is None or required_artifact in cached):
            logger.info("[%s] All artifacts fresh from cache", service)
            return ServiceCredentials(
                service=service,
                artifacts=cached,
                cookies=_load_cookie_jar(service),
                source="cache",
            )

        # Step 2: Refresh. Preserve the tokens still valid in cache (cached is
        # None when any *required* token is stale, which would otherwise blank
        # the still-good ones for multi-token callers that rebuild their set).
        refreshed = _try_refresh(
            active_profile, cached or _load_fresh_artifacts(active_profile), required_artifact
        )
        if refreshed and (required_artifact is None or required_artifact in refreshed):
            logger.info("[%s] Artifacts refreshed successfully", service)
            return ServiceCredentials(
                service=service,
                artifacts=refreshed,
                cookies=_load_cookie_jar(service),
                source="refresh",
            )

    # Step 3: Browser auth fallback
    from .browser import ensure_service_auth

    logger.info("[%s] Falling back to browser auth", service)
    auth_result = ensure_service_auth(
        service,
        profile=active_profile,
        required_artifact=required_artifact,
        headless=resolved_headless,
    )

    # If profile has a cookie exchange strategy, do the exchange now
    if isinstance(active_profile.refresh_strategy, CookieExchangeStrategy) and auth_result.cookies:
        from .refresh import cookie_exchange

        # Only pass cookies declared in the profile's artifact rules -- the
        # full browser session harvests 50-100+ cookies from SSO domains that
        # would bloat the Cookie header past the server's 8KB limit.
        exchange_cookies = {}
        for rule in active_profile.artifacts:
            if rule.source != "cookie":
                continue
            entry = auth_result.cookies.resolve(rule.key, domain=getattr(rule, "domain", None))
            if entry:
                exchange_cookies[rule.key] = entry.value

        try:
            token = cookie_exchange(active_profile.refresh_strategy, exchange_cookies)
            artifact_name = active_profile.refresh_strategy.token_header_name.lower().replace(
                "-", "_"
            )
            auth_result.artifacts[artifact_name] = token
            _cache_artifact(service, artifact_name, token, source="cookie_exchange")
        except Exception as exc:
            logger.warning("[%s] Post-auth cookie exchange failed: %s", service, exc)

    # If the required artifact is still missing, mint it from the refresh
    # strategies now. The browser cannot harvest every artifact (e.g. the
    # Teams mt_token sits ENCRYPTED in the MSAL cache), but the refresh token
    # just captured can - _try_refresh picks the strategy whose artifact_name
    # matches (several audiences can hang behind one refresh token).
    if required_artifact and required_artifact not in auth_result.artifacts:
        try:
            refreshed = _try_refresh(active_profile, dict(auth_result.artifacts), required_artifact)
        except Exception as exc:
            refreshed = None
            logger.warning("[%s] Post-browser refresh failed: %s", service, exc)
        if refreshed:
            auth_result.artifacts.update(refreshed)
            if required_artifact in refreshed:
                logger.info("[%s] Minted %s via post-browser refresh", service, required_artifact)

    # The caller's requirement is enforced HERE, after every recovery step had
    # its chance (browser harvest, cookie exchange, post-browser refresh).
    if required_artifact and required_artifact not in auth_result.artifacts:
        raise RuntimeError(
            f"Missing required artifact: {service}.{required_artifact} "
            "(still absent after browser auth, cookie exchange, and token refresh)"
        )

    return ServiceCredentials(
        service=service,
        artifacts=auth_result.artifacts,
        # Browser harvest persists every cookie to the cache WITH its
        # (domain, path) (browser_auth._store_token); the jar is read back
        # from there so the returned credentials carry the full identity.
        cookies=_load_cookie_jar(service),
        source="browser",
    )
