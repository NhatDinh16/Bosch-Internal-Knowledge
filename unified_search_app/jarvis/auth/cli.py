# SPDX-FileCopyrightText: 2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
"""`jarvis auth` command group: manage the shared token cache.

`cache list` prints a human-readable table rather than JSON - it is meant to be
read, not parsed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import click

from .store import cache as token_cache


@click.group()
def auth() -> None:
    """Shared authentication and token-cache management."""


@auth.group()
def cache() -> None:
    """Manage the token cache."""


@cache.command("list")
@click.option("--service", default=None, help="Filter by service name")
@click.option("--type", "type_", default=None, help="Filter by token type")
@click.option("-a", "--all", "all_", is_flag=True, help="Include cookies in the output")
@click.option("-f", "--full", is_flag=True, help="Show full token names (no truncation)")
def cache_list(service: str | None, type_: str | None, all_: bool, full: bool) -> None:
    """List cached tokens."""
    tokens = token_cache.list_tokens(service=service, type=type_)
    if not all_:
        tokens = [t for t in tokens if t["type"] != "cookie"]
    if not tokens:
        click.echo("No tokens found.")
        return

    rows = []
    for entry in tokens:
        name = entry["name"]
        if not full and len(name) > 40:
            name = name[:37] + "..."
        key = f"{entry['service']}/{entry['type']}/{entry['domain']}/{name}"
        if entry.get("expired"):
            status = "EXPIRED"
        elif "remaining_min" in entry:
            status = f"{entry['remaining_min']:.0f} min left"
        else:
            status = "no expiry"
        updated = datetime.fromtimestamp(entry["updated_at"], tz=UTC).strftime("%Y-%m-%d %H:%M")
        rows.append((key, status, updated))

    status_w = max(len(r[1]) for r in rows)
    for key, status, updated in rows:
        click.echo(f"{updated}  {status:<{status_w}}  {key}")
