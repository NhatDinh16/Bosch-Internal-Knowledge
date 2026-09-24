# SPDX-FileCopyrightText: 2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""User-state locations and multi-instance config for the jarvis distribution.

Two concerns live here, both product-agnostic and free of auth/HTTP:

* **State locations.** All persistent state lives under one root that mirrors the
  import namespace: ``~/.config/jarvis/<subpackage>/...`` (e.g. ``auth/token_cache.db``,
  ``teams/user_cache.json``). CLI-global files sit directly at the root;
  ``~/.config/jarvis/skills/<skill>/`` is reserved for skill-owned config. Callers
  create directories themselves when they write.

* **Multi-instance routing.** Skills that talk to several deployments of the same
  product (Confluence/Jira Datacenter + Cloud tenants) share one config shape - a
  list of ``{name, type, baseUrl}`` instances - and one resolution rule: match a URL
  to the instance whose ``baseUrl`` is its longest path-boundary prefix, or look an
  instance up by name. Baked-in package defaults are merged with the user's file so a
  skill ships useful instances out of the box while the user adds their own.
  :class:`InstanceRegistry` is that shared piece; per-product extras
  (``default_spaces`` for confluence, ``default_instance`` for jira) layer on top.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def config_root() -> Path:
    return Path.home() / ".config" / "jarvis"


def skill_config_path(skill: str) -> Path:
    """Path to a skill's user config file under the jarvis root."""
    return config_root() / "skills" / skill / "config.json"


def load_skill_config(skill: str, *, path: Path | None = None) -> dict:
    """Load a skill's user config, or ``{}`` when the file is absent.

    Reads UTF-8 tolerating a BOM (Windows editors add one). A malformed file raises
    ``json.JSONDecodeError`` - a broken config should fail loudly, not silently.
    """
    p = path or skill_config_path(skill)
    if not p.is_file():
        return {}
    with open(p, encoding="utf-8-sig") as f:
        return json.load(f)


# ── Multi-instance routing ───────────────────────────────────────────────────

_VALID_TYPES = frozenset({"datacenter", "cloud"})


def _normalize_url(url: str) -> str:
    """Lowercase and drop a trailing slash for prefix comparison."""
    return url.rstrip("/").lower()


@dataclass(frozen=True)
class Instance:
    """One configured deployment of a product.

    ``base_url`` is the full root URL including any context path (e.g.
    ``https://host/confluence``), with no trailing slash. ``type`` is
    ``"datacenter"`` or ``"cloud"``.
    """

    name: str
    type: str
    base_url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        if not self.name:
            raise ValueError("Instance.name must be non-empty")
        if self.type not in _VALID_TYPES:
            raise ValueError(
                f"Instance {self.name!r}: type must be one of {sorted(_VALID_TYPES)}, "
                f"got {self.type!r}"
            )
        if not self.base_url:
            raise ValueError(f"Instance {self.name!r}: baseUrl must be non-empty")

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    @classmethod
    def from_dict(cls, d: Mapping[str, str]) -> Instance:
        """Build from a config dict (keys: ``name``, ``type``, ``baseUrl``)."""
        try:
            return cls(name=d["name"], type=str(d["type"]).lower(), base_url=d["baseUrl"])
        except KeyError as e:
            raise ValueError(f"instance config entry missing required field {e}") from None


def _coerce(item: Instance | Mapping[str, str]) -> Instance:
    return item if isinstance(item, Instance) else Instance.from_dict(item)


class InstanceRegistry:
    """A merged set of configured instances plus URL/name routing.

    Built from baked-in *defaults* overlaid with a user config dict: a user entry
    replaces any default it collides with by name (case-insensitive) or by
    normalized ``base_url``, so a user can both add new instances and override a
    shipped default in place. Order is preserved: surviving defaults first (in their
    original order), then user additions.
    """

    def __init__(self, instances: Iterable[Instance], default_name: str | None = None):
        self._instances: list[Instance] = list(instances)
        self._default_name = default_name

    @classmethod
    def from_config(
        cls,
        defaults: Iterable[Instance | Mapping[str, str]],
        user_config: Mapping[str, Any] | None = None,
    ) -> InstanceRegistry:
        result: list[Instance] = [_coerce(d) for d in defaults]
        user_config = user_config or {}
        raw_instances = user_config.get("instances") or []
        for raw in raw_instances:
            ui = _coerce(raw)
            result = [
                d
                for d in result
                if d.name.lower() != ui.name.lower()
                and _normalize_url(d.base_url) != _normalize_url(ui.base_url)
            ]
            result.append(ui)
        default_name = user_config.get("default_instance")
        return cls(result, str(default_name) if default_name else None)

    @classmethod
    def load(
        cls,
        skill: str,
        defaults: Iterable[Instance | Mapping[str, str]],
        *,
        path: Path | None = None,
    ) -> InstanceRegistry:
        """Merge *defaults* with the skill's user config file."""
        return cls.from_config(defaults, load_skill_config(skill, path=path))

    @property
    def instances(self) -> list[Instance]:
        return list(self._instances)

    def names(self) -> list[str]:
        return [i.name for i in self._instances]

    def find_by_name(self, name: str | None) -> Instance | None:
        if not name:
            return None
        for inst in self._instances:
            if inst.name.lower() == name.lower():
                return inst
        return None

    def match_by_url(self, url: str) -> Instance | None:
        """Return the instance whose ``base_url`` is the longest path-boundary prefix.

        A prefix counts only when the URL continues at a path boundary (end of
        string, ``/``, ``?`` or ``#``), so ``.../confluence`` does not swallow a URL
        under ``.../confluence2`` (and vice versa). Among genuine matches the longest
        ``base_url`` wins, so a context-path instance beats a bare-host one.
        """
        norm = _normalize_url(url)
        best: Instance | None = None
        best_len = -1
        for inst in self._instances:
            base = _normalize_url(inst.base_url)
            if not base or not norm.startswith(base):
                continue
            rest = norm[len(base) :]
            if rest and rest[0] not in "/?#":
                continue
            if len(base) > best_len:
                best, best_len = inst, len(base)
        return best

    def resolve(self, *, url: str | None = None, name: str | None = None) -> Instance:
        """Resolve to one instance: URL match, then name, then default, then first.

        Raises ``LookupError`` with an actionable message when nothing resolves.
        """
        if url:
            inst = self.match_by_url(url)
            if inst:
                return inst
            raise LookupError(
                f"No configured instance matches URL {url!r}. "
                f"Configured: {[(i.name, i.base_url) for i in self._instances]}"
            )
        if name:
            inst = self.find_by_name(name)
            if inst:
                return inst
            raise LookupError(f"No configured instance named {name!r}. Available: {self.names()}")
        inst = self.find_by_name(self._default_name)
        if inst:
            return inst
        if self._instances:
            return self._instances[0]
        raise LookupError("No instances configured.")
