# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""The domain-aware cookie jar - the single place skills filter cookies.

The shared browser-auth flow harvests the WHOLE SSO session (50-100+ cookies
from Office/Teams/SharePoint/AAD/…). Sending that entire jar to one target host
blows the ``Cookie`` request header past common gateway/servlet limits (Azure
Application Gateway ~8-16 KB, Apache ``LimitRequestFieldSize`` ~8 KB, Tomcat)
→ ``400 Bad Request`` / ``400 Request Header Or Cookie Too Large``. It is also a
needless credential leak: a cookie set by host A has no business riding along to
unrelated host B.

The first-class cookie identity is the browser's own triple
``(domain, path, name)`` (RFC 6265). The previous model - a flat
``name → value`` dict plus a separate ``name → domain`` map - collapsed
same-named cookies across domains (``google.com`` vs ``google.de`` login
cookies) BEFORE any scoping could run, so the wrong value could win and the
loss was unrecoverable downstream. :class:`CookieJar` keeps every harvested
``(domain, path, name)`` entry and consumers pick the send-jar per target:

- :meth:`CookieJar.for_url` - the full browser send rule: domain match at a
  dot boundary AND cookie-path prefix match. Used where a request URL is in
  hand (:class:`jarvis.auth.auth_client.AuthedClient`).
- :meth:`CookieJar.for_host` - the host-level view (all paths) for consumers
  that pin one jar onto a whole per-host session.
- :func:`select` - narrow a jar by an explicit allowlist / predicate, for the
  residual case where the target host itself shares the SSO domain and
  domain-scoping alone is not enough (e.g. IBM Jazz on ``*.bosch.com`` sitting
  next to M365 cookies on the same parent domain).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# A cookie selector understood by :func:`select` (and AuthedClient.send_cookies):
#   True  -> keep all           False -> keep none
#   iterable of names -> allowlist       callable -> custom predicate over the jar
CookieSelector = bool | Iterable[str] | Callable[[dict[str, str]], Mapping[str, str]]


def _norm_domain(domain: str | None) -> str:
    return (domain or "").lstrip(".").lower()


def _norm_path(path: str | None) -> str:
    p = (path or "/").strip()
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > 1:
        p = p.rstrip("/")
    return p


def _path_matches(cookie_path: str, request_path: str) -> bool:
    """RFC 6265 5.1.4 path-match: equal, or prefix at a ``/`` boundary."""
    if cookie_path == "/" or cookie_path == request_path:
        return True
    return request_path.startswith(cookie_path) and (
        cookie_path.endswith("/") or request_path[len(cookie_path)] == "/"
    )


@dataclass(frozen=True)
class CookieEntry:
    """One harvested cookie: ``(domain, path, name) → value``.

    ``domain`` is normalized (lowercase, no leading dot). An empty domain means
    the provenance is unknown (a pre-domain-tracking cache row); such a cookie
    is sent to every host (fail open) but always loses a name clash against a
    cookie with a real domain. ``path`` defaults to ``"/"`` - two same-named
    cookies with different paths coexist, exactly like in a browser store.

    ``expires_at`` is a harvest attribute (not part of the identity, so it
    never affects dedup or matching); it rides along so the harvest jar can
    persist each cookie with its own expiry.
    """

    domain: str
    name: str
    value: str
    path: str = "/"
    expires_at: float | None = field(default=None, compare=False)


class CookieJar:
    """Immutable, browser-fidelity cookie collection keyed on ``(domain, path, name)``."""

    __slots__ = ("_entries",)

    def __init__(
        self,
        entries: Iterable[CookieEntry | tuple] = (),
    ):
        """Accepts CookieEntry objects or ``(domain, name, value[, path[,
        expires_at]])`` tuples (path defaults to ``"/"``). A CookieEntry's
        ``expires_at`` is preserved through normalization."""
        seen: dict[tuple[str, str, str], CookieEntry] = {}
        for e in entries:
            if not isinstance(e, CookieEntry):
                domain, name, value = e[0], e[1], e[2]
                path = e[3] if len(e) > 3 else "/"
                expires_at = e[4] if len(e) > 4 else None
                e = CookieEntry(
                    _norm_domain(domain), str(name), str(value), _norm_path(path), expires_at
                )
            else:
                e = CookieEntry(
                    _norm_domain(e.domain), e.name, e.value, _norm_path(e.path), e.expires_at
                )
            if e.name and e.value:
                seen[(e.domain, e.path, e.name)] = e  # same identity: last wins
        self._entries: tuple[CookieEntry, ...] = tuple(seen.values())

    @classmethod
    def from_flat(
        cls,
        cookies: Mapping[str, str],
        domains: Mapping[str, str] | None = None,
    ) -> CookieJar:
        """Build a jar from the legacy flat shapes (tests, ad-hoc callers)."""
        domains = domains or {}
        return cls((domains.get(name, ""), name, value) for name, value in cookies.items())

    def entries(self) -> tuple[CookieEntry, ...]:
        return self._entries

    def names(self) -> set[str]:
        return {e.name for e in self._entries}

    def get(self, name: str, *, domain: str | None = None, path: str | None = None) -> str | None:
        """Exact lookup by name (optionally pinned to one harvested domain/path)."""
        want_domain = _norm_domain(domain) if domain is not None else None
        want_path = _norm_path(path) if path is not None else None
        for e in self._entries:
            if (
                e.name == name
                and (want_domain is None or e.domain == want_domain)
                and (want_path is None or e.path == want_path)
            ):
                return e.value
        return None

    def resolve(self, name: str, *, domain: str | None = None) -> CookieEntry | None:
        """Artifact lookup: the entry for *name* where the harvested domain
        equals *domain* or is a subdomain of it (the rule's domain is the
        PARENT, e.g. rule ``google.com`` matches a cookie on
        ``accounts.google.com``). Prefers an exact domain, then the most
        specific subdomain. With *domain* None, the first entry by that name.
        Returns the entry (carrying value + expires_at), or None.
        """
        candidates = [e for e in self._entries if e.name == name]
        if not candidates or domain is None:
            return candidates[0] if candidates else None
        d = _norm_domain(domain)
        exact = [e for e in candidates if e.domain == d]
        if exact:
            return exact[0]
        subs = [e for e in candidates if e.domain.endswith("." + d)]
        return max(subs, key=lambda e: len(e.domain)) if subs else None

    def _best_for(self, host: str, request_path: str | None) -> dict[str, CookieEntry]:
        """Domain-matching entries (path-matching too when *request_path* is
        given), deduped by name.

        Domain rule: equal or parent at a dot boundary (``google.com`` reaches
        ``x.google.com``, never ``evilgoogle.com``); unknown-domain cookies
        match every host (fail open - assume they were harvested for the
        caller's own service). A name clash goes to the most specific entry -
        longest domain first, then longest path (the browser sorts duplicate
        names by path length; a dict can carry each name once, so the most
        specific one is the one a server honoring first-wins would see).
        """
        host_lc = (host or "").lower()
        best: dict[str, CookieEntry] = {}
        for e in self._entries:
            if e.domain and host_lc != e.domain and not host_lc.endswith("." + e.domain):
                continue
            if request_path is not None and not _path_matches(e.path, request_path):
                continue
            cur = best.get(e.name)
            if cur is None or (len(e.domain), len(e.path)) > (len(cur.domain), len(cur.path)):
                best[e.name] = e
        return best

    def for_host(self, host: str) -> dict[str, str]:
        """The host-level jar (all paths), as ``name → value``.

        For consumers that pin one cookie set onto a whole per-host session;
        use :meth:`for_url` when the request URL is in hand.
        """
        return {name: e.value for name, e in self._best_for(host, None).items()}

    def for_url(self, url: str) -> dict[str, str]:
        """The cookies a browser would send to *url* (domain AND path match)."""
        parts = urlsplit(url)
        return {
            name: e.value
            for name, e in self._best_for(
                parts.hostname or "", _norm_path(parts.path or "/")
            ).items()
        }

    def header_for(self, host: str) -> str:
        """Serialize :meth:`for_host` into a ``Cookie`` header value."""
        return "; ".join(f"{name}={value}" for name, value in self.for_host(host).items())

    def __iter__(self) -> Iterator[CookieEntry]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __repr__(self) -> str:  # never print values - they are credentials
        doms = sorted({e.domain or "?" for e in self._entries})
        return f"CookieJar({len(self._entries)} cookies, domains={doms})"


def select(jar: CookieJar, selector: CookieSelector) -> dict[str, str]:
    """Narrow *jar* by *selector*, returning a plain ``name → value`` dict.

    - ``True`` / ``None`` → keep all (name clash: most specific domain wins)
    - ``False`` → keep none
    - an iterable of names → keep only those names (allowlist)
    - a callable → ``selector(flat)`` returns the kept subset

    Used for hosts that deliberately need cross-domain cookies (the target
    shares the SSO parent domain), so it flattens the FULL jar first. An
    allowlist must be a real collection (set/frozenset/list/tuple), not a bare
    ``str`` - a ``str`` would iterate into individual characters.
    """
    if selector is False:
        return {}
    # Flatten with the same clash rule as for_host/for_url: most specific
    # entry wins (longest domain, then longest path).
    best: dict[str, CookieEntry] = {}
    for e in jar:
        cur = best.get(e.name)
        if cur is None or (len(e.domain), len(e.path)) > (len(cur.domain), len(cur.path)):
            best[e.name] = e
    flat = {name: e.value for name, e in best.items()}
    if selector is True or selector is None:
        return flat
    if callable(selector):
        return dict(selector(flat))
    allow = {str(n) for n in selector}
    return {k: v for k, v in flat.items() if k in allow}
