# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""Viva Engage authentication and the GraphQL transport.

``EngageSession`` registers the ``viva-engage`` :class:`ServiceProfile` on import
(no cwd-walk discovery), holds the single Bearer token one browser capture yields
(the ``office_access_token`` cookie), and issues authenticated, self-healing
requests. ``ensure()`` silently refreshes a stale token through the OAuth
``refresh_token`` grant (no browser); ``send()`` injects the Bearer and, on a live
401/403, forces one browser re-auth and retries once.

Every Engage API call is one POST to a single GraphQL endpoint. ``graphql()`` builds
the request body - a self-healing ad-hoc query document where the server accepts one,
else a persisted-query sha256 hash for the operations gated to persisted mode - and
returns the ``data`` object.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from jarvis.auth import ServiceCredentials, ensure_credentials
from jarvis.auth.http.errors import raise_for_status
from jarvis.auth.http.transport import create_session
from jarvis.auth.jwt_utils import token_is_fresh
from jarvis.auth.profiles import (
    ArtifactRule,
    OAuthRefreshStrategy,
    ServiceProfile,
    register,
)

logger = logging.getLogger(__name__)

SERVICE = "viva-engage"
GRAPHQL_URL = "https://engage.cloud.microsoft/graphql"
_REQUIRED_ARTIFACT = "bearer_token"

PROFILE = register(
    ServiceProfile(
        service=SERVICE,
        start_url="https://engage.cloud.microsoft/",
        persistent_profile="default",
        wait_for_cookies=("office_access_token",),
        harvest_ls=False,
        artifacts=(
            ArtifactRule(name="bearer_token", source="cookie", key="office_access_token"),
            ArtifactRule(name="refresh_token", source="oauth", key="refresh_token", required=False),
        ),
        refresh_strategy=OAuthRefreshStrategy(
            token_endpoint="https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
            client_id="c1c74fed-04c9-4704-80dc-9f79a2e515cb",
            # The resource URI the Engage web client itself requests (captured live);
            # .default returns the granted set incl. access_as_user + engage_access.
            scope="https://www.yammer.com/.default",
            origin="https://engage.cloud.microsoft",
            artifact_name="bearer_token",
        ),
    )
)


class PersistedQueryStale(RuntimeError):
    """A persisted-query hash was rejected (``PersistedQueryNotFound``) and needs recapture."""


class EngageGraphQLError(RuntimeError):
    """The GraphQL operation failed: the response carries errors and no usable data."""


class EngageSession:
    """Holds the Engage Bearer token and issues authenticated, self-healing calls."""

    def __init__(self, credentials: ServiceCredentials):
        self._creds = credentials
        self._http = create_session()
        self._http.headers.update({"Accept": "application/json"})

    @classmethod
    def from_session(cls) -> EngageSession:
        """Load the Engage Bearer token from the shared browser-auth session."""
        return cls(ensure_credentials(SERVICE, required_artifact=_REQUIRED_ARTIFACT))

    # ── Token access ────────────────────────────────────────────────────

    def token(self) -> str | None:
        """The current Bearer token, if any."""
        return self._creds.get(_REQUIRED_ARTIFACT)

    def ensure(self) -> str:
        """A fresh Bearer, silently refreshing (no browser) when stale."""
        tok = self.token()
        if tok and token_is_fresh(tok):
            return tok
        self._reload()
        tok = self.token()
        if not tok:
            raise RuntimeError("No Engage token available after login attempt.")
        return tok

    def reauth(self) -> str:
        """Force a fresh browser SSO and return the refreshed Bearer token.

        Used on a live 401/403 - the credential was present but rejected.
        """
        self._reload(force_browser=True)
        return self.ensure()

    def _reload(self, *, force_browser: bool = False) -> None:
        self._creds = ensure_credentials(
            SERVICE, required_artifact=_REQUIRED_ARTIFACT, force_browser=force_browser
        )

    # ── Authenticated request ───────────────────────────────────────────

    def send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send an authenticated request, self-healing once on a live 401/403.

        Returns the response WITHOUT raising so callers keep their own status
        handling.
        """

        def _attempt() -> requests.Response:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {self.ensure()}"
            return self._http.request(method, url, headers=headers, **kwargs)

        resp = _attempt()
        if resp.status_code in (401, 403):
            logger.info("Engage returned %d - forcing re-auth and retrying once", resp.status_code)
            self.reauth()
            resp = _attempt()
        return resp

    # ── GraphQL ─────────────────────────────────────────────────────────

    def graphql(
        self,
        operation: str,
        query: str | None,
        sha256: str | None,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute one GraphQL operation and return its ``data`` object.

        Prefers a self-healing ad-hoc *query* document; falls back to the persisted
        *sha256* hash for operations the server gates to persisted mode. Raises
        :class:`PersistedQueryStale` when a hash is rejected.
        """
        if not query and not sha256:
            raise ValueError(f"Unknown operation: {operation}")

        payload: dict[str, Any] = {"operationName": operation, "variables": variables or {}}
        if query:
            payload["query"] = query
        else:
            payload["extensions"] = {"persistedQuery": {"version": 1, "sha256Hash": sha256}}

        resp = self.send(
            "POST",
            GRAPHQL_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        raise_for_status(resp)
        body = resp.json()
        errors = body.get("errors")
        data = body.get("data")
        if errors:
            if any(e.get("message") == "PersistedQueryNotFound" for e in errors):
                raise PersistedQueryStale(
                    f"Persisted query hash for '{operation}' is stale. Re-capture it from "
                    "Engage GraphQL traffic (web-capture skill), or add a self-healing "
                    "ad-hoc query document for it."
                )
            messages = sorted({str(e.get("message", "")) for e in errors})
            if not data:
                raise EngageGraphQLError(f"{operation} failed: {'; '.join(messages)}")
            # Partial result: some fields errored (e.g. per-item rate limiting) but the
            # operation returned usable data - surface the errors without discarding it.
            logger.warning("GraphQL partial errors for %s: %s", operation, messages)
        return data or {}
