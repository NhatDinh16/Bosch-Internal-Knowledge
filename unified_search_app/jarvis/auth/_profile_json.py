# SPDX-FileCopyrightText: 2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
"""Build a ServiceProfile from a declarative ``auth.json``.

A module that authenticates ships an ``auth.json`` next to its code instead of a
Python ``auth_profile.py`` that registers on import. The JSON mirrors the
ServiceProfile fields; ``wait_for_url`` is a regex string compiled here, and
``refresh_strategy`` / ``extra_refresh_strategies`` carry a ``type`` discriminator
(``oauth`` | ``cookie_exchange`` | ``sapisidhash``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .profiles import (
    ArtifactRule,
    CookieExchangeStrategy,
    OAuthRefreshStrategy,
    SapisidhashStrategy,
    ServiceProfile,
)

_STRATEGY_TYPES = {
    "oauth": OAuthRefreshStrategy,
    "cookie_exchange": CookieExchangeStrategy,
    "sapisidhash": SapisidhashStrategy,
}


def _strategy(data: dict[str, Any] | None):
    if not data:
        return None
    kind = str(data.get("type", ""))
    cls = _STRATEGY_TYPES.get(kind)
    if cls is None:
        raise ValueError(
            f"auth.json: unknown refresh strategy type {kind!r}; "
            f"expected one of {', '.join(sorted(_STRATEGY_TYPES))}"
        )
    fields = {k: v for k, v in data.items() if k != "type"}
    if cls is CookieExchangeStrategy and "extra_headers" in fields:
        # JSON gives a list of [name, value] pairs; the dataclass wants a tuple of tuples.
        fields["extra_headers"] = tuple(tuple(pair) for pair in fields["extra_headers"])
    return cls(**fields)


def profile_from_dict(data: dict[str, Any]) -> ServiceProfile:
    """Build a ServiceProfile from a parsed auth.json mapping."""
    wait_for_url = data.get("wait_for_url")
    if isinstance(wait_for_url, str):
        wait_for_url = re.compile(wait_for_url)

    artifacts = tuple(
        ArtifactRule(
            name=a["name"],
            source=a["source"],
            key=a["key"],
            required=a.get("required", True),
            domain=a.get("domain"),
        )
        for a in data.get("artifacts", ())
    )

    return ServiceProfile(
        service=data["service"],
        start_url=data["start_url"],
        wait_for_url=wait_for_url,
        wait_for_cookies=tuple(data.get("wait_for_cookies", ())),
        wait_for_cookies_mode=data.get("wait_for_cookies_mode", "all"),
        wait_for_idle=data.get("wait_for_idle", 5.0),
        harvest_ls=data.get("harvest_ls", True),
        persistent_profile=data.get("persistent_profile"),
        extra_ls_origins=tuple(data.get("extra_ls_origins", ())),
        browser_args=tuple(data.get("browser_args", ())),
        artifacts=artifacts,
        refresh_strategy=_strategy(data.get("refresh_strategy")),
        extra_refresh_strategies=tuple(
            _strategy(s) for s in data.get("extra_refresh_strategies", ())
        ),
        oauth_audience_contains=data.get("oauth_audience_contains"),
    )


def profile_from_json(path: Path | str) -> ServiceProfile:
    """Load + build a ServiceProfile from an auth.json file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return profile_from_dict(data)
