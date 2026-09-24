# SPDX-FileCopyrightText: 2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""jarvis-engage: Microsoft Viva Engage (Yammer) GraphQL client.

A cookie/Bearer-authenticated client over the single Engage GraphQL endpoint: read
feeds, threads, the inbox, communities, notifications, and search; post to a community
or Storyline, reply, and react. Authentication is the shared browser-auth flow in
:mod:`jarvis.engage.session` (the ``office_access_token`` cookie plus an OAuth
refresh-token grant) - no PATs or app registrations.
"""

from .client import EngageClient
from .content import decode_id, encode_id
from .session import EngageSession

__all__ = [
    "EngageClient",
    "EngageSession",
    "decode_id",
    "encode_id",
]
