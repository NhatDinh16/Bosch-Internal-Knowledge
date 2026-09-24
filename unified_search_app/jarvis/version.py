# SPDX-FileCopyrightText: 2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""The version the running CLI actually is.

An editable dev install freezes its version at install time: uv writes the dist metadata
once, and a later ``version =`` bump in the checkout's pyproject.toml never reaches it.
A stale number is not cosmetic - it makes ``--version`` lie, tells the update check that
a release is newer than the very tree it was cut from (so a dev box is advised to install
a release over its own checkout), and skews the usage ping.

So everything user-facing derives the version from the CHECKOUT when the install is
editable, and from the dist metadata otherwise. The metadata number stays available as
:func:`dist_version` for the one place that needs to talk about the install itself.
"""

from __future__ import annotations

import json
import re
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import click

DIST_NAME = "jarvis-bosch"


def uri_to_path(uri: str) -> Path | None:
    """The local path behind a ``file:`` URI, or None for anything else.

    VS Code and pip both percent-encode the drive letter's colon
    (``file:///c%3A/Users/<name>/...``), which is why the path is unquoted before the
    drive letter is recognised.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if parsed.netloc:  # UNC share: file://server/share/...
        return Path(f"//{parsed.netloc}{path}")
    # A drive-letter URI carries a leading slash that is not part of the path.
    return Path(path[1:] if re.match(r"^/[A-Za-z]:", path) else path)


def dist_version() -> str:
    """The version recorded in the installed distribution's metadata.

    For an editable install this is the version as of the last
    ``uv tool install --editable``, which is NOT necessarily what the code does now.
    """
    try:
        return metadata.version(DIST_NAME)
    except metadata.PackageNotFoundError:
        return "0.0.0"


def editable_checkout() -> Path | None:
    """The checkout an editable install points at, or None for a normal install.

    Reads the distribution's PEP 610 ``direct_url.json``: an editable install records
    ``dir_info.editable`` true and the checkout in ``url``.
    """
    try:
        raw = metadata.distribution(DIST_NAME).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    dir_info = data.get("dir_info")
    if not (isinstance(dir_info, dict) and dir_info.get("editable")):
        return None
    url = data.get("url")
    return uri_to_path(url) if isinstance(url, str) else None


def source_version(checkout: Path) -> str | None:
    """``[project] version`` from a checkout's pyproject.toml, or None if unreadable."""
    try:
        with (checkout / "pyproject.toml").open("rb") as handle:
            data: Any = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    return version if isinstance(version, str) and version else None


def effective_version() -> str:
    """The version of the code that is actually running.

    The checkout's version for an editable install, the dist metadata otherwise. Falls
    back to the metadata if the checkout has gone away or its pyproject cannot be read.
    """
    checkout = editable_checkout()
    if checkout is not None:
        from_source = source_version(checkout)
        if from_source:
            return from_source
    return dist_version()


def _print_version(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(f"{ctx.find_root().info_name}, version {effective_version()}")
    ctx.exit()


def version_option() -> Any:
    """``--version`` reporting :func:`effective_version`.

    Replaces ``click.version_option(package_name=...)``, which reads the dist metadata
    and therefore reports a stale number on a dev box. Resolved lazily, only when the
    flag is actually passed.
    """
    return click.option(
        "--version",
        is_flag=True,
        expose_value=False,
        is_eager=True,
        callback=_print_version,
        help="Show the version and exit.",
    )
