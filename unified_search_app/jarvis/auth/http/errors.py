# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
"""Build full, human-readable error messages from failed HTTP responses.

Skills should surface the *complete* server error to stdout/stderr so the agent
(or user) can act on it — not just a status code, and not a wall of raw HTML.

``describe_http_error(resp)`` returns one readable line:
``GET https://host/api -> HTTP 401: <cleaned body>`` where the body is the
odata/OAuth error message when the response is JSON, or HTML stripped to plain
text otherwise. ``describe_urllib_error(err)`` does the same for a
``urllib.error.HTTPError`` (for the few skills still on urllib).

How to surface an HTTP failure — pick the shape by the raise site's ROLE, so the
whole repo stays consistent without breaking re-auth control flow:

  Rule 1 — non-terminal client layer whose CALLER catches ``HTTPError`` /
    inspects ``status_code`` to drive re-auth:
        from jarvis.auth.http.errors import raise_for_status
        raise_for_status(resp)        # raises requests.HTTPError + .response
    (Call this BEFORE streaming a body. Drop-in for resp.raise_for_status().)

  Rule 2 — terminal failure point that has ALREADY handled re-auth itself
    (status check + retry) and is now giving up:
        raise RuntimeError(describe_http_error(resp))

  Rule 3 — diagnostic that logs and continues (no raise):
        print(describe_http_error(resp), file=sys.stderr)
"""

from __future__ import annotations

import contextlib
import html
import json
import re
from typing import Any
from urllib.parse import urlsplit

import requests

_SCRIPT_STYLE = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1>")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def redact_url(url: str) -> str:
    """Drop the query string, fragment, and userinfo from a URL for display.

    Signed grants and tokens ride in query strings (``?token=...``,
    ``?tempauth=...``, ``?key=...``) - they must never reach a log line,
    exception message, or CLI error output. Every URL this module puts into
    an error message goes through here."""
    text = str(url or "")
    try:
        p = urlsplit(text)
    except ValueError:
        return text.split("?", 1)[0]
    if not p.scheme:
        return text.split("?", 1)[0]
    host = p.hostname or ""
    if p.port is not None:
        host = f"{host}:{p.port}"
    return f"{p.scheme}://{host}{p.path}"


def strip_html(text: str) -> str:
    """Reduce an HTML (or plain-text) blob to a single line of readable text."""
    if not text:
        return ""
    text = _SCRIPT_STYLE.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    return _WS.sub(" ", text).strip()


def _extract_json_error(data: Any) -> str:
    """Pull a human message out of common JSON error envelopes.

    Handles SharePoint/odata ``{"error": {"message": {"value": "..."}}}`` and
    ``{"error": {"message": "..."}}``, OAuth ``{"error": "...",
    "error_description": "..."}``, and a few generic shapes.
    """
    if isinstance(data, dict):
        err = data.get("error") or data.get("odata.error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, dict):
                return str(msg.get("value") or msg)
            if msg:
                return str(msg)
            return strip_html(json.dumps(err))
        if isinstance(err, str):
            desc = data.get("error_description")
            return f"{err}: {desc}" if desc else err
        for key in ("message", "error_description", "detail", "Message"):
            if data.get(key):
                return str(data[key])
        return strip_html(json.dumps(data))
    if isinstance(data, list):
        return "; ".join(str(x) for x in data)
    return str(data)


def describe_http_error(resp: Any, *, max_len: int = 2000) -> str:
    """Return a full, readable one-line description of a failed HTTP response.

    Includes method, URL, status, and a cleaned response body (JSON error
    message when available, else HTML stripped to plain text). Safe to call on
    any ``requests.Response``-like object; never raises.
    """
    try:
        status = getattr(resp, "status_code", "?")
        url = redact_url(getattr(resp, "url", "")) or "?"
        req = getattr(resp, "request", None)
        method = getattr(req, "method", None) or "?"
        ctype = ""
        with contextlib.suppress(Exception):
            ctype = (resp.headers.get("Content-Type", "") or "").lower()

        body = ""
        try:
            if "json" in ctype:
                body = _extract_json_error(resp.json())
            else:
                text = resp.text or ""
                # Some JSON APIs don't set a JSON content-type; try anyway.
                stripped = text.lstrip()
                if stripped[:1] in "{[":
                    try:
                        body = _extract_json_error(json.loads(text))
                    except Exception:
                        body = strip_html(text)
                else:
                    body = strip_html(text)
        except Exception:
            try:
                body = strip_html(resp.text or "")
            except Exception:
                body = ""

        body = body.strip()
        if len(body) > max_len:
            body = body[:max_len] + " …(truncated)"

        msg = f"{method} {url} -> HTTP {status}"
        return f"{msg}: {body}" if body else msg
    except Exception as exc:  # never let error formatting mask the real failure
        return f"HTTP error (could not format response: {exc})"


def raise_for_status(resp: Any) -> None:
    """Drop-in for ``resp.raise_for_status()`` that surfaces the full server body.

    Raises ``requests.HTTPError`` (so callers that ``except HTTPError`` or inspect
    ``e.response.status_code`` to drive re-auth keep working unchanged) but with
    ``describe_http_error(resp)`` as the message instead of the bare status line.

    Use this at non-terminal client layers (Rule 1). Call it BEFORE consuming a
    streamed body so the error body is still readable.
    """
    status = getattr(resp, "status_code", 0) or 0
    if status < 400:
        return
    raise requests.HTTPError(describe_http_error(resp), response=resp)


def describe_urllib_error(err: Any, *, max_len: int = 2000) -> str:
    """Like :func:`describe_http_error`, but for a ``urllib.error.HTTPError``.

    Produces output indistinguishable from the requests path so error text stays
    consistent across skills regardless of the HTTP library used.
    """
    try:
        status = getattr(err, "code", "?")
        url = redact_url(getattr(err, "url", None) or getattr(err, "filename", None) or "") or "?"
        text = ""
        try:
            raw = err.read()
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        except Exception:
            text = ""
        ctype = ""
        with contextlib.suppress(Exception):
            ctype = (err.headers.get("Content-Type", "") or "").lower()

        body = ""
        try:
            if text and ("json" in ctype or text.lstrip()[:1] in "{["):
                body = _extract_json_error(json.loads(text))
            else:
                body = strip_html(text)
        except Exception:
            body = strip_html(text)
        if not body:
            body = str(getattr(err, "reason", "") or "")

        body = body.strip()
        if len(body) > max_len:
            body = body[:max_len] + " …(truncated)"

        msg = f"{url} -> HTTP {status}"
        return f"{msg}: {body}" if body else msg
    except Exception as exc:
        return f"HTTP error (could not format urllib error: {exc})"
