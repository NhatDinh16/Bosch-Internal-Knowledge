# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
"""Browser auto-discovery for Chromium-based browsers (Edge, Chrome).

Resolves which browser executable to use for Playwright CDP sessions.
Edge is preferred on Bosch/ETAS machines where it's always installed;
Chrome is the fallback.

Resolution order:

    1. Config file (~/.config/jarvis/auth/browser.json)
       → explicit ``executable_path`` or ``browser`` name
    2. Edge at known Windows/macOS/Linux paths
    3. Chrome at known Windows/macOS/Linux paths
    4. RuntimeError if nothing found

Config file format (all fields optional)::

    {
        "browser": "edge",           // "edge", "chrome", or "auto" (default)
        "executable_path": null       // absolute path; overrides browser field
    }
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
from pathlib import Path

from jarvis.config import config_root

logger = logging.getLogger(__name__)

_CONFIG_DIR = config_root() / "auth"
_CONFIG_FILE = _CONFIG_DIR / "browser.json"

# ── Known browser paths ─────────────────────────────────────────────────────

_EDGE_PATHS_WINDOWS = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

_CHROME_PATHS_WINDOWS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]

_EDGE_PATHS_LINUX = [
    Path("/usr/bin/microsoft-edge"),
    Path("/usr/bin/microsoft-edge-stable"),
]

_CHROME_PATHS_LINUX = [
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/google-chrome-stable"),
    Path("/usr/bin/chromium-browser"),
    Path("/usr/bin/chromium"),
]

_EDGE_PATHS_MACOS = [
    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
]

_CHROME_PATHS_MACOS = [
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
]


def _find_executable(candidates: list[Path]) -> Path | None:
    """Return the first candidate that exists on disk."""
    for p in candidates:
        if p.is_file():
            return p
    return None


def _find_on_path(name: str) -> Path | None:
    """Try to find an executable via PATH (shutil.which)."""
    result = shutil.which(name)
    return Path(result) if result else None


def _platform_candidates() -> tuple[list[Path], list[Path]]:
    """Return (edge_candidates, chrome_candidates) for this OS."""
    system = platform.system()
    if system == "Windows":
        return _EDGE_PATHS_WINDOWS, _CHROME_PATHS_WINDOWS
    elif system == "Darwin":
        return _EDGE_PATHS_MACOS, _CHROME_PATHS_MACOS
    else:
        return _EDGE_PATHS_LINUX, _CHROME_PATHS_LINUX


def _load_config() -> dict:
    """Load browser config from disk, if it exists."""
    if not _CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", _CONFIG_FILE, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("%s is not a JSON object; ignoring it.", _CONFIG_FILE)
        return {}
    return data


def _find_edge() -> Path | None:
    """Find Edge executable: known paths first, then PATH."""
    edge_paths, _ = _platform_candidates()
    found = _find_executable(edge_paths)
    if found:
        return found
    return _find_on_path("msedge") or _find_on_path("microsoft-edge")


def _find_chrome() -> Path | None:
    """Find Chrome executable: known paths first, then PATH."""
    _, chrome_paths = _platform_candidates()
    found = _find_executable(chrome_paths)
    if found:
        return found
    return (
        _find_on_path("chrome")
        or _find_on_path("google-chrome")
        or _find_on_path("chromium")
        or _find_on_path("chromium-browser")
    )


# ── Public API ───────────────────────────────────────────────────────────────


def discover_browser() -> tuple[str, str]:
    """Discover the best available Chromium-based browser.

    Returns:
        (browser_name, executable_path) -- e.g. ("edge", "C:\\...\\msedge.exe")

    Raises:
        RuntimeError: if no browser is found.
    """
    config = _load_config()

    # Explicit path in config overrides everything
    explicit_path = config.get("executable_path")
    if explicit_path:
        p = Path(explicit_path)
        if p.is_file():
            name = config.get("browser", "custom")
            logger.info("Browser from config: %s (%s)", name, p)
            return name, str(p)
        raise RuntimeError(f"Configured browser path does not exist: {explicit_path}")

    # Config requests a specific browser
    preference = config.get("browser", "auto")

    if preference == "edge":
        found = _find_edge()
        if found:
            logger.info("Browser (config=edge): %s", found)
            return "edge", str(found)
        raise RuntimeError("Config requests Edge but it was not found")

    if preference == "chrome":
        found = _find_chrome()
        if found:
            logger.info("Browser (config=chrome): %s", found)
            return "chrome", str(found)
        raise RuntimeError("Config requests Chrome but it was not found")

    # Auto-discovery: Edge first (corporate default), Chrome fallback
    edge = _find_edge()
    if edge:
        logger.info("Browser (auto): Edge at %s", edge)
        return "edge", str(edge)

    chrome = _find_chrome()
    if chrome:
        logger.info("Browser (auto): Chrome at %s", chrome)
        return "chrome", str(chrome)

    raise RuntimeError(
        "No Chromium-based browser found. Install Edge or Chrome, "
        "or set executable_path in ~/.config/jarvis/auth/browser.json"
    )
