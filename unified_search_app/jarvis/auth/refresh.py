"""Centralized token refresh and exchange logic.

The execution layer that matches the refresh strategies declared in
profiles.py, shared by every service profile that needs one (powerbi, leanix,
normmaster, azure-devops, gemini, and others) - no individual skill hand-rolls
its own token exchange.

Three strategies:
    oauth2_refresh      - OAuth2 grant_type=refresh_token exchange
    cookie_exchange     - Cookie -> JWT/Bearer token exchange
    compute_sapisidhash - Google SAPISIDHASH Authorization header computation

All HTTP calls go through ``http_transport.request`` so that corporate
proxies (Bosch Zscaler) get SSPI authentication automatically. Direct
``urllib.request.urlopen`` honors HTTP(S)_PROXY env vars but does not perform
proxy auth, which yields a 407 Proxy Authentication Required on managed
networks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
from typing import Any

from .http import transport as http_transport
from .jwt_utils import jwt_expiry
from .profiles import CookieExchangeStrategy, OAuthRefreshStrategy, SapisidhashStrategy

log = logging.getLogger(__name__)


# ── OAuth2 refresh_token exchange ───────────────────────────────────────────


def oauth2_refresh(
    strategy: OAuthRefreshStrategy,
    refresh_token: str,
) -> dict[str, Any]:
    """Exchange a refresh_token for a new access_token via OAuth2.

    Used by PowerBI (Azure AD, needs CORS Origin header) and LeanIX.

    Returns dict with at least ``access_token``; may also contain
    ``refresh_token`` and ``expires_in``.
    """
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": strategy.client_id,
            "refresh_token": refresh_token,
            **({"scope": strategy.scope} if strategy.scope else {}),
            **({"claims": strategy.claims} if strategy.claims else {}),
        }
    )

    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if strategy.origin:
        headers["Origin"] = strategy.origin

    log.debug("OAuth2 refresh -> %s", strategy.token_endpoint)
    try:
        resp = http_transport.request(
            "POST",
            strategy.token_endpoint,
            headers=headers,
            data=body,
            timeout=30,
        )
    except Exception as exc:
        raise RuntimeError(f"OAuth2 refresh connection error: {exc}") from exc

    if resp.status_code >= 400:
        detail = resp.text[:500] if resp.text else ""
        raise RuntimeError(f"OAuth2 refresh failed ({resp.status_code}): {detail}")

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OAuth2 refresh response was not JSON: {resp.text[:200]}") from exc

    if "error" in data:
        description = data.get("error_description", "(no description)")
        raise RuntimeError(f"OAuth2 refresh error: {data['error']} - {description}")

    if "access_token" not in data:
        raise RuntimeError(f"OAuth2 refresh response missing access_token: {list(data.keys())}")

    exp = jwt_expiry(data["access_token"])
    if exp:
        log.info(
            "OAuth2 token refreshed, expires in %.0f s",
            exp - time.time(),
        )
    else:
        log.info("OAuth2 token refreshed (non-JWT or no exp claim)")

    return data


# ── Cookie → token exchange ─────────────────────────────────────────────────


def cookie_exchange(
    strategy: CookieExchangeStrategy,
    cookies: dict[str, str],
    *,
    url_params: dict[str, str] | None = None,
) -> str:
    """Exchange cookies for a JWT or Bearer token.

    Used by NormMaster (POST cookies → JWT) and Azure DevOps (POST cookies →
    Bearer session token).

    ``url_params`` is used for template substitution in the endpoint URL,
    e.g. ``{organization}`` for Azure DevOps.

    Returns the extracted token string.
    """
    endpoint = strategy.exchange_endpoint
    if url_params:
        endpoint = endpoint.format(**url_params)

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    headers: dict[str, str] = {
        "Cookie": cookie_header,
        "Content-Type": "application/json",
    }
    for hdr_name, hdr_value in strategy.extra_headers:
        headers[hdr_name] = hdr_value

    body = strategy.body or None

    log.debug("Cookie exchange -> %s", endpoint)
    try:
        resp = http_transport.request(
            strategy.method,
            endpoint,
            headers=headers,
            data=body,
            timeout=30,
        )
    except Exception as exc:
        raise RuntimeError(f"Cookie exchange connection error: {exc}") from exc

    if resp.status_code >= 400:
        detail = resp.text[:500] if resp.text else ""
        raise RuntimeError(f"Cookie exchange failed ({resp.status_code}): {detail}")

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cookie exchange response was not JSON: {resp.text[:200]}") from exc

    token = data.get(strategy.extract_field)
    if not token:
        raise RuntimeError(
            f"Cookie exchange response missing '{strategy.extract_field}': {list(data.keys())}"
        )

    log.info("Cookie exchange succeeded for %s", endpoint)
    return token


# ── SAPISIDHASH computation ─────────────────────────────────────────────────


def compute_sapisidhash(
    strategy: SapisidhashStrategy,
    cookies: dict[str, str],
    origin: str | None = None,
) -> str:
    """Compute a Google SAPISIDHASH Authorization header value.

    Used by Gemini / Vertex AI Search. The hash is
    ``SHA1(timestamp + " " + SAPISID + " " + origin)``, returned as a
    multi-part Authorization header.
    """
    sapisid = cookies.get(strategy.cookie_name)
    if not sapisid:
        raise RuntimeError(f"SAPISIDHASH computation requires '{strategy.cookie_name}' cookie")

    effective_origin = origin or strategy.origin
    if not effective_origin:
        raise RuntimeError("SAPISIDHASH computation requires an origin URL")

    timestamp = str(int(time.time()))
    raw = f"{timestamp} {sapisid} {effective_origin}"
    token_hash = hashlib.sha1(raw.encode()).hexdigest()

    header = f"SAPISIDHASH {timestamp}_{token_hash}"
    # Google APIs expect all three variants
    header += f" SAPISID1PHASH {timestamp}_{token_hash}"
    header += f" SAPISID3PHASH {timestamp}_{token_hash}"

    log.debug("SAPISIDHASH computed for origin %s", effective_origin)
    return header
