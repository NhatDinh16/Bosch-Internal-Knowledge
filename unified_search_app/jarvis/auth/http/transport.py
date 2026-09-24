from __future__ import annotations

import base64
import http.client
import os
import socket
from urllib.parse import urlsplit

import requests
import urllib3.connection
import urllib3.connectionpool
import urllib3.poolmanager
from requests.adapters import HTTPAdapter

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

try:
    import pywintypes
    import sspi
    import sspicon
    import win32security
except ImportError:  # pragma: no cover - Windows-only dependency
    pywintypes = None
    sspi = None
    sspicon = None
    win32security = None


_PYWIN_ERROR = getattr(pywintypes, "error", Exception) if pywintypes else Exception


class ProxyAuthenticationError(requests.RequestException):
    """Raised when proxy negotiation fails."""


_DEFAULT_TIMEOUT = 30  # seconds; prevents infinite hangs on unresponsive hosts


class _TimeoutSession(requests.Session):
    """Session subclass that applies a default timeout to all requests."""

    def request(self, method, url, **kwargs):  # type: ignore[override]  # adds a default timeout; forwards the parent's kwargs unchanged
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        # ``trust_env`` is deliberately disabled so proxy behavior is explicit
        # and deterministic. Resolve it per URL instead: this honors NO_PROXY,
        # avoids a failed-direct-then-proxy retry, and never advertises plain
        # HTTP support that the SSPI CONNECT adapter cannot provide.
        kwargs.setdefault("proxies", _proxy_mapping(url))
        return super().request(method, url, **kwargs)


def configure_session(session: requests.Session) -> requests.Session:
    """Apply proxy-aware NTLM/Negotiate support to an existing session.

    Use this when you have a custom Session subclass and cannot use
    ``create_session()``. Mutates and returns the session for chaining.

    Example::

        from jarvis.auth.http.transport import configure_session

        class MySession(requests.Session):
            def __init__(self):
                super().__init__()
                configure_session(self)
    """
    session.trust_env = False
    # Safe to mount unconditionally: direct HTTPS requests retain normal
    # HTTPAdapter behavior, while a proxy can appear in the environment after
    # session construction and still get the SSPI-aware manager.
    session.mount("https://", _ProxyAuthHTTPAdapter())

    # _TimeoutSession resolves proxies per request. A caller-supplied custom
    # Session cannot do that, so preserve configure_session's historical
    # proxy-aware behavior while being explicit that SSPI support is HTTPS-only.
    if not isinstance(session, _TimeoutSession):
        session.proxies = _proxy_mapping("https://placeholder.invalid")
    return session


def create_session() -> requests.Session:
    """Create a requests.Session with proxy-aware NTLM/Negotiate support.

    The returned session handles corporate proxy authentication (Windows SSPI)
    transparently and applies a default 30s timeout to all requests.
    Use it as a drop-in replacement for ``requests.Session()``.

    If no proxy environment variables are set, the session behaves identically
    to a standard requests.Session (plus the default timeout).

    Callers can still override per-request: ``session.get(url, timeout=60)``.

    Example::

        from jarvis.auth.http.transport import create_session

        session = create_session()
        resp = session.get("https://api.example.com/data")  # 30s timeout
    """
    return configure_session(_TimeoutSession())


def request(
    method,
    url,
    *,
    headers=None,
    data=None,
    json_body=None,
    timeout=_DEFAULT_TIMEOUT,
    allow_redirects=True,
    params=None,
    cookies=None,
    cookie_domain=None,
):
    session = create_session()
    _apply_cookies(session, cookies, cookie_domain)
    return session.request(
        method,
        url,
        headers=headers,
        data=data,
        json=json_body,
        timeout=timeout,
        allow_redirects=allow_redirects,
        params=params,
    )


def _proxy_request(
    method,
    url,
    *,
    headers=None,
    data=None,
    json_body=None,
    timeout=_DEFAULT_TIMEOUT,
    allow_redirects=True,
    params=None,
    cookies=None,
    cookie_domain=None,
):
    if urlsplit(url).scheme.lower() != "https":
        raise ProxyAuthenticationError("Authenticated proxy transport supports HTTPS URLs only")

    proxy_url = _get_proxy_url(url)
    if not proxy_url:
        raise ProxyAuthenticationError(f"No proxy configured for {url}")

    session = create_session()
    _apply_cookies(session, cookies, cookie_domain)

    return session.request(
        method,
        url,
        headers=headers,
        data=data,
        json=json_body,
        timeout=timeout,
        allow_redirects=allow_redirects,
        params=params,
        proxies={"https": proxy_url},
    )


def _apply_cookies(session, cookies, cookie_domain):
    if not cookies:
        return
    for name, value in cookies.items():
        if cookie_domain:
            session.cookies.set(name, value, domain=cookie_domain)
        else:
            session.cookies.set(name, value)


def _get_proxy_url(url):
    no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY")
    if requests.utils.should_bypass_proxies(url, no_proxy=no_proxy):
        return None

    scheme = urlsplit(url).scheme.lower()
    if scheme == "https":
        return (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
        )
    if scheme == "http":
        return os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
    return None


def _proxy_mapping(url):
    """Explicit requests proxy mapping for one URL.

    The custom SSPI implementation authenticates HTTPS CONNECT tunnels. Plain
    HTTP proxying is intentionally omitted rather than silently routing it
    through an adapter that cannot complete Negotiate/NTLM authentication.
    """
    if urlsplit(url).scheme.lower() != "https":
        return {}
    proxy_url = _get_proxy_url(url)
    return {"https": proxy_url} if proxy_url else {}


class _ProxyAuthHTTPAdapter(HTTPAdapter):
    def proxy_manager_for(self, proxy, **proxy_kwargs):
        if proxy in self.proxy_manager:
            return self.proxy_manager[proxy]

        manager = _ProxyAuthProxyManager(
            proxy,
            proxy_headers=self.proxy_headers(proxy),
            num_pools=self._pool_connections,
            maxsize=self._pool_maxsize,
            block=self._pool_block,
            **proxy_kwargs,
        )
        self.proxy_manager[proxy] = manager
        return manager


class _ProxyAuthProxyManager(urllib3.poolmanager.ProxyManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pool_classes_by_scheme = urllib3.poolmanager.pool_classes_by_scheme.copy()
        self.pool_classes_by_scheme["https"] = _ProxyAuthHTTPSConnectionPool


class _ProxyAuthHTTPSConnectionPool(urllib3.connectionpool.HTTPSConnectionPool):
    # Reassigned to _ProxyAuthHTTPSConnection below (defined after this class).
    ConnectionCls = None  # type: ignore[assignment]


class _ProxyTunnelReconnect(Exception):
    """Internal signal: the proxy closed the connection mid-auth; the tunnel must be
    retried on a fresh socket (handled by ``_ProxyAuthHTTPSConnection.connect``)."""


class _ProxyAuthHTTPSConnection(urllib3.connection.HTTPSConnection):
    # Proxy-Authorization computed on a previous connect() attempt that the proxy then
    # dropped (407 answered with "Connection: close"). Presented pre-emptively on the
    # fresh socket's first CONNECT so the tunnel comes up without re-challenging.
    _pending_proxy_auth: str = ""

    def connect(self) -> None:
        # urllib3's connect() captures the socket in a local BEFORE calling _tunnel() and
        # TLS-wraps that local afterwards (it also re-reads self._tunnel_host). A proxy
        # that closes the connection on its 407 challenge forces the authenticated CONNECT
        # onto a NEW socket - so _tunnel() must never swap self.sock in place, or urllib3
        # would wrap the stale, closed one. Instead _tunnel() raises _ProxyTunnelReconnect
        # and we re-run the whole connect() on a fresh socket, presenting the auth it
        # already computed. Keep-alive proxies never raise and take one pass.
        self._pending_proxy_auth = ""
        for _ in range(4):
            try:
                super().connect()
                return
            except _ProxyTunnelReconnect:
                continue
        raise OSError(
            "Tunnel connection failed: proxy kept closing the connection during authentication"
        )

    def _tunnel(self) -> None:
        if self.sock is None:
            raise OSError("Proxy socket is not connected")

        # _tunnel_headers/_host/_port are set by http.client.HTTPConnection.set_tunnel and
        # read again by urllib3's connect() after this returns, so this method must leave
        # them intact on any path that continues the tunnel (it never calls self.close()
        # on the reconnect path - only on genuine, terminal failures).
        tunnel_host = self._tunnel_host
        tunnel_port = self._tunnel_port

        headers = dict(self._tunnel_headers)  # type: ignore[attr-defined]
        headers.setdefault("Host", _format_host(tunnel_host, tunnel_port))
        headers.setdefault("Proxy-Connection", "Keep-Alive")
        if self._pending_proxy_auth:
            headers["Proxy-Authorization"] = self._pending_proxy_auth

        proxy_context: _SSPIProxyContext | None = None
        active_scheme: str | None = None

        # First iteration sends a plain CONNECT (or the pre-emptive auth from a prior
        # reconnect); internal hosts that need no auth answer 200 immediately.
        for _attempt in range(5):
            status, reason, auth_headers, should_reconnect = _send_connect_request(
                self,
                tunnel_host,
                tunnel_port,
                headers,
            )

            if status == http.client.OK:
                self._pending_proxy_auth = ""
                return

            if status != http.client.PROXY_AUTHENTICATION_REQUIRED:
                self.close()
                raise OSError(f"Tunnel connection failed: {status} {reason.strip()}")

            next_scheme, challenge = _choose_proxy_scheme(auth_headers)
            if next_scheme is None:
                self.close()
                raise OSError("Tunnel connection failed: 407 Proxy Authentication Required")

            if proxy_context is None or next_scheme != active_scheme:
                proxy_context = _SSPIProxyContext(self.host, next_scheme)
                active_scheme = next_scheme

            authorization = proxy_context.build_authorization(challenge)
            headers["Proxy-Authorization"] = authorization

            if should_reconnect:
                # The proxy will drop this connection. Cache the auth and retry the whole
                # tunnel on a fresh socket via connect(). Close only the socket (not the
                # connection) so _tunnel_host/_port survive for the retry and for urllib3's
                # post-_tunnel handshake.
                self._pending_proxy_auth = authorization
                if self.sock is not None:
                    self.sock.close()
                    self.sock = None
                raise _ProxyTunnelReconnect

        self.close()
        raise OSError("Tunnel connection failed: 407 Proxy Authentication Required")


_ProxyAuthHTTPSConnectionPool.ConnectionCls = _ProxyAuthHTTPSConnection  # type: ignore[assignment]


class _SSPIProxyContext:
    def __init__(self, proxy_host, scheme):
        if not all((sspi, sspicon, win32security, pywintypes)):
            raise ProxyAuthenticationError("Windows SSPI libraries are not available")

        self.proxy_host = _canonicalize_host(proxy_host)
        self.scheme = scheme
        self._pkg_info = win32security.QuerySecurityPackageInfo(scheme)
        self._clientauth = sspi.ClientAuth(scheme, targetspn=f"HTTP/{self.proxy_host}")
        self._sec_buffer = win32security.PySecBufferDescType()

    def build_authorization(self, challenge):
        # Every authorize leg receives only the current proxy challenge. Reusing
        # the descriptor accumulates stale token buffers and breaks multi-leg
        # Negotiate handshakes.
        self._sec_buffer = win32security.PySecBufferDescType()
        if challenge:
            token_buffer = win32security.PySecBufferType(
                self._pkg_info["MaxToken"],
                sspicon.SECBUFFER_TOKEN,
            )
            token_buffer.Buffer = base64.b64decode(challenge)
            self._sec_buffer.append(token_buffer)

        try:
            _error, auth = self._clientauth.authorize(self._sec_buffer)
        except _PYWIN_ERROR as exc:
            raise ProxyAuthenticationError(f"SSPI proxy auth failed: {exc}") from exc

        return f"{self.scheme} {base64.b64encode(auth[0].Buffer).decode('ascii')}"


def _send_connect_request(conn, tunnel_host, tunnel_port, headers):
    connect = b"CONNECT %s:%d HTTP/1.0\r\n" % (
        tunnel_host.encode("ascii"),
        tunnel_port,
    )
    payload = [connect]
    for header, value in headers.items():
        payload.append(f"{header}: {value}\r\n".encode("latin-1"))
    payload.append(b"\r\n")
    conn.send(b"".join(payload))

    response = conn.response_class(conn.sock, method="CONNECT")
    response.begin()
    status = response.status
    reason = response.reason
    auth_headers = response.headers.get_all("Proxy-Authenticate") or []
    should_reconnect = bool(response.will_close)

    if status != http.client.OK:
        try:
            if response.length not in (None, 0):
                response.read(response.length)
            elif response.will_close:
                response.read()
        except (OSError, http.client.HTTPException):
            pass

    response.close()
    return status, reason, auth_headers, should_reconnect


def _choose_proxy_scheme(auth_headers):
    values: list[str] = []
    for raw in auth_headers:
        values.extend(part.strip() for part in raw.split(",") if part.strip())

    for scheme in ("Negotiate", "NTLM"):
        prefix = scheme.lower()
        for value in values:
            if value.lower() == prefix:
                return scheme, None
            if value.lower().startswith(prefix + " "):
                return scheme, value[len(scheme) :].strip() or None
    return None, None


def _canonicalize_host(host):
    try:
        canonical = socket.getaddrinfo(host, None, 0, 0, 0, socket.AI_CANONNAME)[0][3]
    except socket.gaierror:
        return host
    return canonical or host


def _format_host(host, port):
    if host is None:
        return ""
    if port is None:
        return host
    return f"{host}:{port}"
