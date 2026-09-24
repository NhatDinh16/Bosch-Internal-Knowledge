# SPDX-FileCopyrightText: 2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
"""Reusable Bearer-token request wrapper with one-shot reauthentication."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import requests


def bearer_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    token: Callable[[], str],
    reauthenticate: Callable[[], str],
    headers: dict[str, str] | None = None,
    auth_header: str = "Authorization",
    prefix: str = "Bearer ",
    retry_statuses: tuple[int, ...] = (401, 403),
    should_retry: Callable[[requests.Response], bool] | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Send an authenticated request and retry once after reauthentication.

    ``token`` supplies the current credential; it is stamped as ``auth_header``
    with ``prefix`` in front (``"Authorization: Bearer <token>"`` by default, but
    e.g. ``auth_header="NM-Authorization", prefix=""`` for a raw-token header).
    ``reauthenticate`` must refresh the credential and return the replacement
    value.

    A retry happens iff the response status is in ``retry_statuses`` AND, when a
    ``should_retry`` predicate is given, ``should_retry(response)`` is true. The
    predicate lets a caller inspect the response body (e.g. distinguish a stale
    session from a permission denial that share a status code); it is consulted
    only for a retry-status response, never on success. Without it, every
    ``retry_statuses`` response triggers the one reauth+retry.

    The caller retains status handling; even the second failing response is
    returned unchanged.
    """

    base_headers = dict(headers or {})

    def _attempt(value: str) -> requests.Response:
        attempt_headers = dict(base_headers)
        attempt_headers[auth_header] = f"{prefix}{value}"
        return session.request(method, url, headers=attempt_headers, **kwargs)

    response = _attempt(token())
    if response.status_code in retry_statuses and (should_retry is None or should_retry(response)):
        response = _attempt(reauthenticate())
    return response
