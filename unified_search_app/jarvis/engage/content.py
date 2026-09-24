# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""Viva Engage entity-id, Draft.js, and response-formatting helpers.

Pure functions with no network or auth: GraphQL node ids are base64 of
``{"_type":<T>,"id":<numeric>}``; message text is a Draft.js
``serializedContentState`` blob. These convert ids both ways, extract plain text
and titles, build a Draft.js payload for writes, and shape raw GraphQL nodes into
clean dicts.
"""

from __future__ import annotations

import base64
import json
from typing import Any

# ── ID encoding/decoding ─────────────────────────────────────────────────────


def encode_id(type_name: str, database_id: str) -> str:
    """Encode a database id into a GraphQL node id (unpadded base64).

    >>> encode_id("Group", "12345678901")
    'eyJfdHlwZSI6Ikdyb3VwIiwiaWQiOiIxMjM0NTY3ODkwMSJ9'
    """
    raw = json.dumps({"_type": type_name, "id": str(database_id)}, separators=(",", ":"))
    return base64.b64encode(raw.encode()).decode().rstrip("=")


def decode_id(node_id: str) -> dict[str, str]:
    """Decode a GraphQL node id to its type and database id.

    Raises ``ValueError`` for anything that is not a base64-encoded node id, so a
    malformed id surfaces as a specific structured error instead of a raw
    decode exception.

    >>> decode_id('eyJfdHlwZSI6Ikdyb3VwIiwiaWQiOiIxMjM0NTY3ODkwMSJ9')
    {'_type': 'Group', 'id': '12345678901'}
    """
    padded = node_id + "=" * (-len(node_id) % 4)
    try:
        decoded = json.loads(base64.b64decode(padded, validate=True))
    except (ValueError, UnicodeDecodeError) as e:  # binascii.Error is a ValueError
        raise ValueError(f"invalid node id or database id: {node_id!r}") from e
    if not isinstance(decoded, dict):
        raise ValueError(f"invalid node id or database id: {node_id!r}")
    return decoded


def to_node_id(type_name: str, value: str) -> str:
    """Return a node id, encoding *value* as *type_name* if it is a bare numeric id."""
    return encode_id(type_name, value) if value.isdigit() else value


# ── Draft.js content ─────────────────────────────────────────────────────────


def _parse_draftjs(scs: str) -> str:
    """Parse a Draft.js serializedContentState JSON string to plain text."""
    if not scs:
        return ""
    try:
        cs = json.loads(scs)
        blocks = cs.get("blocks", [])
        return "\n".join(b.get("text", "") for b in blocks)
    except (json.JSONDecodeError, TypeError):
        return ""


def _extract(message: dict[str, Any], field: str) -> str:
    """Plain text of *field* on a message node, trying both response shapes.

    Feed/thread queries nest under ``languageSpecificContent``; search and
    notification queries put it directly on the node.
    """
    lsc = message.get("languageSpecificContent")
    if isinstance(lsc, dict):
        inner = lsc.get(field)
        if isinstance(inner, dict):
            text = _parse_draftjs(inner.get("serializedContentState", ""))
            if text:
                return text
    inner = message.get(field)
    if isinstance(inner, dict):
        text = _parse_draftjs(inner.get("serializedContentState", ""))
        if text:
            return text
    return ""


def extract_plain_text(message: dict[str, Any]) -> str:
    """Extract the plain-text body from a message node."""
    return _extract(message, "body")


def extract_title(message: dict[str, Any]) -> str:
    """Extract the plain-text title from a message node (empty for most posts)."""
    return _extract(message, "title")


def build_draftjs_content(text: str) -> str:
    """Build a Draft.js serializedContentState JSON string from plain text.

    Each line becomes a block. Lines starting with ``- `` or ``* `` become
    ``unordered-list-item`` blocks; everything else is ``unstyled``.
    """
    blocks = []
    for i, line in enumerate(text.split("\n")):
        block_type = "unstyled"
        block_text = line
        if line.startswith("- ") or line.startswith("* "):
            block_type = "unordered-list-item"
            block_text = line[2:]
        blocks.append(
            {
                "key": f"{i:05x}",
                "text": block_text,
                "type": block_type,
                "depth": 0,
                "inlineStyleRanges": [],
                "entityRanges": [],
                "data": {},
            }
        )
    return json.dumps({"blocks": blocks, "entityMap": {}}, separators=(",", ":"))


# ── Node formatters ──────────────────────────────────────────────────────────


def format_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Shape a raw GraphQL message node into a clean dict (empty fields stripped)."""
    sender = msg.get("sender") or {}
    reactions = msg.get("reactionsConnection") or {}
    result = {
        "id": msg.get("id"),
        "sender": sender.get("displayName"),
        "senderEmail": sender.get("email"),
        "createdAt": msg.get("createdAt"),
        "text": extract_plain_text(msg),
        "title": extract_title(msg),
        "isEdited": msg.get("isEdited", False),
        "isQuestion": msg.get("isQuestion", False),
        "isArticle": msg.get("isArticle", False),
        "reactionsTotal": reactions.get("totalCount", 0),
        "likeCount": reactions.get("likeCount", 0),
    }
    return {k: v for k, v in result.items() if v}


def format_thread(thread: dict[str, Any] | None) -> dict[str, Any]:
    """Shape a raw GraphQL thread node into a clean dict.

    ``None`` (a query rejected by the API, e.g. bad args) yields a structured error
    rather than crashing the caller.
    """
    if not thread:
        return {"error": "thread not found or query rejected by the API"}
    starter = thread.get("threadStarter") or {}
    group = thread.get("group") or {}
    topics = thread.get("topics") or {}
    topic_edges = topics.get("edges", []) if isinstance(topics, dict) else []

    result: dict[str, Any] = {
        "threadId": thread.get("id"),
        "databaseId": thread.get("databaseId") or thread.get("telemetryId"),
        "group": group.get("displayName") if group else None,
        "groupId": group.get("id") if group else None,
        "createdAt": thread.get("createdAt"),
        "updatedAt": thread.get("updatedAt"),
        "seenByCount": thread.get("seenByCount"),
        "isAnnouncement": thread.get("isAnnouncement"),
        "isClosed": thread.get("isClosed"),
        "isDirectMessage": thread.get("isDirectMessage"),
        "hasUnread": thread.get("viewerHasUnreadMessages"),
        "topics": [e.get("node", {}).get("name", "") for e in topic_edges if e.get("node")],
        "starter": format_message(starter),
    }

    replies_data = thread.get("topLevelReplies") or {}
    reply_edges = replies_data.get("edges", [])
    if reply_edges:
        result["replies"] = [format_message(e["node"]) for e in reply_edges if e.get("node")]
        result["repliesPageInfo"] = replies_data.get("pageInfo")

    attachments = starter.get("attachments") or {}
    att_edges = attachments.get("edges", []) if isinstance(attachments, dict) else []
    if att_edges:
        result["attachments"] = [
            {
                "name": (a.get("node") or {}).get("name"),
                "type": (a.get("node") or {}).get("__typename"),
            }
            for a in att_edges
        ]

    return {k: v for k, v in result.items() if v}


def format_group(group: dict[str, Any]) -> dict[str, Any]:
    """Shape a raw GraphQL group node into a clean dict."""
    return {
        "groupId": group.get("id"),
        "databaseId": group.get("databaseId"),
        "name": group.get("displayName"),
        "description": group.get("description"),
        "privacy": group.get("privacy"),
        "state": group.get("state"),
        "isOfficial": group.get("isOfficial"),
        "isExternal": group.get("isExternal"),
        "membershipStatus": group.get("viewerMembershipStatus"),
        "isAdmin": group.get("viewerIsAdmin"),
        "canPost": group.get("viewerCanStartThread"),
    }
