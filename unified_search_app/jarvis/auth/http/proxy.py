# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
"""Corporate proxy bypass for localhost connections.

Prevents the Bosch corporate proxy (Zscaler) from intercepting
localhost connections (e.g. WebSocket or HTTP to local services).
"""

from __future__ import annotations

import os
import urllib.request


def apply_proxy_bypass() -> None:
    """Fix corporate proxy intercepting localhost connections.

    - Adds 127.0.0.1 to NO_PROXY env var (fixes websockets lib)
    - Installs empty ProxyHandler to bypass urllib proxy detection

    Call restore_proxy_defaults() after the browser session to re-enable
    the system proxy for subsequent urllib.request calls.
    """
    no_proxy = os.environ.get("NO_PROXY", "")
    if "127.0.0.1" not in no_proxy:
        os.environ["NO_PROXY"] = no_proxy + ",127.0.0.1" if no_proxy else "127.0.0.1"
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))


def restore_proxy_defaults() -> None:
    """Re-enable the system proxy after a browser session.

    apply_proxy_bypass() installs an empty ProxyHandler that disables
    *all* proxy handling for urllib.request -- including the corporate
    proxy needed to reach external hosts.  Calling this restores the
    default opener so subsequent urllib.request calls use the system
    proxy again.
    """
    urllib.request.install_opener(None)
