from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from re import Pattern

logger = logging.getLogger(__name__)

# Entra issues single-page-app (SPA) refresh tokens with a fixed, non-extendable
# lifetime (AADSTS700084: "a fixed, limited lifetime of 1.00:00:00, which cannot be
# extended"). The clock starts at the interactive sign-in and is preserved across
# silent rotations, so a near-expired SPA refresh token must trigger a browser
# re-auth rather than a doomed grant_type=refresh_token exchange.
MS_SPA_REFRESH_TOKEN_LIFETIME = 24 * 3600
_MS_SPA_TOKEN_HOST = "login.microsoftonline.com"


_VALID_ARTIFACT_SOURCES = frozenset(
    {
        "cookie",
        "oauth",
        "ls",
        "ls_key_contains",
        "ls_aud_equals",
        "ls_aud_contains",
    }
)


@dataclass(frozen=True)
class ArtifactRule:
    name: str
    source: str
    key: str
    required: bool = True
    domain: str | None = None  # For cookies: prefer this domain (e.g. "google.com")

    def __post_init__(self) -> None:
        if self.source not in _VALID_ARTIFACT_SOURCES:
            raise ValueError(
                f"ArtifactRule '{self.name}': invalid source '{self.source}'. "
                f"Must be one of: {', '.join(sorted(_VALID_ARTIFACT_SOURCES))}"
            )


# ── Refresh strategies ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class OAuthRefreshStrategy:
    """OAuth2 refresh_token exchange configuration."""

    token_endpoint: str
    client_id: str
    scope: str = ""
    origin: str | None = None  # CORS Origin header (required for SPA apps)
    artifact_name: str = ""  # artifact name for the refreshed access_token (required)
    # Services sharing an IdP whose refresh_token is broadly authorized (e.g. all Microsoft
    # web apps) name a group here. A refresh_token is bound to the client that minted it, so
    # the shared slot stores {refresh_token, client_id, origin} together; a service with no
    # own refresh_token re-mints its resource token from the group's stored token, redeeming
    # with the stored client and only varying the scope.
    shared_group: str | None = None
    # Fixed lifetime (seconds) of the refresh_token this strategy mints, when known.
    # None = derive: a Microsoft SPA client (sends a CORS Origin to login.microsoftonline.com)
    # gets the fixed 24h SPA lifetime; anything else stays unknown (None). Set explicitly only
    # to override that derivation.
    refresh_token_lifetime: float | None = None
    # Raw OAuth2 'claims' request parameter (a JSON string), sent verbatim in the token
    # request body when set. MSAL.js sends this on every refresh for a Continuous Access
    # Evaluation (CAE) aware client; None = the strategy has no claims to declare.
    claims: str | None = None

    def known_refresh_token_lifetime(self) -> float | None:
        """The refresh token's fixed lifetime in seconds, or None when unknown.

        Prefers an explicit ``refresh_token_lifetime``; otherwise derives the 24h SPA
        lifetime for Microsoft SPA clients (Origin header + login.microsoftonline.com
        endpoint). Non-Microsoft or non-SPA strategies stay None - we neither know nor
        assume a lifetime for them."""
        if self.refresh_token_lifetime is not None:
            return self.refresh_token_lifetime
        if self.origin and _MS_SPA_TOKEN_HOST in self.token_endpoint:
            return MS_SPA_REFRESH_TOKEN_LIFETIME
        return None


def refresh_token_expiry(
    strategy: RefreshStrategy,
    *,
    carried: float | None = None,
) -> float | None:
    """Absolute expiry to stamp on a refresh_token being persisted, or None if unknown.

    ``carried`` is the session expiry of the token this one descends from (the row it
    rotated out of); when present it wins, because an SPA refresh token's lifetime is
    anchored to the sign-in and cannot be extended by rotation. With no carried value a
    freshly harvested token is anchored at ``now + lifetime`` when the strategy declares
    (or derives) one. Returns None when neither applies - NULL means "no assumed expiry"."""
    if carried is not None:
        return carried
    if not isinstance(strategy, OAuthRefreshStrategy):
        return None
    lifetime = strategy.known_refresh_token_lifetime()
    if lifetime is None:
        return None
    return time.time() + lifetime


@dataclass(frozen=True)
class CookieExchangeStrategy:
    """Cookie → JWT/Bearer token exchange configuration."""

    exchange_endpoint: str
    method: str = "POST"
    extract_field: str = "token"
    token_header_name: str = "Authorization"
    body: str | None = None  # Optional request body (None = empty)
    extra_headers: tuple[tuple[str, str], ...] = ()  # frozen-compat


@dataclass(frozen=True)
class SapisidhashStrategy:
    """Google SAPISIDHASH computation configuration."""

    cookie_name: str = "SAPISID"
    origin: str = ""


# ── Service profile ─────────────────────────────────────────────────────────


RefreshStrategy = OAuthRefreshStrategy | CookieExchangeStrategy | SapisidhashStrategy | None


@dataclass(frozen=True)
class ServiceProfile:
    service: str
    start_url: str
    wait_for_url: str | Pattern[str] | None = None
    wait_for_cookies: tuple[str, ...] = ()
    wait_for_cookies_mode: str = "all"  # "all" = every cookie must be present; "any" = at least one
    # Hold the browser open until an OAuth access_token is captured, instead of stopping at
    # the URL/cookie signal. Needed when the token only appears AFTER landing - e.g. a SPA
    # that completes its own SSO exchange and then sends a Bearer on its API calls, whose
    # landing URL also matches the pre-login page (DependencyTrack).
    wait_for_oauth_access_token: bool = False
    wait_for_idle: float = 5.0
    harvest_ls: bool = True
    persistent_profile: str | None = None
    extra_ls_origins: tuple[str, ...] = ()
    browser_args: tuple[str, ...] = ()
    artifacts: tuple[ArtifactRule, ...] = field(default_factory=tuple)
    refresh_strategy: RefreshStrategy = None
    # Additional OAuth2 refresh strategies for services that hold several Bearer
    # tokens of different audiences behind ONE refresh token (e.g. Teams: graph +
    # sharepoint + spaces/mt). Each targets a distinct artifact_name (its own
    # scope); ensure_credentials picks the one matching the artifact it needs and
    # refreshes it silently, no browser. Empty for single-token services.
    extra_refresh_strategies: tuple = field(default_factory=tuple)
    # When set, only OAuth access_tokens whose JWT `aud` claim contains this
    # substring are captured. Use when a page surfaces multiple access_tokens for
    # different resources (e.g. SharePoint: the SPO REST resource
    # "00000003-0000-0ff1-ce00-000000000000" vs Graph/extensibility tokens) and
    # only one audience is usable by the skill's API. None = capture any token.
    oauth_audience_contains: str | None = None


# ── Registry ────────────────────────────────────────────────────────────────
#
# A profile reaches the registry two ways:
#   1. Explicit: ``register(ServiceProfile(...))`` (e.g. from a module's
#      ``auth_profile.py`` imported on package import).
#   2. Declarative: a directory registered via ``register_profile_dir(path)``
#      holds an ``auth.json`` (or ``<service>.auth.json``); ``get_profile`` loads
#      and builds it lazily on the first miss.


_REGISTRY: dict[str, ServiceProfile] = {}
_PROFILE_DIRS: list[Path] = []


def register(profile: ServiceProfile) -> ServiceProfile:
    """Register a profile in the global registry. Idempotent.

    Returns the profile so it can be used at module level::

        PROFILE = register(ServiceProfile(service="myskill", ...))
    """
    if not isinstance(profile, ServiceProfile):
        raise TypeError(f"register() expects ServiceProfile, got {type(profile).__name__}")
    _REGISTRY[profile.service] = profile
    return profile


def register_profile_dir(path: Path | str) -> None:
    """Register a directory that holds declarative ``auth.json`` profiles.

    A package/skill calls this once (e.g. ``register_profile_dir(Path(__file__).parent)``);
    ``get_profile(service)`` then loads ``<dir>/auth.json`` or
    ``<dir>/<service>.auth.json`` on demand, no import-time side effect.
    """
    p = Path(path)
    if p not in _PROFILE_DIRS:
        _PROFILE_DIRS.append(p)


def _load_from_dirs(service: str) -> ServiceProfile | None:
    from ._profile_json import profile_from_json

    for d in _PROFILE_DIRS:
        for candidate in (d / f"{service}.auth.json", d / "auth.json"):
            if candidate.is_file():
                profile = profile_from_json(candidate)
                if profile.service == service:
                    _REGISTRY[profile.service] = profile
                    return profile
    return None


def get_profile(service: str) -> ServiceProfile:
    """Look up a registered profile, loading a declarative auth.json on first miss."""
    if service in _REGISTRY:
        return _REGISTRY[service]
    loaded = _load_from_dirs(service)
    if loaded is not None:
        return loaded
    raise KeyError(
        f"No profile registered for '{service}'. Either import the package that "
        f"registers it, or register_profile_dir() a directory with its auth.json."
    )


def list_profiles() -> list[ServiceProfile]:
    """Return all currently-registered profiles."""
    return list(_REGISTRY.values())
