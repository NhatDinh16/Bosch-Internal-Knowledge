# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""`jarvis engage` command group: read and write Microsoft Viva Engage (Yammer).

Reads: ``whoami``, ``feed`` (home / community), ``thread``, ``inbox``, ``search``,
``communities`` (mine / official / suggested / sidebar / info / members / search),
``notifications``. Writes: ``post`` (community / storyline), ``reply``, ``react``.
Every subcommand prints one JSON object: ``{"status": "ok", ...}`` on success,
``{"status": "error", ...}`` on failure (exit 1).
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import click

from jarvis import outcome
from jarvis.debughook import print_debug_traceback
from jarvis.version import version_option

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")


def _emit(obj: dict[str, Any]) -> None:
    print_debug_traceback()
    click.echo(json.dumps(obj, indent=2, ensure_ascii=False))


def _client():
    from .client import EngageClient

    return EngageClient()


# ── Handlers ──────────────────────────────────────────────────────────────


def _cmd_whoami(ns) -> dict[str, Any]:
    return {"user": _client().get_current_user()}


def _cmd_feed(ns) -> dict[str, Any]:
    client = _client()
    if ns.community:
        # The ad-hoc community-feed field exposes no count/sort/cursor arguments, so
        # these flags cannot be applied - surface that instead of silently ignoring.
        warnings = [
            f"--{name} is not applied to a community feed (the API's ad-hoc field "
            "exposes no such argument; the server default applies)"
            for name, value in (("count", ns.count), ("sort", ns.sort), ("cursor", ns.cursor))
            if value is not None
        ]
        result = client.get_community_feed(
            ns.community,
            count=ns.count if ns.count is not None else 10,
            sort_by=ns.sort or "CREATED_AT",
            older_than=ns.cursor,
        )
        if warnings:
            result["warnings"] = warnings
        return result
    if ns.sort is not None:
        raise outcome.UsageProblem("--sort applies only to a community feed (use with --community)")
    return client.get_home_feed(
        count=ns.count if ns.count is not None else 10, older_than=ns.cursor
    )


def _cmd_thread(ns) -> dict[str, Any]:
    return {"thread": _client().get_thread(ns.thread_id, reply_count=ns.replies)}


def _cmd_inbox(ns) -> dict[str, Any]:
    client = _client()
    if ns.count_only:
        return {"unreadCount": client.get_inbox_unread_count()}
    inbox_type = "ALL" if ns.all else "UNREAD"
    result = client.get_inbox(count=ns.count, inbox_type=inbox_type)
    if ns.count > 20:
        result["warnings"] = ["--count is capped at 20 for the inbox (API page maximum)"]
    return result


def _cmd_search(ns) -> dict[str, Any]:
    result = _client().search(ns.query, count=ns.count)
    return {"query": ns.query, **result}


def _cmd_communities(ns) -> dict[str, Any]:
    client = _client()
    if ns.info:
        return {"community": client.get_community_info(ns.info)}
    if ns.members:
        out: dict[str, Any] = {"members": client.get_community_members(ns.members, role=ns.role)}
        if ns.role != "ALL":
            # The ad-hoc members field applies no server-side role filter.
            out["warnings"] = [
                "--role is not applied by the API's ad-hoc field; "
                "the full member list (first 50) is returned"
            ]
        return out
    if ns.search:
        return {"communities": client.search_groups(ns.search)}
    if ns.official:
        return {"communities": client.get_official_communities(count=ns.count)}
    if ns.suggested:
        return {"communities": client.get_suggested_communities(count=ns.count)}
    if ns.sidebar:
        return client.get_navigation_groups()
    return {"communities": client.get_my_communities(count=ns.count)}


def _cmd_notifications(ns) -> dict[str, Any]:
    return {"notifications": _client().get_notifications(count=ns.count)}


def _cmd_post(ns) -> dict[str, Any]:
    if ns.storyline and ns.announcement:
        raise outcome.UsageProblem("--announcement is not supported for storyline posts")
    client = _client()
    if ns.storyline:
        return client.post_to_storyline(
            text=ns.message, is_question=ns.question, topic_names=list(ns.topic) or None
        )
    return client.post_message(
        group_id=ns.community,
        text=ns.message,
        is_announcement=ns.announcement,
        is_question=ns.question,
        topic_names=list(ns.topic) or None,
    )


def _cmd_reply(ns) -> dict[str, Any]:
    return _client().reply_to_thread(message_id=ns.message_id, text=ns.message)


def _cmd_react(ns) -> dict[str, Any]:
    return _client().react_to_message(message_id=ns.message_id, reaction=ns.reaction)


def _run(handler, **opts: Any) -> None:
    ns = SimpleNamespace(**opts)
    try:
        payload = handler(ns)
    except Exception as e:
        _emit({"status": "error", "error": str(e), "type": type(e).__name__})
        raise click.exceptions.Exit(outcome.fail(e)) from None
    _emit({"status": "ok", **payload})
    raise click.exceptions.Exit(0)


# ── Commands ──────────────────────────────────────────────────────────────


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@version_option()
def engage() -> None:
    """Microsoft Viva Engage (Yammer) - feeds, threads, communities, posts."""


@engage.command("whoami", short_help="Show the authenticated Engage user")
def engage_whoami() -> None:
    """Print the current user's Engage profile (a read-only auth probe)."""
    _run(_cmd_whoami)


@engage.command("feed", short_help="Read the home feed or a community feed")
@click.option("--community", metavar="ID", help="Community node id or database id (omit for home).")
@click.option(
    "--count",
    type=int,
    default=None,
    help="Number of threads (home feed only, default 10, paginated transparently; "
    "a community feed returns the server default page - API limitation, warned).",
)
@click.option(
    "--sort",
    type=click.Choice(["CREATED_AT", "LAST_UPDATED"]),
    default=None,
    help="Community feed sort preference; not applied by the API's ad-hoc field "
    "(server default CREATED_AT - limitation, warned).",
)
@click.option(
    "--cursor",
    help="Pagination cursor (home feed only; endCursor from a previous response - "
    "not applied to a community feed).",
)
def engage_feed(**opts: Any) -> None:
    """Read the home feed (default) or a community feed (--community).

    Community feeds return the server's default page and order: the API's ad-hoc
    field exposes no count/sort/cursor arguments, so those flags trigger a warning
    in the output instead of taking effect.
    """
    _run(_cmd_feed, **opts)


@engage.command("thread", short_help="Read a thread with replies")
@click.argument("thread_id")
@click.option(
    "--replies", type=int, default=20, show_default=True, help="Replies to fetch (API max 20)."
)
def engage_thread(**opts: Any) -> None:
    """Read THREAD_ID (node id or database id) with its replies."""
    _run(_cmd_thread, **opts)


@engage.command("inbox", short_help="Read the inbox")
@click.option("--all", "all", is_flag=True, help="Show all inbox threads (default: unread only).")
@click.option("--count-only", "count_only", is_flag=True, help="Only the unread thread count.")
@click.option("--count", type=int, default=10, show_default=True, help="Number of threads.")
def engage_inbox(**opts: Any) -> None:
    """Read inbox threads (unread by default), or just the unread count."""
    _run(_cmd_inbox, **opts)


@engage.command("search", short_help="Search threads by text")
@click.argument("query")
@click.option("--count", type=int, default=10, show_default=True, help="Max results.")
def engage_search(**opts: Any) -> None:
    """Search threads for QUERY (use `communities --search` for community search)."""
    _run(_cmd_search, **opts)


@engage.command("communities", short_help="List and inspect communities")
@click.option("--official", is_flag=True, help="List official (company-endorsed) communities.")
@click.option("--suggested", is_flag=True, help="List suggested communities.")
@click.option("--sidebar", is_flag=True, help="Sidebar navigation list (joined + favorites).")
@click.option("--info", metavar="ID", help="Detailed info about one community.")
@click.option("--members", metavar="ID", help="List members of one community (fixed page of 50).")
@click.option("--search", metavar="TEXT", help="Search communities by name.")
@click.option(
    "--role",
    type=click.Choice(["ALL", "ADMIN", "MEMBER"]),
    default="ALL",
    show_default=True,
    help="Role filter preference for --members; not applied by the API's ad-hoc "
    "field (full list returned - limitation, warned).",
)
@click.option("--count", type=int, default=20, show_default=True, help="Max results.")
def engage_communities(**opts: Any) -> None:
    """List your communities (default) or use a flag to pick another view."""
    _run(_cmd_communities, **opts)


@engage.command("notifications", short_help="Read notifications")
@click.option(
    "--count",
    type=int,
    default=10,
    show_default=True,
    help="Max notifications returned (enforced client-side; the API returns its default page).",
)
def engage_notifications(**opts: Any) -> None:
    """Read recent notifications (at most --count, truncated client-side)."""
    _run(_cmd_notifications, **opts)


@engage.command("post", short_help="Post a message to a community or your Storyline")
@click.option("--community", metavar="ID", help="Community node id or database id.")
@click.option("--storyline", is_flag=True, help="Post to your own Storyline feed instead.")
@click.option("--message", required=True, help="Message text ('- ' lines become list items).")
@click.option("--announcement", is_flag=True, help="Post as an announcement (communities only).")
@click.option("--question", is_flag=True, help="Post as a question.")
@click.option("--topic", multiple=True, metavar="NAME", help="Add a topic/hashtag (repeatable).")
def engage_post(**opts: Any) -> None:
    """Post a new message. Exactly one of --community / --storyline is required."""
    if bool(opts.get("community")) == bool(opts.get("storyline")):
        _emit(
            {
                "status": "error",
                "error": "provide exactly one of --community or --storyline",
                "type": "UsageError",
            }
        )
        raise click.exceptions.Exit(outcome.fail_with(outcome.BAD_ARGUMENT))
    _run(_cmd_post, **opts)


@engage.command("reply", short_help="Reply to a thread")
@click.option(
    "--message-id", "message_id", required=True, metavar="ID", help="Message to reply to."
)
@click.option("--message", required=True, help="Reply text.")
def engage_reply(**opts: Any) -> None:
    """Reply to a thread by the id of the message replied to (usually the starter)."""
    _run(_cmd_reply, **opts)


@engage.command("react", short_help="React to (like) a message")
@click.option(
    "--message-id", "message_id", required=True, metavar="ID", help="Message to react to."
)
@click.option(
    "--reaction", default="LIKE", show_default=True, help="Reaction type (LIKE verified)."
)
def engage_react(**opts: Any) -> None:
    """React to a message. Only LIKE is verified against live traffic."""
    _run(_cmd_react, **opts)
