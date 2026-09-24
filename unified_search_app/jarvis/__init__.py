# SPDX-FileCopyrightText: 2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""jarvis: unified auth, SharePoint, and Microsoft Teams client with one CLI.

Distributed as ``jarvis-bosch`` (the bare ``jarvis`` name is taken on PyPI);
imported as ``jarvis``. Subpackages:

    jarvis.auth        shared browser-auth, token cache, OAuth refresh
    jarvis.sharepoint  SharePoint / OneDrive REST client
    jarvis.teams       Microsoft Teams client + Trouter push core
    jarvis.testkit     dev-only test harness (ships behind the ``dev`` extra)

Flagship symbols are re-exported here for convenience; the subpackages remain
the canonical import paths. The re-exports resolve LAZILY (PEP 562): a bare
``import jarvis`` must not drag in Teams (or create any state directory) for
a caller that only wants ``jarvis.auth``. ``__version__`` comes from the
installed package metadata - the single version source is pyproject.toml.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # static resolution for type checkers; runtime stays lazy
    from .auth import ServiceCredentials, ensure_credentials
    from .teams import Client, TeamsSession

__all__ = [
    "Client",
    "ServiceCredentials",
    "TeamsSession",
    "ensure_credentials",
]

_EXPORTS = {
    "Client": ("jarvis.teams", "Client"),
    "TeamsSession": ("jarvis.teams", "TeamsSession"),
    "ServiceCredentials": ("jarvis.auth", "ServiceCredentials"),
    "ensure_credentials": ("jarvis.auth", "ensure_credentials"),
}


def _dist_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("jarvis-bosch")
    except PackageNotFoundError:  # running from a raw checkout without install
        return "0.0.0+uninstalled"


def __getattr__(name: str) -> Any:
    if name == "__version__":
        value: Any = _dist_version()
        globals()[name] = value
        return value
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'jarvis' has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value  # cache: subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS) | {"__version__"})
