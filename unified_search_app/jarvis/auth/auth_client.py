# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""Authenticated HTTP client with automatic 401 retry.

Combines ``ensure_credentials`` + ``http_transport.request`` into a single
client that handles credential injection and transparent re-authentication.

Usage (Bearer token)::

    from jarvis.auth.auth_client import AuthedClient

    client = AuthedClient("bai")
    resp = client.get("https://bai-api.cloud.bosch.tech/api/search", params={"query": "RBGF"})
    data = resp.json()

Usage (cookie-based)::

    client = AuthedClient("org-manager")
    resp = client.get("https://org-manager.app.bosch.com/.../units/search", params={...})

The client:
- Calls ``ensure_credentials`` on first use (lazy)
- Injects the correct auth headers/cookies based on the credential type
- On 401/403: invalidates the cache, re-authenticates, retries ONCE
- Works with http_transport (NTLM proxy aware)

For skills with exotic auth flows (SAPISID, two-step token exchange),
keep using ``ensure_credentials`` + ``invalidate`` directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .auth import ServiceCredentials, ensure_credentials, invalidate
from .http import transport as http_transport
from .http.cookies import CookieSelector
from .http.cookies import select as _select_cookies

logger = logging.getLogger(__name__)

_REAUTH_STATUS_CODES = (401, 403)


class AuthedClient:
    """HTTP client with automatic credential management and 401 retry.

    Args:
        service: Service name matching a profile (e.g. "bai", "teams").
        required_artifact: Artifact that must be present in credentials.
        auth_header_builder: Optional callable that takes ServiceCredentials
            and returns a dict of HTTP headers to inject. If None, the client
            auto-detects Bearer token vs cookie-based auth from the credentials.
        cookie_domain: Domain for cookie pinning (auto-detected from URL if None).
            Also the host the cookie jar is scoped against (see send_cookies).
        extra_headers: Additional headers merged into every request.
        send_cookies: Which harvested cookies ride on each request. The shared
            browser profile accumulates the whole SSO jar (50-100+ cookies);
            sending all of it overflows gateway header limits
            (``400 Request Header Or Cookie Too Large``) and leaks unrelated
            cookies. Accepts:

            - ``True`` (default): domain-scope — send only cookies whose
              harvested domain matches the request host or a parent
              (``CookieJar.for_host``). Zero config; correct for
              almost every skill.
            - ``False``: send no cookies (pure Bearer-token APIs, e.g. feber).
            - an iterable of names / a callable: an explicit allowlist or
              predicate applied to the FULL jar (``jarvis.auth.cookies.select``),
              for hosts that share the SSO domain so domain-scoping alone is
              insufficient (e.g. ALM's Jazz on ``*.bosch.com``, GHE).
        auth_challenge: Optional predicate over the ``requests.Response``. Some
            servers answer an expired session with ``200`` + a login page (or a
            marker header) instead of ``401``/``403`` (IBM Jazz, some
            SharePoint). Return True to treat the response as an auth failure
            and trigger the one-shot re-auth + retry.
        profile: Override the ServiceProfile used for auth. Required for skills
            whose profile is built at runtime (per-host service keys / dynamic
            start_url, e.g. jira, bitbucket, github-enterprise). Passed through
            to ``ensure_credentials`` on both first use and refresh.
    """

    def __init__(
        self,
        service: str,
        *,
        required_artifact: str | None = None,
        auth_header_builder: Callable[[ServiceCredentials], dict[str, str]] | None = None,
        cookie_domain: str | None = None,
        extra_headers: dict[str, str] | None = None,
        send_cookies: CookieSelector = True,
        auth_challenge: Callable[[Any], bool] | None = None,
        profile: Any = None,
    ):
        self._service = service
        self._required_artifact = required_artifact
        self._auth_header_builder = auth_header_builder
        self._cookie_domain = cookie_domain
        self._extra_headers = extra_headers or {}
        self._send_cookies = send_cookies
        self._auth_challenge = auth_challenge
        self._profile = profile
        self._creds: ServiceCredentials | None = None

    @property
    def credentials(self) -> ServiceCredentials:
        """Current credentials (lazily fetched)."""
        if self._creds is None:
            self._creds = ensure_credentials(
                self._service,
                required_artifact=self._required_artifact,
                profile=self._profile,
            )
        return self._creds

    def refresh(self) -> ServiceCredentials:
        """Force re-authentication (browser flow)."""
        invalidate(self._service)
        self._creds = ensure_credentials(
            self._service,
            required_artifact=self._required_artifact,
            profile=self._profile,
            force_browser=True,
        )
        return self._creds

    def _resolve_cookies(self, creds: ServiceCredentials, url: str) -> dict[str, str] | None:
        """Pick the cookie subset to send, per the ``send_cookies`` policy."""
        if self._send_cookies is False or not creds.cookies:
            return None
        if self._send_cookies is True:
            # Default: the browser's send rule for THIS request (domain AND
            # cookie-path match). A cookie_domain override pins the host but
            # keeps the request's path for the path match.
            if self._cookie_domain:
                scoped = creds.cookies.for_url(f"https://{self._cookie_domain}{_path_of(url)}")
            else:
                scoped = creds.cookies.for_url(url)
        else:
            # Explicit allowlist / predicate: apply to the FULL jar so the skill
            # keeps exactly the cookies it declared, even cross-domain ones.
            scoped = _select_cookies(creds.cookies, self._send_cookies)
        return scoped or None

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json_body: Any = None,
        timeout: int = 15,
        allow_redirects: bool = True,
        _retried: bool = False,
    ):
        """Make an authenticated HTTP request with auto-retry on 401/403.

        Returns a ``requests.Response``.
        """
        creds = self.credentials
        auth_headers = self._build_auth_headers(creds)

        # Merge: auth_headers < extra_headers < per-call headers
        merged_headers = {**auth_headers, **self._extra_headers}
        if headers:
            merged_headers.update(headers)

        # Determine cookies and domain
        cookies = self._resolve_cookies(creds, url)
        domain = self._cookie_domain or _domain_from_url(url)

        resp = http_transport.request(
            method,
            url,
            headers=merged_headers,
            params=params,
            data=data,
            json_body=json_body,
            timeout=timeout,
            allow_redirects=allow_redirects,
            cookies=cookies,
            cookie_domain=domain,
        )

        challenged = bool(self._auth_challenge and self._auth_challenge(resp))
        if (resp.status_code in _REAUTH_STATUS_CODES or challenged) and not _retried:
            logger.info(
                "[%s] Got %s%s, re-authenticating...",
                self._service,
                resp.status_code,
                " (auth challenge)" if challenged else "",
            )
            self.refresh()
            return self.request(
                method,
                url,
                params=params,
                headers=headers,
                data=data,
                json_body=json_body,
                timeout=timeout,
                allow_redirects=allow_redirects,
                _retried=True,
            )

        return resp

    # Convenience methods

    def get(self, url: str, **kwargs):
        """GET request."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        """POST request."""
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        """PUT request."""
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs):
        """DELETE request."""
        return self.request("DELETE", url, **kwargs)

    def patch(self, url: str, **kwargs):
        """PATCH request."""
        return self.request("PATCH", url, **kwargs)

    # ── Private ─────────────────────────────────────────────────────────

    def _build_auth_headers(self, creds: ServiceCredentials) -> dict[str, str]:
        """Build auth headers from credentials."""
        if self._auth_header_builder:
            return self._auth_header_builder(creds)

        # Auto-detect: if any artifact looks like a Bearer token, use it
        for name, value in creds.artifacts.items():
            if "token" in name and value and value.startswith("eyJ"):
                return {"Authorization": f"Bearer {value}"}

        # Also check for explicitly named access_token / bearer
        for key in ("access_token", "bearer_token", "feber_access_token"):
            token = creds.get(key)
            if token:
                return {"Authorization": f"Bearer {token}"}

        # No Bearer token found -- rely on cookies (injected separately)
        return {}


def _domain_from_url(url: str) -> str:
    """Extract domain from URL for cookie pinning."""
    return urlsplit(url).hostname or ""


def _path_of(url: str) -> str:
    """The request path of *url* (``/`` when absent)."""
    return urlsplit(url).path or "/"
