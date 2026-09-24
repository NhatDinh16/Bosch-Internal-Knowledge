"""Shared authentication and token-cache layer for jarvis tools.

Public API::

    from jarvis.auth import ensure_credentials

    creds = ensure_credentials("teams")
    token = creds.get("graph_token")
"""

from .auth import ServiceCredentials, ensure_credentials
from .jwt_utils import decode_jwt_payload, jwt_expiry, token_is_fresh

__all__ = [
    "ServiceCredentials",
    "decode_jwt_payload",
    "ensure_credentials",
    "jwt_expiry",
    "token_is_fresh",
]
