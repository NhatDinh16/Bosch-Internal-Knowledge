# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""
Shared browser-based authentication via Playwright (Edge/Chromium).

Launches a controlled browser session (Edge via Playwright) that navigates
to an SSO entry-point URL and harvests all resulting tokens:

    - Cookies set by the target domain (and SSO domains)
    - OAuth2 token responses found by scanning ALL network responses
    - MSAL / localStorage tokens extracted after page load

Bootstrap strategy (configurable per skill):

    1. **Headless first** (default): fast, invisible, works when SSO
       completes automatically via Kerberos / cached session.
    2. **Headed fallback**: if headless fails or times out, re-launch
       with a visible window so the user can complete MFA / consent.
    3. **Headed only**: skip headless entirely (``headless=False``).
       Useful for skills that always require user interaction.

Harvested tokens are stored in ``cache.py`` under the service name provided by
the caller unless ``persist_tokens=False`` is selected.

Internal API (do not import directly -- use ``auth.ensure_credentials()``):

    ensure_auth(service, url, **kwargs)
        → AuthResult   (cookies, tokens, localStorage items)

    ensure_service_auth(service, **kwargs)
        → ServiceAuthResult   (named artifacts derived from cookies/tokens)

    AuthResult
        .cookies        dict[str, str]      all cookies from the session
        .oauth_tokens   dict[str, str]      intercepted OAuth2 fields
        .ls_tokens      dict[str, str]      localStorage key → value
        .get_cookie(name) → str | None
        .get_oauth(key)   → str | None
        .get_ls(key)      → str | None

    ServiceAuthResult
        .artifacts      dict[str, str]      named service artifacts
        .get_artifact(name) → str | None

Configuration:

    headless          True | False | "auto" (default "auto")
                      "auto" = try headless, fall back to headed
                      True   = headless only (fail if interaction needed)
                      False  = headed only (always show browser)

    timeout           int   seconds to wait for SSO completion (default 120).
                      Zero performs no asynchronous polling after navigation;
                      it recognizes only capture work complete when goto returns.

    wait_for_url      str | re.Pattern | None
                      Consider auth complete when the browser navigates
                      to a URL matching this pattern.  Default: the
                      *url* argument's origin. Match a STABLE landing URL
                      (an app host/path the signed-in app stays on): a
                      completion must still hold after the settle
                      quiet-period, so a pattern the SPA routes away from
                      while idle would resume the sign-in. Query and
                      fragment are ignored when matching. For a signal that
                      is transient rather than a stable URL, use
                      wait_for_oauth_access_token instead.

    wait_for_cookies  list[str] | None
                      Cookie names that must be present before returning.

    wait_for_oauth_access_token  bool
                      Ignore URL/cookie/navigation signals and return only
                      after an accepted OAuth access token is captured.

    accept_access_token  callable(str) -> bool | None
                      Optional strict predicate applied before any captured
                      access token is retained or persisted.

    wait_for_idle     float  seconds of network quiet before harvesting
                      localStorage (default 3.0).

    harvest_ls        bool   whether to read localStorage after load
                      (default True).

    ls_origins        list[str] | None
                      Additional localStorage origins to read besides
                      the target URL's origin.  Format: "https+++host".

    extra_ls_origins  list[str] | None
                      MSAL origins to scan (teams, outlook, sharepoint).
                      Default: all known MSAL origins.

    on_token          callable(service, type, name, value, expires_at) | None
                      Caller-owned notification hook called for every
                      harvested token before any shared-cache write.

    on_page           async callable(page) -> Any | None
                      Caller-owned hook handed the live, authenticated page
                      once it has settled on the target origin (before the
                      localStorage harvest navigates away). Its return value is
                      stored on ``AuthResult.page_result`` (caller-defined). A
                      failure in the hook is logged and non-fatal to the auth.

    persist_tokens    bool   whether captured material is written to the shared
                      cache (default True). In-memory results and ``on_token``
                      notifications are retained when disabled. This controls
                      only Jarvis's encrypted cache; a persistent Playwright
                      profile can still update its own browser SSO state.

    proxy_bypass      bool   apply NO_PROXY + ProxyHandler fix for
                      corporate proxies (default True).

    sandbox           bool   keep the Chromium OS-level sandbox enabled
                      (default True). A launch that fails with it on is
                      retried once without it.

    browser_args      list[str]  extra Chrome launch arguments.
"""

from __future__ import annotations

import asyncio
import getpass
import ipaddress
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import parse_qs, urlparse

from jarvis.config import config_root

from ..browser_lock import profile_auth_lock
from ..http.cookies import CookieEntry, CookieJar
from ..http.proxy import apply_proxy_bypass as _apply_proxy_bypass
from ..http.proxy import restore_proxy_defaults as _restore_proxy_defaults
from ..jwt_utils import decode_jwt_payload
from ..jwt_utils import jwt_expiry as _jwt_expiry
from ..profiles import ServiceProfile, get_profile, refresh_token_expiry
from . import login_wall
from .harvest_cache import (
    _load_cached_artifacts,
    _result_from_cached_artifacts,
    _store_token,
)
from .results import (
    AuthResult,
    ServiceAuthResult,
    _artifact_expiry,
    _resolve_artifact,
    normalize_artifacts,
)

logger = logging.getLogger(__name__)


# Playwright is imported inside the async session to keep module import fast.

# A module-level indirection over the monotonic clock so the opt-in
# whole-operation deadline (``enforce_total_timeout``) can be driven
# deterministically in tests. Production always uses ``time.monotonic``.
_monotonic: Callable[[], float] = time.monotonic


class OAuthAccessTokenUnavailable(TimeoutError):
    """Browser navigation completed without yielding an accepted access token."""


class BrowserOperationTimeout(TimeoutError):
    """An opt-in deadline expired while a browser-auth operation was running."""


class _SessionKwargs(TypedDict):
    """The keyword arguments _run_browser_session takes besides ``headless``."""

    url: str
    service: str
    persistent_profile: str | None
    timeout: float
    wait_for_url: str | re.Pattern | None
    wait_for_cookies: list[str] | None
    wait_for_cookies_mode: str
    wait_for_idle: float
    harvest_ls: bool
    ls_origins: list[str] | None
    extra_ls_origins: list[str] | None
    on_token: Callable | None
    on_page: Callable | None
    sandbox: bool
    browser_args: list[str] | None
    oauth_audience_contains: str | None
    wait_for_oauth_access_token: bool
    accept_access_token: Callable[[str], bool] | None
    persist_tokens: bool
    enforce_total_timeout: bool


# ── Data classes ────────────────────────────────────────────────────────────


def _access_token_audience_matches(token: str, audience_contains: str | None) -> bool:
    """True if the JWT `aud` claim contains *audience_contains* (or filter is off).

    Used to capture only the access_token for the resource the skill actually
    calls when a page mints several access_tokens for different audiences.
    A token whose `aud` can't be decoded is rejected when a filter is active.
    """
    if not audience_contains:
        return True
    payload = decode_jwt_payload(token)
    if not payload:
        return False
    aud = payload.get("aud", "")
    audiences = aud if isinstance(aud, list) else [aud]
    return any(audience_contains in str(item) for item in audiences)


_MICROSOFT_OAUTH_TOKEN_PATH = re.compile(
    r"^/(?:token|[^/]+/oauth2(?:/v2\.0)?/token)/?$",
)
_CHATHUB_PATH_PREFIX = re.compile(
    r"^/m365copilot/chathub(?:/|$)",
    re.IGNORECASE | re.ASCII,
)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_IP_LITERAL = re.compile(r"[0-9a-f:.]+", re.ASCII)


def _canonical_endpoint_hostname(hostname: str) -> str | None:
    """Return a conservative ASCII representation of a parsed hostname."""
    if "%" in hostname:
        return None

    try:
        ip_literal = ipaddress.ip_address(hostname).compressed.lower()
    except ValueError:
        pass
    else:
        return ip_literal if _IP_LITERAL.fullmatch(ip_literal) else None

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None

    hostname_without_root = ascii_hostname.removesuffix(".")
    if not hostname_without_root or len(ascii_hostname) > 253:
        return None
    if any(not _DNS_LABEL.fullmatch(label) for label in hostname_without_root.split(".")):
        return None
    return ascii_hostname


def _is_safe_endpoint_path(path: str) -> bool:
    """Reject raw path forms whose normalization could change allowlist meaning."""
    return "%" not in path and "\\" not in path and not {".", ".."}.intersection(path.split("/"))


def _safe_endpoint_label(url: str) -> str:
    """Return a non-sensitive label for a captured-token endpoint URL."""
    if (
        not isinstance(url, str)
        or "\\" in url
        or any(unicodedata.category(char).startswith("C") for char in url)
    ):
        return "unknown-endpoint"

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        return "unknown-endpoint"

    if not hostname:
        return "unknown-endpoint"

    hostname = _canonical_endpoint_hostname(hostname)
    if hostname is None:
        return "unknown-endpoint"

    scheme = parsed.scheme.lower()
    uses_standard_port = port in {None, 443}
    path_is_safe = _is_safe_endpoint_path(parsed.path)
    has_no_path_params = not parsed.params
    if (
        scheme == "https"
        and hostname == "login.microsoftonline.com"
        and uses_standard_port
        and path_is_safe
        and has_no_path_params
        and _MICROSOFT_OAUTH_TOKEN_PATH.fullmatch(parsed.path)
    ):
        return "login.microsoftonline.com/token"
    if (
        scheme in {"https", "wss"}
        and hostname == "substrate.office.com"
        and uses_standard_port
        and path_is_safe
        and has_no_path_params
        and _CHATHUB_PATH_PREFIX.match(parsed.path)
    ):
        return "substrate.office.com/Chathub"
    return hostname


def _minting_context(
    request_post_data: str | None, request_headers: dict[str, Any] | None = None
) -> dict[str, str]:
    """The OAuth client identity from a token request's form body and headers.

    A refresh_token is bound to the client that minted it AND to that client's SPA
    origin, so both halves of the pairing are recorded at harvest time and replayed
    together later. Redeeming with one half swapped is AADSTS70000.
    """
    context = {}
    if request_headers:
        origin = request_headers.get("origin") or request_headers.get("Origin")
        if origin:
            context["origin"] = origin
    if not request_post_data:
        return context
    try:
        fields = parse_qs(request_post_data)
    except ValueError:
        return context
    client_id = (fields.get("client_id") or [""])[0]
    if client_id:
        context["client_id"] = client_id
    grant_type = (fields.get("grant_type") or [""])[0]
    if grant_type:
        context["grant_type"] = grant_type
    return context


def _declared_refresh_client(service: str) -> str:
    """The client_id the service's profile expects to redeem its refresh_token with.

    Empty when the service registers no profile or no OAuth refresh strategy, in which
    case no captured token can be judged foreign and every one is accepted.
    """
    try:
        strategy = get_profile(service).refresh_strategy
    except Exception:
        return ""
    return getattr(strategy, "client_id", "") or ""


def _refresh_token_session_expiry(service: str) -> float | None:
    """Fresh SPA session expiry to stamp on a refresh token captured during browser auth.

    Anchors the fixed lifetime clock now - the best available estimate for an opaque token,
    since a browser pass is normally the interactive sign-in that starts it. None when the
    service's strategy declares no known lifetime. A best-effort estimate, so an unregistered
    or malformed profile yields None rather than failing the capture."""
    try:
        strategy = get_profile(service).refresh_strategy
    except Exception:
        return None
    return refresh_token_expiry(strategy)


def _capture_oauth_response_body(
    service: str,
    response_url: str,
    body: str,
    result: AuthResult,
    *,
    on_token: Callable | None = None,
    audience_contains: str | None = None,
    accept_access_token: Callable[[str], bool] | None = None,
    persist_tokens: bool = True,
    request_post_data: str | None = None,
    request_headers: dict[str, Any] | None = None,
) -> bool:
    try:
        resp_data = json.loads(body)
    except (json.JSONDecodeError, ValueError, TypeError):
        return False

    if not isinstance(resp_data, dict):
        return False

    has_token = any(resp_data.get(key) for key in ("access_token", "refresh_token", "id_token"))
    if not has_token:
        return False

    logger.info("[%s] Found tokens in %s", service, _safe_endpoint_label(response_url))

    minting = _minting_context(request_post_data, request_headers)
    declared_client = _declared_refresh_client(service)

    captured = False
    for key in ("access_token", "refresh_token", "id_token"):
        val = resp_data.get(key)
        if not val:
            continue
        # Don't overwrite access_token if it was captured from an outgoing
        # request to the target origin (that token is authoritative).
        if key == "access_token" and result.oauth_from_request:
            continue
        # A portal page hosts widgets from other first-party apps, each minting its own
        # tokens. Only the one minted by the profile's declared client is redeemable with
        # that profile's client/origin/scope, so never let a foreign app's token displace
        # it -- the last token response on the page would otherwise win.
        if (
            key == "refresh_token"
            and declared_client
            and result.oauth_minting_client.get(key) == declared_client
            and minting.get("client_id", "") != declared_client
        ):
            logger.debug(
                "[%s] keeping refresh_token minted by %s, ignoring one from %s",
                service,
                declared_client,
                minting.get("client_id") or "<unknown client>",
            )
            continue
        # Skip access_tokens for the wrong resource when the profile pins an
        # audience (e.g. SharePoint surfaces Graph + SPO tokens; only SPO works).
        if key == "access_token" and not _access_token_audience_matches(val, audience_contains):
            continue
        if key == "access_token" and accept_access_token and not accept_access_token(val):
            continue
        result.oauth_tokens[key] = val
        result.oauth_minting_client[key] = minting.get("client_id", "")
        captured = True
        exp = None
        if key == "access_token":
            exp = _jwt_expiry(val)
            if not exp and "expires_in" in resp_data:
                exp = time.time() + int(resp_data["expires_in"])
        elif key == "refresh_token":
            exp = _refresh_token_session_expiry(service)
        result.oauth_expiry[key] = exp
        _store_token(
            service,
            "oauth",
            key,
            val,
            expires_at=exp,
            metadata={
                "scope": resp_data.get("scope", ""),
                **minting,
            },
            on_token=on_token,
            persist=persist_tokens,
        )

    for meta_key in ("resource", "cluster_url", "foci", "client_info"):
        val = resp_data.get(meta_key)
        if val and isinstance(val, str):
            result.response_meta[meta_key] = val

    return captured


def _capture_request_bearer_token(
    service: str,
    request_url: str,
    headers: dict[str, Any],
    result: AuthResult,
    *,
    target_origin: str | None = None,
    on_token: Callable | None = None,
    audience_contains: str | None = None,
    accept_access_token: Callable[[str], bool] | None = None,
    persist_tokens: bool = True,
) -> bool:
    if not isinstance(headers, dict):
        return False

    authorization = headers.get("Authorization") or headers.get("authorization")
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return False

    token = authorization[len("Bearer ") :].strip()
    if len(token) < 20 or token.startswith("<"):
        return False

    if result.oauth_tokens.get("access_token") == token:
        return False  # same token already captured

    # Skip bearer tokens for the wrong resource when the profile pins an audience.
    if not _access_token_audience_matches(token, audience_contains):
        return False
    if accept_access_token and not accept_access_token(token):
        return False

    # Only capture from requests to the target origin (or if no target specified)
    is_target = not target_origin or request_url.startswith(target_origin)
    if not is_target and result.oauth_tokens.get("access_token"):
        return False  # don't overwrite with off-target request tokens

    result.oauth_tokens["access_token"] = token
    if is_target:
        result.oauth_from_request = True
    exp = _jwt_expiry(token)
    result.oauth_expiry["access_token"] = exp
    endpoint_label = _safe_endpoint_label(request_url)
    _store_token(
        service,
        "oauth",
        "access_token",
        token,
        expires_at=exp,
        metadata={"source": "authorization_header", "url": endpoint_label},
        on_token=on_token,
        persist=persist_tokens,
    )
    logger.info("[%s] Found bearer access token in request headers for %s", service, endpoint_label)
    return True


def ensure_service_auth(
    service: str,
    *,
    profile: ServiceProfile | None = None,
    required_artifact: str | None = None,
    persist_tokens: bool = True,
    **kwargs,
) -> ServiceAuthResult:
    active_profile = profile or get_profile(service)
    cached_artifacts = _load_cached_artifacts(active_profile, required_artifact)
    if cached_artifacts:
        return _result_from_cached_artifacts(
            active_profile.service,
            active_profile,
            cached_artifacts,
        )

    headless_mode = kwargs.pop("headless", "auto")

    def _auth_and_normalize(headless_override=None):
        auth_kwargs = dict(kwargs)
        if headless_override is not None:
            auth_kwargs["headless"] = headless_override
        elif headless_mode != "auto":
            auth_kwargs["headless"] = headless_mode

        raw = ensure_auth(
            service=active_profile.service,
            url=active_profile.start_url,
            persistent_profile=active_profile.persistent_profile,
            wait_for_url=active_profile.wait_for_url,
            wait_for_cookies=list(active_profile.wait_for_cookies),
            wait_for_cookies_mode=active_profile.wait_for_cookies_mode,
            wait_for_oauth_access_token=active_profile.wait_for_oauth_access_token,
            wait_for_idle=active_profile.wait_for_idle,
            harvest_ls=active_profile.harvest_ls,
            extra_ls_origins=list(active_profile.extra_ls_origins),
            browser_args=list(active_profile.browser_args),
            oauth_audience_contains=active_profile.oauth_audience_contains,
            persist_tokens=persist_tokens,
            **auth_kwargs,
        )
        artifacts = normalize_artifacts(
            active_profile,
            raw,
            required_artifact=required_artifact,
        )
        return raw, artifacts

    # Serialize browser auth on the shared persistent profile. Edge/Chromium
    # allow only ONE instance per user-data-dir, so concurrent auths (multiple
    # skills / sessions / a polling loop) otherwise collide and the browser is
    # closed mid-harvest. Ephemeral profiles (no persistent_profile) get their
    # own temp dir and need no lock. See jarvis.auth.browser_lock.
    _prof_dir = (
        _persistent_profile_dir(active_profile.persistent_profile)
        if active_profile.persistent_profile
        else None
    )
    with profile_auth_lock(_prof_dir):
        if headless_mode == "auto":
            try:
                raw, artifacts = _auth_and_normalize(headless_override=True)
            except (RuntimeError, TimeoutError, Exception) as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                logger.info(
                    "[%s] Headless auth failed (%s: %s), retrying headed...",
                    active_profile.service,
                    type(exc).__name__,
                    exc,
                )
                print(
                    f"[{active_profile.service}] Headless auth failed, opening "
                    f"browser window for interactive login...",
                    file=sys.stderr,
                )
                # Scoped to THIS profile: under the lock no peer session is
                # authing on it, so its leftover browsers are ours to reap.
                # Reaping the whole profiles root would kill other sessions'
                # in-flight auth.
                if _prof_dir is not None:
                    _kill_browsers_for_profile(str(_prof_dir))
                else:
                    _kill_stale_browsers()
                raw, artifacts = _auth_and_normalize(headless_override=False)
        else:
            raw, artifacts = _auth_and_normalize()

    for rule in active_profile.artifacts:
        if rule.name not in artifacts:
            continue
        value = artifacts[rule.name]
        _value, source_key = _resolve_artifact(rule, raw)
        expires_at = _artifact_expiry(rule, raw, value)
        if rule.name == "refresh_token" and expires_at is None:
            # An opaque SPA refresh token carries no readable expiry; anchor its fixed
            # session lifetime now (best estimate for this browser sign-in). None when
            # the strategy declares no known lifetime.
            expires_at = refresh_token_expiry(active_profile.refresh_strategy)
        _store_token(
            active_profile.service,
            "artifact",
            rule.name,
            value,
            expires_at=expires_at,
            metadata={"source_key": source_key},
            persist=persist_tokens,
        )

    return ServiceAuthResult(
        service=raw.service,
        cookies=raw.cookies,
        oauth_tokens=raw.oauth_tokens,
        oauth_expiry=raw.oauth_expiry,
        ls_tokens=raw.ls_tokens,
        response_meta=raw.response_meta,
        artifacts=artifacts,
        source="browser",
    )


# Known MSAL localStorage origins (Microsoft SSO ecosystem)
_KNOWN_LS_ORIGINS = [
    "https+++teams.microsoft.com",
    "https+++outlook.office.com",
    "https+++bosch-my.sharepoint.com",
    "https+++bosch.sharepoint.com",
]

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


# ── URL matching helper ─────────────────────────────────────────────────────


def _origin_from_url(url: str) -> str:
    """Extract origin (scheme + host) from a URL."""
    p = urlparse(url)
    return f"{p.scheme}://{p.hostname}"


def _ls_origin_dir(url: str) -> str:
    """Convert a URL to a localStorage origin directory key.

    E.g. "https://feber.bosch.tech" → "https+++feber.bosch.tech"
    """
    p = urlparse(url)
    return f"{p.scheme}+++{p.hostname}"


def _cookies_complete(
    cookie_names: set[str],
    wait_for_cookies: list[str] | None,
    wait_for_cookies_mode: str,
) -> bool:
    """Return whether observed cookie names satisfy the configured wait."""
    if not wait_for_cookies:
        return False
    wanted = set(wait_for_cookies)
    if wait_for_cookies_mode == "any":
        return bool(wanted & cookie_names)
    return wanted.issubset(cookie_names)


def _auth_completion_reached(
    *,
    current_url: str,
    start_url: str,
    target_origin: str,
    wait_for_url: str | re.Pattern | None,
    wait_for_cookies: list[str] | None,
    cookies_complete: bool,
    access_token: str | None,
    wait_for_oauth_access_token: bool,
) -> bool:
    """Evaluate browser-auth completion without accessing Playwright state."""
    if wait_for_oauth_access_token:
        return bool(access_token)

    if wait_for_url:
        # Match only scheme://host/path, never the query or fragment. An OAuth
        # authorize URL on the identity provider carries the TARGET app's host
        # inside its redirect_uri, e.g.
        #   login.microsoftonline.com/.../authorize?redirect_uri=https://teams.microsoft.com/v2/...
        # so a substring/search over the whole URL falsely reports completion
        # while the browser is still on the "Sign in to your account" page - the
        # flow then breaks out before signing in and harvests no token. This bug
        # hits every MS profile (teams/outlook/sharepoint/entra), whose
        # wait_for_url is the app host that also appears in that redirect_uri.
        url_path = current_url.split("?", 1)[0].split("#", 1)[0]
        if isinstance(wait_for_url, re.Pattern):
            if wait_for_url.search(url_path):
                return True
        elif wait_for_url in url_path:
            return True

    if wait_for_cookies and cookies_complete:
        return True

    if not wait_for_url and not wait_for_cookies:
        if current_url.startswith(target_origin) and current_url.rstrip("/") != start_url.rstrip(
            "/"
        ):
            return True
        if access_token:
            return True

    return False


# ── Token caching ───────────────────────────────────────────────────────────


def _shared_browser_profiles_root() -> Path:
    return config_root() / "auth" / "browser-profiles"


# Chromium names the profile subfolder inside --user-data-dir "Default" unless told
# otherwise, and Edge derives its OS-wide PWA launcher filenames from that name
# (msedge-<app-id>-<profile-directory>.desktop on Linux). Sharing the name with the
# user's real browser profile means Edge's preinstalled-web-app registration inside
# our isolated profile overwrites the user's own Teams/Outlook/M365 launchers. A
# distinct name keeps both namespaces apart.
PROFILE_DIRECTORY = "jarvis"


def migrate_profile_directory(profile_root: Path) -> bool:
    """Move a profile's inner ``Default`` folder to :data:`PROFILE_DIRECTORY`.

    Returns whether the profile root is ready to be used with
    ``--profile-directory``. Renaming keeps the cached browser session, so the
    services sharing this profile do not all need a fresh interactive sign-in.
    A locked folder (a browser still holding it) is not fatal: the caller keeps
    the old layout for this run and the move is retried on the next launch.
    """
    target = profile_root / PROFILE_DIRECTORY
    if target.exists():
        return True
    legacy = profile_root / "Default"
    if not legacy.exists():
        return True
    try:
        legacy.rename(target)
    except OSError as exc:
        logger.warning(
            "Could not move browser profile %s to %s (%s); keeping the previous "
            "layout for this run",
            legacy,
            target.name,
            exc,
        )
        return False
    logger.info("Moved browser profile %s to %s", legacy, target.name)
    return True


def _persistent_profile_dir(profile_name: str) -> Path:
    normalized_name = profile_name or ""
    if not normalized_name.strip():
        raise ValueError("persistent_profile must be a non-empty simple name")

    if any(ord(char) < 32 for char in normalized_name):
        raise ValueError("persistent_profile must not contain control characters")

    if normalized_name[-1] in {".", " "}:
        raise ValueError("persistent_profile must not end with a dot or space")

    if any(char in normalized_name for char in '<>:"/\\|?*'):
        raise ValueError("persistent_profile must not contain Windows-invalid filename characters")

    reserved_device_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
    device_name = normalized_name.split(".", 1)[0].upper()
    if device_name in reserved_device_names:
        raise ValueError("persistent_profile must not use a Windows reserved device name")

    candidate = Path(normalized_name)
    if candidate.is_absolute():
        raise ValueError("persistent_profile must not be an absolute path")

    if any(sep in normalized_name for sep in ("/", "\\")):
        raise ValueError("persistent_profile must not contain path separators")

    if ":" in normalized_name:
        raise ValueError("persistent_profile must not contain drive specifiers or colons")

    if normalized_name in {".", ".."} or candidate.parts != (normalized_name,):
        raise ValueError("persistent_profile must not contain path traversal")

    return _shared_browser_profiles_root() / normalized_name


async def _close_extra_startup_pages(context, navigation_page) -> None:
    """Best-effort close of stray startup pages (e.g. Edge sync-confirmation)."""
    for page in context.pages:
        if page is navigation_page:
            continue
        url = page.url or ""
        if url.startswith(
            (
                "edge://sync-confirmation-dialog",
                "chrome://sync-confirmation-dialog",
                "edge://welcome",
                "chrome://welcome",
            )
        ):
            try:
                await page.close()
                logger.debug("Closed startup page: %s", url)
            except Exception:
                pass


def _kill_browsers_for_profile(profile_dir: str | None) -> None:
    """Force-kill leftover automation browser processes pinned to *profile_dir*.

    Playwright's graceful ``context.close()`` can time out on Windows (Teams etc.
    keep EventSource/WebSocket streams open), which cancels the close and leaks
    the msedge process — it then holds the persistent profile and the next launch
    races/fails ("gracefully close end"). This is scoped by ``--user-data-dir`` so
    only OUR automation browser (which runs against this profile) is killed; the
    user's own Chrome/Edge (a different user-data-dir) is never touched.

    This cleanup can run from a windowless caller too (the router's own token refresh,
    which runs detached for days), so the ``powershell`` child needs ``CREATE_NO_WINDOW``:
    without it, a console-less parent gets a fresh, briefly visible console for it - empty,
    because ``capture_output`` already piped its output away from that console.
    """
    if not profile_dir:
        return
    import subprocess

    try:
        if sys.platform == "win32":
            needle = profile_dir.replace("'", "''")
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or "
                "Name='chrome.exe'\" | Where-Object { $_.CommandLine -like "
                f"'*{needle}*' }} | ForEach-Object {{ Stop-Process -Id "
                "$_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            subprocess.run(["pkill", "-f", profile_dir], capture_output=True, timeout=10)
    except Exception as exc:  # never let cleanup mask the real result
        logger.debug("Scoped browser kill for %s failed (ignored): %s", profile_dir, exc)


def _auth_debug_enabled() -> bool:
    """Whether to dump the final page (title/screenshot/HTML) for diagnosing a
    stalled sign-in. Honours ``JARVIS_AUTH_DEBUG`` and the broader ``JARVIS_DEBUG``."""
    for var in ("JARVIS_AUTH_DEBUG", "JARVIS_DEBUG"):
        if os.environ.get(var, "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


async def _dump_page_state(page, service: str, reason: str) -> None:
    """Report where a browser sign-in actually ended up.

    The final URL always goes to stderr (a cookie count alone cannot tell you
    WHICH page a headless flow stalled on - a login interstitial, a "Stay signed
    in?" prompt, a consent screen). Under ``JARVIS_AUTH_DEBUG`` the page title, a
    screenshot and the full HTML are written to ``<config>/auth/debug/`` so the
    missing auto-advance step can be identified from the real page, not guessed.
    That dump is an opt-in diagnostic: it can hold a login page's markup and
    screenshot, so it is written only under the flag, to the user-private config
    dir (outside any repo), and overwritten per service.
    """
    try:
        url = page.url or "(no url)"
    except Exception:
        url = "(url unavailable)"
    title = ""
    with suppress(Exception):
        title = await page.title()
    print(f"[{service}] sign-in ended ({reason}) at: {url}  title={title!r}", file=sys.stderr)

    if not _auth_debug_enabled():
        return
    try:
        debug_dir = config_root() / "auth" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        html_path = debug_dir / f"{service}-last-page.html"
        png_path = debug_dir / f"{service}-last-page.png"
        with suppress(Exception):
            html = await page.content()
            html_path.write_text(html, encoding="utf-8")
        with suppress(Exception):
            await page.screenshot(path=str(png_path), full_page=True)
        print(
            f"[{service}] auth-debug: dumped page to {html_path} and {png_path}",
            file=sys.stderr,
        )
    except Exception as exc:  # diagnostics must never break the auth flow
        logger.debug("[%s] auth-debug dump failed: %s", service, exc)


# ── Core browser session ────────────────────────────────────────────────────


async def _run_browser_session(
    *,
    url: str,
    service: str,
    headless: bool,
    persistent_profile: str | None,
    timeout: float,
    wait_for_url: str | re.Pattern | None,
    wait_for_cookies: list[str] | None,
    wait_for_cookies_mode: str = "all",
    wait_for_idle: float,
    harvest_ls: bool,
    ls_origins: list[str] | None,
    extra_ls_origins: list[str] | None,
    on_token: Callable | None,
    on_page: Callable | None = None,
    sandbox: bool,
    browser_args: list[str] | None,
    oauth_audience_contains: str | None = None,
    wait_for_oauth_access_token: bool = False,
    accept_access_token: Callable[[str], bool] | None = None,
    persist_tokens: bool = True,
    enforce_total_timeout: bool = False,
) -> AuthResult:
    """Run a single browser session (headless or headed) and harvest tokens."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    result = AuthResult(service=service)

    # ── Opt-in whole-operation deadline ───────────────────────────────────
    # One absolute monotonic expiry established BEFORE browser discovery/launch.
    # Every pre-completion stage passes only the remaining budget and raises
    # BrowserOperationTimeout on overrun; finalization stages after a successful
    # capture are merely capped (a good token is never discarded). Teardown runs
    # outside this deadline. When disabled, legacy timeout semantics are kept.
    operation_expiry: float | None = _monotonic() + timeout if enforce_total_timeout else None

    def _remaining_seconds() -> float | None:
        if operation_expiry is None:
            return None
        return operation_expiry - _monotonic()

    def _require_operation_budget(stage: str) -> float | None:
        """Remaining seconds for a pre-completion *stage*; raise if exhausted."""
        remaining = _remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise BrowserOperationTimeout(
                f"[{service}] Browser operation deadline exhausted before {stage}"
            )
        return remaining

    # ── Browser discovery ─────────────────────────────────────────────────
    from ..browser_discovery import discover_browser

    browser_name, browser_path = discover_browser()

    # Edge carries the Windows identity broker (WAM/CloudAP): at
    # login.microsoftonline.com it presents the device Primary Refresh Token, so an
    # Entra-joined box signs in silently with the user AND device claim (the device
    # claim satisfies device-gated Conditional Access without a separate MFA prompt),
    # and it runs windowless under Playwright's headless. Playwright launches Edge via
    # channel="msedge" (found through the registry) and any other discovered browser
    # via its executable path.
    channel: str | None = None
    executable_path: str | None = None
    if browser_name == "edge":
        channel = "msedge"
    else:
        executable_path = browser_path

    # ── Build launch arguments ────────────────────────────────────────────
    launch_args = [
        "--disable-sync",
        "--disable-features=msEdgeSidebarV2,msEdgeShoppingAssistant,FedCm",
    ]
    if browser_args:
        launch_args.extend(browser_args)

    # ── WSL2 + Windows browser fixes ──────────────────────────────────────
    # When the browser executable is a Windows binary (path starts with
    # /mnt/), Playwright needs the Windows path for the user-data-dir.
    wsl2_mode = str(browser_path).startswith("/mnt/")
    if wsl2_mode:
        try:
            _win_host_ip = None
            _resolv = Path("/etc/resolv.conf").read_text()
            for _line in _resolv.splitlines():
                if _line.startswith("nameserver "):
                    _win_host_ip = _line.split()[1]
                    break
            if _win_host_ip:
                for _pkey in ("NO_PROXY", "no_proxy"):
                    _cur = os.environ.get(_pkey, "")
                    if _win_host_ip not in _cur:
                        os.environ[_pkey] = f"{_cur},{_win_host_ip}" if _cur else _win_host_ip
                logger.info("WSL2 Windows browser: host IP → %s", _win_host_ip)
        except Exception as _e:
            logger.warning("WSL2 host IP detection failed: %s", _e)

    # ── Persistent profile ────────────────────────────────────────────────
    profile_dir: str | None = None
    if persistent_profile:
        pd = _persistent_profile_dir(persistent_profile)
        pd.mkdir(parents=True, exist_ok=True)
        profile_dir = str(pd)
        if migrate_profile_directory(pd):
            launch_args.append(f"--profile-directory={PROFILE_DIRECTORY}")

    mode = "headless" if headless else "headed"
    logger.info("[%s] Launching %s (%s) → %s", service, browser_name, mode, url)
    print(f"[{service}] Launching {browser_name} ({mode})...", file=sys.stderr)

    # Common launch kwargs. chromium_sandbox must be passed explicitly: Playwright
    # appends --no-sandbox whenever the option is not exactly True, so leaving it
    # unset disables the OS-level sandbox on every launch.
    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "args": launch_args,
        "chromium_sandbox": sandbox,
    }
    if channel:
        launch_kwargs["channel"] = channel
    if executable_path:
        launch_kwargs["executable_path"] = executable_path

    async with async_playwright() as p:
        # Launch browser (persistent context or ephemeral)
        browser = None
        context = None
        capture_open = False
        body_capture_tasks: set[asyncio.Task] = set()
        try:
            launch_budget = _require_operation_budget("browser launch")
            if launch_budget is not None:
                launch_kwargs["timeout"] = launch_budget * 1000  # Playwright ms

            async def _launch(kwargs: dict[str, Any]) -> tuple[Any, Any]:
                if profile_dir:
                    return None, await p.chromium.launch_persistent_context(profile_dir, **kwargs)
                launched = await p.chromium.launch(**kwargs)
                return launched, await launched.new_context()

            try:
                try:
                    browser, context = await _launch(launch_kwargs)
                except PlaywrightTimeoutError:
                    raise  # a launch deadline is not a sandbox problem
                except Exception as exc:
                    # A box where the sandbox is unusable (no setuid chrome-sandbox
                    # helper, unprivileged user namespaces disabled) cannot launch at
                    # all with it on. Failing auth outright would be worse than
                    # running without the boundary, so retry once unsandboxed.
                    if not launch_kwargs.get("chromium_sandbox"):
                        raise
                    logger.warning(
                        "[%s] Browser launch with the sandbox enabled failed (%s); "
                        "retrying with the sandbox disabled",
                        service,
                        exc,
                    )
                    launch_kwargs["chromium_sandbox"] = False
                    launch_kwargs["args"] = [*launch_args, "--no-sandbox"]
                    browser, context = await _launch(launch_kwargs)
            except PlaywrightTimeoutError as exc:
                if operation_expiry is not None:
                    raise BrowserOperationTimeout(
                        f"[{service}] Browser launch exceeded the operation deadline"
                    ) from exc
                raise

            # Get or create a page
            page = context.pages[0] if context.pages else await context.new_page()
            await _close_extra_startup_pages(context, page)

            # ── Capture ALL responses ─────────────────────────────────────────
            all_responses: list = []
            polling_wakeup = asyncio.Event()
            polling_failure: Exception | None = None

            def _record_polling_failure(exc: Exception) -> None:
                nonlocal polling_failure
                if polling_failure is None:
                    polling_failure = exc
                polling_wakeup.set()

            def _raise_polling_failure() -> None:
                if polling_failure is not None:
                    raise polling_failure

            async def _capture_response_body(response):
                if not capture_open:
                    return
                try:
                    body_bytes = await response.body()
                    body_text = body_bytes.decode("utf-8", errors="ignore")
                except Exception:
                    # Streaming and irrelevant responses may not expose a body.
                    return

                if not capture_open:
                    return
                try:
                    request_post_data = response.request.post_data
                except Exception:
                    request_post_data = None
                try:
                    request_headers = response.request.headers
                except Exception:
                    request_headers = None
                try:
                    captured = _capture_oauth_response_body(
                        service,
                        response.url,
                        body_text,
                        result,
                        on_token=on_token,
                        audience_contains=oauth_audience_contains,
                        accept_access_token=accept_access_token,
                        persist_tokens=persist_tokens,
                        request_post_data=request_post_data,
                        request_headers=request_headers,
                    )
                except Exception as exc:
                    _record_polling_failure(exc)
                    return

                if captured and result.oauth_tokens.get("access_token"):
                    polling_wakeup.set()

            def _on_response(response):
                if not capture_open:
                    return
                all_responses.append(response)
                # Schedule body capture as a concurrent task
                loop = asyncio.get_event_loop()
                task = loop.create_task(_capture_response_body(response))
                body_capture_tasks.add(task)
                task.add_done_callback(body_capture_tasks.discard)

            def _on_request(request):
                if not capture_open:
                    return
                try:
                    captured = _capture_request_bearer_token(
                        service,
                        request.url,
                        dict(request.headers),
                        result,
                        target_origin=target_origin,
                        on_token=on_token,
                        audience_contains=oauth_audience_contains,
                        accept_access_token=accept_access_token,
                        persist_tokens=persist_tokens,
                    )
                except Exception as exc:
                    _record_polling_failure(exc)
                    return

                if captured:
                    polling_wakeup.set()

            def _record_polling_interruption(description: str) -> None:
                _record_polling_failure(
                    RuntimeError(f"[{service}] Browser authentication interrupted: {description}")
                )

            target_origin = _origin_from_url(url)

            capture_open = True
            page.on("close", lambda *_args: _record_polling_interruption("page closed"))
            page.on("crash", lambda *_args: _record_polling_interruption("page crashed"))
            owning_browser = browser or getattr(context, "browser", None)
            browser_on = getattr(owning_browser, "on", None)
            if callable(browser_on):
                browser_on(
                    "disconnected",
                    lambda *_args: _record_polling_interruption("browser disconnected"),
                )
            page.on("response", _on_response)
            page.on("request", _on_request)

            # ── Navigate and wait for auth to complete ──────────────────────────
            goto_kwargs: dict[str, Any] = {"wait_until": "commit"}
            nav_budget = _require_operation_budget("navigation")
            if nav_budget is not None:
                goto_kwargs["timeout"] = nav_budget * 1000  # Playwright ms
            try:
                await page.goto(url, **goto_kwargs)
            except PlaywrightTimeoutError as exc:
                if operation_expiry is not None:
                    raise BrowserOperationTimeout(
                        f"[{service}] Navigation exceeded the operation deadline"
                    ) from exc
                raise
            # The completion-polling window gets only the budget left after launch
            # and navigation - it is never reset to a fresh ``timeout`` here.
            if operation_expiry is not None:
                deadline = asyncio.get_event_loop().time() + max(0.0, _remaining_seconds() or 0.0)
            else:
                deadline = asyncio.get_event_loop().time() + timeout
            auth_complete = False
            ms_advance_steps = 0
            sso_kickoff_urls: set[str] = set()
            corp_email = f"{getpass.getuser().lower()}@bosch.com"
            current_url = page.url or ""
            cookies_complete = False
            _raise_polling_failure()

            async def _cookies_now_complete() -> bool:
                if not wait_for_cookies or wait_for_oauth_access_token:
                    return False
                try:
                    ctx_cookies = await context.cookies()
                    return _cookies_complete(
                        {c["name"] for c in ctx_cookies},
                        wait_for_cookies,
                        wait_for_cookies_mode,
                    )
                except Exception:
                    return False

            def _completion_now() -> bool:
                return _auth_completion_reached(
                    current_url=current_url,
                    start_url=url,
                    target_origin=target_origin,
                    wait_for_url=wait_for_url,
                    wait_for_cookies=wait_for_cookies,
                    cookies_complete=cookies_complete,
                    access_token=result.oauth_tokens.get("access_token"),
                    wait_for_oauth_access_token=wait_for_oauth_access_token,
                )

            # ── Sign-in convergence: poll → settle → re-validate ────────────────
            # A completion observed at one instant can lapse while traffic settles:
            # with a warm profile the app shell commits on the target URL (matching
            # wait_for_url) and only then decides its session is stale, bouncing to
            # the identity provider - harvesting at that point yields a handful of
            # cookies and no tokens. A completion is final only when it still holds
            # AFTER the settle quiet-period; otherwise the sign-in loop resumes
            # (auto-advancing the IdP pages) within the same overall deadline.
            while True:
                while asyncio.get_event_loop().time() < deadline:
                    if wait_for_oauth_access_token:
                        remaining = max(0.0, deadline - asyncio.get_event_loop().time())
                        with suppress(TimeoutError):
                            await asyncio.wait_for(
                                polling_wakeup.wait(),
                                timeout=min(1.0, remaining),
                            )
                        polling_wakeup.clear()
                    else:
                        await asyncio.sleep(1.0)

                    current_url = page.url or ""
                    _raise_polling_failure()

                    # ── Auto-advance the Microsoft sign-in ───────────────────────
                    # A sign-in walks up to three interstitials on
                    # login.microsoftonline.com: username entry, "Pick an account"
                    # (when the profile has a cached account), and "Stay signed
                    # in?". Whichever is present on a given tick is handled; the
                    # handlers are idempotent (the username page is filled only
                    # while empty; the picker/KMSI leave the DOM once their click
                    # lands). The password step is answered silently by Kerberos
                    # WIA on *.bosch.com, never typed.
                    if "login.microsoftonline.com" in current_url:
                        try:
                            loginfmt = await page.query_selector('input[name="loginfmt"]')
                            if (
                                loginfmt is not None
                                and not ((await loginfmt.get_attribute("value")) or "").strip()
                            ):
                                await loginfmt.fill(corp_email)
                                ms_advance_steps += 1
                                await page.click('input[type="submit"]', timeout=5000)
                                logger.info(
                                    "[%s] MS sign-in: submitted username %s",
                                    service,
                                    corp_email,
                                )
                            elif await page.query_selector('input[name="DontShowAgain"]'):
                                # "Stay signed in?" - Yes keeps the profile session
                                # alive across runs.
                                await page.click("#idSIButton9", timeout=5000)
                                ms_advance_steps += 1
                                logger.info("[%s] MS sign-in: confirmed 'Stay signed in?'", service)
                            elif await page.query_selector(f'[data-test-id="{corp_email}"]'):
                                # "Pick an account" - the tile's data-test-id is the
                                # email; the exact match cannot hit the row's
                                # "-menu-dots" control or "signinOptions".
                                await page.click(f'[data-test-id="{corp_email}"]', timeout=5000)
                                ms_advance_steps += 1
                                logger.info(
                                    "[%s] MS sign-in: picked account %s", service, corp_email
                                )
                        except Exception as exc:
                            # A query/click racing an in-flight navigation destroys
                            # the execution context; this is benign - the next 1s
                            # tick re-evaluates the (now-settled) page. Only a
                            # non-navigation failure is worth a warning.
                            msg = str(exc)
                            if "context was destroyed" in msg or "navigation" in msg.lower():
                                logger.debug(
                                    "[%s] MS sign-in auto-advance skipped (navigation in "
                                    "flight); retrying next tick",
                                    service,
                                )
                            else:
                                logger.warning(
                                    "[%s] MS sign-in auto-advance failed: %s", service, exc
                                )

                    # ── Auto-fill Atlassian login email ──────────────────────────
                    if "id.atlassian.com/login" in current_url:
                        try:
                            inp = await page.query_selector('input[name="username"]')
                            if inp:
                                value = await inp.get_attribute("value") or ""
                                if not value.strip():
                                    await inp.fill(corp_email)
                                    remember = await page.query_selector('input[name="remember"]')
                                    if remember:
                                        await remember.click()
                                    submit = await page.query_selector("#login-submit")
                                    if submit:
                                        await submit.click()
                                        logger.info(
                                            "[%s] Auto-filled Atlassian email: %s",
                                            service,
                                            corp_email,
                                        )
                        except Exception as exc:
                            logger.warning("[%s] Atlassian auto-fill failed: %s", service, exc)

                    # ── Auto-fill Google account email ───────────────────────────
                    if "accounts.google.com" in current_url and "signin/identifier" in current_url:
                        try:
                            # Google's identifier field is <input type="text"
                            # id="identifierId" name="identifier">, NOT type="email";
                            # match the real one first.
                            inp = await page.query_selector(
                                '#identifierId, input[name="identifier"], input[type="email"]'
                            )
                            if inp:
                                value = await inp.get_attribute("value") or ""
                                if not value.strip():
                                    await inp.fill(corp_email)
                                    submit = await page.query_selector("#identifierNext button")
                                    if not submit:
                                        submit = await page.query_selector("#identifierNext")
                                    if submit:
                                        await submit.click()
                                        logger.info(
                                            "[%s] Auto-filled Google email: %s",
                                            service,
                                            corp_email,
                                        )
                                    else:
                                        logger.warning(
                                            "[%s] Google identifier page: no "
                                            "#identifierNext button found to submit %s",
                                            service,
                                            corp_email,
                                        )
                        except Exception as exc:
                            # Don't hide a cold-start stall behind a bare pass.
                            logger.warning(
                                "[%s] Google email auto-fill failed on %s: %s",
                                service,
                                current_url,
                                exc,
                            )

                    # ── Start an application's own single-sign-on ────────────────
                    # An app that presents a sign-in wall instead of its content
                    # (whether at a /login URL or rendered in place by an SPA).
                    # login_wall judges the DOM, so no URL gate is needed here;
                    # the Microsoft IdP host is excluded because its own handler
                    # above owns it. Each URL is kicked at most once, so a wall
                    # that does not navigate is not clicked on every tick.
                    if (
                        "login.microsoftonline.com" not in current_url
                        and current_url not in sso_kickoff_urls
                        and await login_wall.start_sso(page, service)
                    ):
                        sso_kickoff_urls.add(current_url)

                    # ── Dismiss the Google passkey-enrollment speed bump ─────────
                    # Gated on the passkey speed-bump URL so it only fires when we
                    # actually land on that interstitial (Google shows it after
                    # SSO, offering to enroll a passkey). We click "Not now".
                    if "speedbump/passkeyenrollment" in current_url:
                        try:
                            clicked = await page.evaluate(
                                "(() => {"
                                "  const els = [...document.querySelectorAll('button, a')];"
                                "  const re = /(not now|maybe later|skip|jetzt nicht|nicht jetzt|sp\\u00e4ter)/i;"  # noqa: E501  embedded JS, not reflowable
                                "  const btn = els.find(el => re.test((el.textContent || '').trim()));"  # noqa: E501  embedded JS, not reflowable
                                "  if (btn) { btn.click(); return (btn.textContent || '').trim(); }"
                                "  return null;"
                                "})()"
                            )
                            if clicked:
                                logger.info(
                                    "[%s] Dismissed passkey speed bump via %r",
                                    service,
                                    clicked,
                                )
                            else:
                                logger.warning(
                                    "[%s] On passkey speed bump but found no "
                                    "'Not now'/skip button to click",
                                    service,
                                )
                        except Exception as exc:
                            logger.warning(
                                "[%s] passkey speed-bump skip failed: %s",
                                service,
                                exc,
                            )

                    cookies_complete = await _cookies_now_complete()
                    _raise_polling_failure()
                    auth_complete = _completion_now()
                    if auth_complete:
                        break

                if not auth_complete and wait_for_oauth_access_token:
                    # This check is deliberately synchronous: timeout=0 grants no
                    # event-loop turn to response.body() work after the deadline.
                    _raise_polling_failure()
                    auth_complete = _completion_now()

                if not auth_complete:
                    # Pending response tasks must not mutate AuthResult or persist
                    # tokens after clean polling exhaustion.
                    capture_open = False
                    for task in list(body_capture_tasks):
                        if not task.done():
                            task.cancel()
                    timed_out_url = page.url
                    _raise_polling_failure()
                    expected_hint = (
                        str(wait_for_url.pattern)
                        if isinstance(wait_for_url, re.Pattern)
                        else str(wait_for_url)
                        if wait_for_url
                        else "(any same-origin URL)"
                    )
                    logger.warning(
                        "[%s] Auth timed out after %ds. Browser was at: %s "
                        "(expected URL matching: %s)",
                        service,
                        timeout,
                        timed_out_url,
                        expected_hint,
                    )
                    print(
                        f"WARNING: [{service}] Auth timed out after {timeout}s.\n"
                        f"  Browser was at:  {timed_out_url}\n"
                        f"  Expected URL matching: {expected_hint}",
                        file=sys.stderr,
                    )
                    if "login.microsoftonline.com" in current_url and ms_advance_steps == 0:
                        logger.warning(
                            "[%s] Timed out on the MS login page and no auto-advance "
                            "step matched -- the login page structure may have changed.",
                            service,
                        )
                        print(
                            f"WARNING: [{service}] No sign-in auto-advance step matched "
                            f"on login.microsoftonline.com. The page structure may "
                            f"have changed.",
                            file=sys.stderr,
                        )
                    await _dump_page_state(page, service, "timeout")
                    error_message = (
                        f"[{service}] Auth did not complete within {timeout}s. "
                        f"Last URL: {timed_out_url}"
                    )
                    if wait_for_oauth_access_token:
                        raise OAuthAccessTokenUnavailable(error_message)
                    raise TimeoutError(error_message)

                # ── Settle: let remaining traffic arrive ──────────────────────
                logger.info(
                    "[%s] Auth complete, waiting %.1fs for traffic to settle...",
                    service,
                    wait_for_idle,
                )
                # Finalization runs only within the budget left after a successful
                # capture; it is capped (never raising) so a good token survives.
                settle_remaining = _remaining_seconds()
                effective_idle = (
                    wait_for_idle
                    if settle_remaining is None
                    else max(0.0, min(wait_for_idle, settle_remaining))
                )
                settle_end = asyncio.get_event_loop().time() + effective_idle
                max_settle_end = asyncio.get_event_loop().time() + effective_idle * 3
                last_count = len(all_responses)
                while asyncio.get_event_loop().time() < settle_end:
                    await asyncio.sleep(0.5)
                    if len(all_responses) > last_count:
                        last_count = len(all_responses)
                        settle_end = min(
                            settle_end + 1.0,
                            asyncio.get_event_loop().time() + effective_idle,
                            max_settle_end,
                        )

                # Re-validate after the quiet period; only a completion that
                # survived it is final.
                current_url = page.url or ""
                cookies_complete = await _cookies_now_complete()
                if _completion_now():
                    break
                auth_complete = False
                logger.info(
                    "[%s] Completion lapsed during settle (now at %s) - resuming sign-in",
                    service,
                    current_url,
                )
                print(
                    f"[{service}] sign-in not settled (now at "
                    f"{urlparse(current_url).netloc or current_url[:60]}) - continuing...",
                    file=sys.stderr,
                )

            logger.info("[%s] Captured %d total responses", service, len(all_responses))
            # Report where a "complete" sign-in actually landed. A flow can match
            # the completion URL yet still be unauthenticated (the app shell loads
            # but SSO never finished), harvesting no refresh token - under
            # JARVIS_AUTH_DEBUG this dumps the page so the missing step is visible.
            if _auth_debug_enabled():
                await _dump_page_state(page, service, "auth-complete")
            if body_capture_tasks:
                # Teams uses EventSource/streaming responses whose body()
                # never completes. Cap the wait so we don't hang forever, and
                # shrink it to the remaining operation budget when enforced.
                pending = list(body_capture_tasks)
                drain_remaining = _remaining_seconds()
                drain_timeout = (
                    5.0 if drain_remaining is None else max(0.0, min(5.0, drain_remaining))
                )
                if drain_timeout <= 0:
                    for t in pending:
                        if not t.done():
                            t.cancel()
                else:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*pending, return_exceptions=True),
                            timeout=drain_timeout,
                        )
                    except TimeoutError:
                        logger.debug(
                            "[%s] %d body capture task(s) still pending after %.1fs -- cancelling",
                            service,
                            sum(1 for t in pending if not t.done()),
                            drain_timeout,
                        )
                        for t in pending:
                            if not t.done():
                                t.cancel()

            # ── Hand the live, settled page to an on_page callback ──────────────
            # Runs while the page is still on the target origin - BEFORE the
            # localStorage harvest below navigates it away. Whatever the callback
            # returns rides back on the result. A callback failure must NOT read
            # as an auth failure (which would trip the headed retry and skip the
            # cookie harvest below): swallow it and leave page_result unset for
            # the caller to detect.
            if on_page is not None:
                try:
                    result.page_result = await on_page(page)
                except Exception as exc:
                    logger.warning("[%s] on_page callback failed: %s", service, exc)

            # ── Harvest cookies ─────────────────────────────────────────────────
            try:
                all_cookies = await context.cookies()
                harvested: list[CookieEntry] = []
                for c in all_cookies:
                    domain = c["domain"].lstrip(".")
                    path = c.get("path") or "/"
                    expires_at: float | None = c.get("expires", -1)
                    if expires_at is not None and expires_at <= 0:
                        expires_at = None
                    jwt_exp = _jwt_expiry(c["value"]) if c["value"].startswith("eyJ") else None
                    exp = jwt_exp or expires_at
                    harvested.append(CookieEntry(domain, c["name"], c["value"], path, exp))
                    _store_token(
                        service,
                        "cookie",
                        c["name"],
                        c["value"],
                        domain=domain,
                        path=path,
                        expires_at=exp,
                        on_token=on_token,
                        persist=persist_tokens,
                    )
                result.cookies = CookieJar(harvested)
                logger.info("[%s] Harvested %d cookies", service, len(result.cookies))
            except Exception as exc:
                logger.warning("[%s] Cookie harvest failed: %s", service, exc)

            if result.oauth_tokens:
                logger.info(
                    "[%s] Intercepted OAuth2 tokens: %s",
                    service,
                    ", ".join(sorted(result.oauth_tokens.keys())),
                )

            # ── Harvest localStorage ────────────────────────────────────────────
            if harvest_ls:
                origins_to_scan = []
                target_ls_origin = _ls_origin_dir(url)
                origins_to_scan.append(target_ls_origin)
                if ls_origins:
                    for o in ls_origins:
                        if o not in origins_to_scan:
                            origins_to_scan.append(o)
                extra = (
                    extra_ls_origins if extra_ls_origins is not None else list(_KNOWN_LS_ORIGINS)
                )
                for o in extra:
                    if o not in origins_to_scan:
                        origins_to_scan.append(o)

                for origin in origins_to_scan:
                    ls_remaining = _remaining_seconds()
                    if ls_remaining is not None and ls_remaining <= 0:
                        # Budget spent on this best-effort finalization; stop
                        # scanning rather than overrun the operation deadline.
                        break
                    try:
                        if origin != target_ls_origin:
                            origin_url = origin.replace("+++", "://")
                            ls_goto_timeout = 10000
                            if ls_remaining is not None:
                                ls_goto_timeout = min(10000, max(1, int(ls_remaining * 1000)))
                            try:
                                await page.goto(
                                    origin_url, wait_until="commit", timeout=ls_goto_timeout
                                )
                                await asyncio.sleep(1.0)
                            except Exception:
                                logger.debug("[%s] Could not navigate to %s", service, origin_url)
                                continue

                        ls_data = await page.evaluate(
                            "(() => { try { return Object.fromEntries(Object.entries(localStorage)); } catch(e) { return {}; } })()"  # noqa: E501  embedded JS, not reflowable
                        )
                        if not ls_data or not isinstance(ls_data, dict):
                            continue
                        for ls_key, ls_val in ls_data.items():
                            if not ls_val:
                                continue
                            key_lower = ls_key.lower()
                            is_token = (
                                "-accesstoken-" in ls_key
                                or "|accesstoken|" in ls_key
                                or "accesstoken" in key_lower
                                or "access_token" in key_lower
                                or "refreshtoken" in key_lower
                                or "refresh_token" in key_lower
                                or ls_key.startswith("lxRefreshToken:")
                                or ls_key.startswith("lxAccessToken:")
                            )
                            if is_token:
                                token_val = _extract_token_from_ls_value(ls_val)
                                if token_val:
                                    ls_store_key = f"{origin}::{ls_key}"
                                    result.ls_tokens[ls_store_key] = token_val
                                    exp = _jwt_expiry(token_val)
                                    _store_token(
                                        service,
                                        "ls",
                                        ls_key,
                                        token_val,
                                        domain=origin,
                                        expires_at=exp,
                                        on_token=on_token,
                                        persist=persist_tokens,
                                    )
                    except Exception as exc:
                        logger.debug(
                            "[%s] Could not read localStorage for %s: %s", service, origin, exc
                        )

                if result.ls_tokens:
                    logger.info(
                        "[%s] Harvested %d localStorage tokens", service, len(result.ls_tokens)
                    )

            print(f"[{service}] {result.summary()}", file=sys.stderr)
            return result
        finally:
            capture_open = False
            pending_capture_tasks = list(body_capture_tasks)
            for task in pending_capture_tasks:
                if not task.done():
                    task.cancel()
            if pending_capture_tasks:
                try:
                    await asyncio.wait(pending_capture_tasks, timeout=0.25)
                except Exception as exc:
                    logger.debug(
                        "[%s] OAuth capture-task cleanup failed (ignored): %s",
                        service,
                        exc,
                    )
            # Close browser/context with a hard timeout. Teams keeps
            # EventSource/WebSocket streams alive, and Playwright's graceful
            # close waits for them forever on Windows. Wrap in wait_for so
            # we don't hang the whole process after auth succeeded.
            try:
                if browser:
                    await asyncio.wait_for(browser.close(), timeout=5.0)
                elif context:
                    await asyncio.wait_for(context.close(), timeout=5.0)
            except (TimeoutError, Exception) as exc:
                logger.debug("[%s] Browser close timeout/error (ignored): %s", service, exc)
            # Graceful close can leave the persistent-context msedge alive on
            # Windows; force-kill anything still pinned to this profile so it
            # doesn't leak and lock the profile for the next launch.
            _kill_browsers_for_profile(profile_dir)


def _extract_token_from_ls_value(value: str) -> str | None:
    """Extract a token (JWT or opaque) from a localStorage value string.

    Handles these formats:
      - MSAL/Teams: JSON with "secret" field → {"secret": "eyJ..."}
      - Raw JWT: "eyJ..." (three dot-separated base64 segments)
      - Opaque tokens: long strings (>100 chars) that aren't HTML/JSON
        (e.g., LeanIX refresh tokens, FEBER opaque tokens)
    """
    if not value or len(value) < 20:
        return None

    # MSAL format: JSON object with "secret" field containing the JWT
    if value.startswith("{"):
        try:
            data = json.loads(value)
            for field_name in (
                "secret",
                "access_token",
                "refresh_token",
                "id_token",
                "token",
                "jwt",
            ):
                candidate = data.get(field_name, "")
                if isinstance(candidate, str) and len(candidate) > 20:
                    return candidate
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # Skip HTML error pages or wrapper content
    if value.startswith("<"):
        return None

    # Raw JWT (base64-encoded header.payload.signature)
    if value.startswith("eyJ"):
        m = _JWT_RE.match(value)
        if m:
            return m.group()

    # Opaque tokens (refresh tokens etc.) -- long non-structured strings
    if len(value) > 100:
        return value

    return None


# ── Public API ──────────────────────────────────────────────────────────────


def _kill_stale_browsers() -> None:
    """Free leftover AUTOMATION browsers holding a shared-auth profile.

    Called before the headed fallback so a still-alive Edge from the failed
    headless attempt doesn't lock the profile. Scoped by ``--user-data-dir`` to
    the shared browser-profiles root, so the user's OWN Chrome/Edge (a different
    profile) is never killed. An image-wide ``taskkill /IM chrome.exe`` would do the
    opposite: kill the user's personal Chrome and spare the leaked automation Edge.
    """
    _kill_browsers_for_profile(str(_shared_browser_profiles_root()))


def ensure_auth(
    service: str,
    url: str,
    *,
    headless: bool | str = "auto",
    persistent_profile: str | None = None,
    timeout: float = 60,
    wait_for_url: str | re.Pattern | None = None,
    wait_for_cookies: list[str] | None = None,
    wait_for_cookies_mode: str = "all",
    wait_for_idle: float = 3.0,
    harvest_ls: bool = True,
    ls_origins: list[str] | None = None,
    extra_ls_origins: list[str] | None = None,
    on_token: Callable | None = None,
    on_page: Callable | None = None,
    persist_tokens: bool = True,
    proxy_bypass: bool = True,
    sandbox: bool = True,
    browser_args: list[str] | None = None,
    oauth_audience_contains: str | None = None,
    wait_for_oauth_access_token: bool = False,
    accept_access_token: Callable[[str], bool] | None = None,
    enforce_total_timeout: bool = False,
) -> AuthResult:
    """Launch a browser, authenticate via SSO, and harvest all tokens.

    This is the main entry point. See module docstring for parameter docs.

    Args:
        service:  Identifier for cache storage (e.g. "powerbi", "leanix").
        url:      SSO entry point URL to navigate to.
        timeout: Seconds available for completion polling. Zero is strict: it
            does not wait after goto for asynchronous response-body capture.
        wait_for_oauth_access_token: Require an accepted OAuth access token,
            ignoring the normal URL, cookie, and same-origin completion signals.
        accept_access_token: Optional predicate that must approve captured
            OAuth access tokens before they are retained or persisted.
        persist_tokens: Write captured material to the shared cache when true.
            Disabling this does not remove material from the returned result or
            suppress the caller-owned ``on_token`` notification hook (which
            may itself persist material). It does not make a persistent
            Playwright profile read-only; browser SSO state may still change.
        enforce_total_timeout: Opt-in whole-operation deadline. When true, one
            absolute monotonic expiry is set before launch and bounds launch,
            navigation, the completion-polling window, settling, body draining,
            and localStorage harvest; ``timeout`` is not restarted after
            navigation. Launch/navigation overruns raise
            ``BrowserOperationTimeout``. Default false keeps legacy per-stage
            semantics for unrelated services.

    Returns:
        AuthResult with all harvested cookies, OAuth2 tokens, and
        localStorage tokens.

    Raises:
        TimeoutError: If auth does not complete within *timeout* seconds.
    """
    if persistent_profile is not None:
        _persistent_profile_dir(persistent_profile)

    if proxy_bypass:
        _apply_proxy_bypass()

    common_kwargs: _SessionKwargs = {
        "url": url,
        "service": service,
        "persistent_profile": persistent_profile,
        "timeout": timeout,
        "wait_for_url": wait_for_url,
        "wait_for_cookies": wait_for_cookies,
        "wait_for_cookies_mode": wait_for_cookies_mode,
        "wait_for_idle": wait_for_idle,
        "harvest_ls": harvest_ls,
        "ls_origins": ls_origins,
        "extra_ls_origins": extra_ls_origins,
        "on_token": on_token,
        "on_page": on_page,
        "sandbox": sandbox,
        "browser_args": browser_args,
        "oauth_audience_contains": oauth_audience_contains,
        "wait_for_oauth_access_token": wait_for_oauth_access_token,
        "accept_access_token": accept_access_token,
        "persist_tokens": persist_tokens,
        "enforce_total_timeout": enforce_total_timeout,
    }

    try:
        if headless == "auto":
            # Strategy: try headless first, fall back to headed
            try:
                return asyncio.run(_run_browser_session(headless=True, **common_kwargs))
            except (RuntimeError, TimeoutError, Exception) as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                logger.info(
                    "[%s] Headless attempt failed (%s), retrying headed...",
                    service,
                    type(exc).__name__,
                )
                print(
                    f"[{service}] Headless auth failed, opening browser window "
                    f"for interactive login...",
                    file=sys.stderr,
                )
                _kill_stale_browsers()
                common_kwargs["timeout"] = 120  # give headed more time
                return asyncio.run(_run_browser_session(headless=False, **common_kwargs))
        else:
            return asyncio.run(_run_browser_session(headless=bool(headless), **common_kwargs))
    finally:
        if proxy_bypass:
            _restore_proxy_defaults()
