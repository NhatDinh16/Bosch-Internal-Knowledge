# SPDX-FileCopyrightText: 2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
"""jarvis.auth.browser: Playwright-based SSO login and token harvesting.

    session      the browser session engine + ensure_auth / ensure_service_auth
    results      AuthResult / ServiceAuthResult + artifact resolution
    harvest_cache  the bridge to the token store
    login_wall   recognises an app's sign-in wall and starts its SSO

Callers use ``ensure_service_auth`` (via ``jarvis.auth.ensure_credentials``);
the rest is internal.
"""

from .results import AuthResult, ServiceAuthResult, normalize_artifacts
from .session import (
    BrowserOperationTimeout,
    OAuthAccessTokenUnavailable,
    ensure_auth,
    ensure_service_auth,
)

__all__ = [
    "AuthResult",
    "BrowserOperationTimeout",
    "OAuthAccessTokenUnavailable",
    "ServiceAuthResult",
    "ensure_auth",
    "ensure_service_auth",
    "normalize_artifacts",
]
