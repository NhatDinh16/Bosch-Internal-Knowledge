# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""Cross-process lock that serializes browser auth on a shared browser profile.

Edge/Chromium allow only ONE instance per ``--user-data-dir``. When several
skills -- or several Claude sessions, or a polling loop that periodically
re-harvests a token -- authenticate against the shared ``default`` browser
profile at the same time, the second launch collides with the first's profile
lock: the browser is closed out from under the cookie harvest and auth fails
with ``Target page, context or browser has been closed`` (missing artifact).
This lock makes those auths queue instead of colliding.

A crashed or hung auth must never hold the lock forever, yet a *legitimate* auth
can run several minutes (a headless attempt up to ~110s, then a headed retry up
to ~180s). A flat timeout can't tell "slow but valid" from "dead", so the lock
uses two independent release paths:

- **PID liveness** -- the lockfile records the holder's PID; a waiter reclaims
  the lock immediately if that process is gone. A crashed/broken attempt frees
  the lock in seconds, not minutes.
- **Heartbeat + stale TTL** -- the holder refreshes the lockfile every
  ``heartbeat_interval`` while working; a waiter reclaims a lock whose last
  heartbeat is older than ``stale_ttl`` (a hung-but-alive holder). Because a
  healthy long auth keeps heart-beating, a slow-but-valid auth is never wrongly
  reclaimed.

The lockfile is never held open (create-then-close), so reclaiming a stale lock
works on Windows too (an open handle would block ``unlink``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# A module-level indirection over the monotonic clock so the acquisition-wait
# budget can be driven deterministically in tests. Local wait budgeting uses
# this (immune to wall-clock jumps); cross-process heartbeat staleness stays on
# ``time.time()``.
_monotonic: Callable[[], float] = time.monotonic

# Defaults. A legit auth (headless ~110s + headed retry ~180s) stays well under
# STALE_TTL as long as it heart-beats, so these are safe.
STALE_TTL = 180.0  # seconds since last heartbeat -> holder assumed dead/hung
HEARTBEAT_INTERVAL = 15.0  # holder refreshes the lockfile this often while working
ACQUIRE_TIMEOUT = 360.0  # max time a waiter blocks before giving up (fail closed)
POLL_INTERVAL = 1.5  # how often a waiter re-checks the lock
_HEARTBEAT_WRITE_RETRIES = 3  # transient heartbeat-write retries before giving up
_HEARTBEAT_RETRY_BACKOFF = 0.5  # seconds between those retries


class BrowserProfileBusyError(TimeoutError):
    """A live, heart-beating auth still holds the shared profile after
    ``acquire_timeout``.

    Failing THIS caller is deliberate (fail closed): proceeding without the
    lock would launch a second browser on the same user-data-dir and kill the
    holder's in-flight sign-in - exactly the collision the lock exists to
    prevent, and it would make the stale-reclaim logic meaningless. Dead or
    hung holders never reach this point: PID liveness and the stale TTL
    reclaim those within seconds."""


def _pid_alive(pid: int) -> bool:
    """True if a process with *pid* currently exists (best-effort, no deps)."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True  # couldn't read exit code -> assume alive (conservative)
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    except OSError:
        return False
    return True


def _payload() -> bytes:
    return json.dumps(
        {"pid": os.getpid(), "host": os.environ.get("COMPUTERNAME") or "", "ts": time.time()}
    ).encode("utf-8")


def _read_holder(lockpath: Path):
    """Return (pid, ts) of the current holder, or None if unreadable/absent."""
    try:
        data = json.loads(lockpath.read_text(encoding="utf-8"))
        return int(data.get("pid", 0)), float(data.get("ts", 0.0))
    except (FileNotFoundError, ValueError, OSError):
        return None


def _rewrite(lockpath: Path) -> None:
    """Refresh the lockfile contents (heartbeat)."""
    fd = os.open(str(lockpath), os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, _payload())
    finally:
        os.close(fd)


def _notify(msg: str) -> None:
    """Surface a lock status message to the agent (stderr) and the logs.

    Printed to stderr so it is visible in the skill's captured output -- a skill
    blocked on this lock would otherwise look hung, and an agent watching it may
    panic and kill/retry. These messages say explicitly that waiting is expected
    and self-resolving.
    """
    print(msg, file=sys.stderr, flush=True)
    logger.info(msg.replace("\n", " "))


def _reclaimable(lockpath: Path, now: float, stale_ttl: float) -> bool:
    """True if the current lock may be stolen (holder dead, hung, or corrupt)."""
    holder = _read_holder(lockpath)
    if holder is None:
        # Unreadable/partial write: fall back to file mtime for staleness.
        try:
            return (now - lockpath.stat().st_mtime) > stale_ttl
        except OSError:
            return True  # vanished between checks -> free
    pid, ts = holder
    if pid and not _pid_alive(pid):
        return True  # holder process is gone (crash)
    return (now - ts) > stale_ttl  # heartbeat too old (hung-but-alive)


@contextlib.contextmanager
def profile_auth_lock(
    profile_dir: str | os.PathLike | None,
    *,
    stale_ttl: float = STALE_TTL,
    heartbeat_interval: float = HEARTBEAT_INTERVAL,
    acquire_timeout: float = ACQUIRE_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
):
    """Serialize browser auth on *profile_dir* across processes.

    Yields ``True`` when the lock was acquired, ``False`` only when there is
    nothing to lock (no profile dir - an ephemeral profile). Raises
    :class:`BrowserProfileBusyError` when a LIVE, heart-beating holder still
    owns the profile after ``acquire_timeout``, AND when the lock cannot be
    created at all (unusable lock directory or lockfile): a persistent shared
    profile must never proceed unlocked, so both cases fail closed. Dead/hung
    holders are reclaimed via PID liveness + the stale TTL long before that.
    """
    if not profile_dir:
        yield False  # ephemeral profile -> nothing to serialize
        return

    lockpath = Path(str(profile_dir) + ".authlock")
    try:
        lockpath.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # A persistent shared profile MUST be serialized: if we cannot even
        # create the lock directory, proceeding unlocked would let a second
        # browser open the same user-data-dir and kill a peer's sign-in. Fail
        # closed. (Ephemeral profiles returned False far above - they need no
        # lock and never reach here.)
        raise BrowserProfileBusyError(
            f"cannot create the browser-lock directory for {lockpath.name}: {exc}"
        ) from exc

    # Local wait budgeting is monotonic (a wall-clock jump must not shorten or
    # extend the wait); cross-process heartbeat staleness below stays wall-clock.
    mono_deadline = _monotonic() + acquire_timeout
    wait_start: float | None = None  # monotonic; set when we first find it held
    last_notice = 0.0
    holder_info: tuple = ("?", "?")

    def _give_up() -> None:
        hpid, age = holder_info
        _notify(
            f"[browser-lock] waited {acquire_timeout:.0f}s and the profile is still "
            f"held by a LIVE sign-in (holder pid={hpid}, last active {age} ago) -- "
            f"giving up instead of barging in (that would kill the holder's sign-in). "
            f"Retry once the other session's browser auth has finished."
        )
        raise BrowserProfileBusyError(
            f"shared browser profile still held by a live auth (pid={hpid}) after "
            f"{acquire_timeout:.0f}s - retry once the other sign-in has finished"
        ) from None

    while True:
        # Before every acquire attempt: once the budget is exhausted, fail closed
        # rather than grab a lock that only became free after our deadline. The
        # first iteration (wait_start is None) always gets one attempt.
        if wait_start is not None and _monotonic() >= mono_deadline:
            _give_up()
        try:
            fd = os.open(str(lockpath), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, _payload())
            finally:
                os.close(fd)
            if wait_start is not None:
                _notify(
                    f"[browser-lock] OK -- acquired the shared browser profile after "
                    f"{_monotonic() - wait_start:.0f}s; continuing with sign-in."
                )
            break
        except FileExistsError:
            wall_now = time.time()  # heartbeat staleness is wall-clock, not monotonic
            if _reclaimable(lockpath, wall_now, stale_ttl):
                _notify(
                    f"[browser-lock] previous browser sign-in looks stale/dead -- "
                    f"reclaiming {lockpath.name} and continuing."
                )
                # OSError -> someone else won the reclaim; loop and retry.
                with contextlib.suppress(OSError):
                    os.unlink(str(lockpath))
                continue
            holder = _read_holder(lockpath)
            hpid = holder[0] if holder else "?"
            age = f"{wall_now - holder[1]:.0f}s" if holder else "?"
            holder_info = (hpid, age)
            mono_now = _monotonic()
            if wait_start is None:
                # First time we see it held: explain, so the agent doesn't panic.
                wait_start = mono_now
                last_notice = mono_now
                _notify(
                    "[browser-lock] WAITING for the shared browser sign-in profile.\n"
                    f"  Another browser auth is already running (holder pid={hpid}, last "
                    f"active {age} ago).\n"
                    "  Only one browser can use the shared profile at a time, so this sign-in "
                    "is queued.\n"
                    "  It will start AUTOMATICALLY when the other one finishes, or within "
                    f"~{stale_ttl:.0f}s if that one is stale/dead.\n"
                    "  THIS IS EXPECTED under concurrent skills/sessions -- do NOT kill or retry "
                    "this process; just wait."
                )
            elif mono_now - last_notice >= 30:
                last_notice = mono_now
                _notify(
                    f"[browser-lock] still waiting ({mono_now - wait_start:.0f}s elapsed) for the "
                    f"shared browser profile; holder pid={hpid}, last active {age} ago. "
                    "Normal under concurrency -- will proceed automatically, no action needed."
                )
            remaining = mono_deadline - _monotonic()
            if remaining <= 0:
                _give_up()
            # Never sleep past the remaining budget: a partial final sleep lands
            # exactly on the deadline, and the next iteration fails closed.
            time.sleep(min(poll_interval, remaining))
        except OSError as exc:
            # Same fail-closed reasoning as the mkdir above: a persistent
            # profile must never proceed unlocked.
            raise BrowserProfileBusyError(
                f"cannot open the browser lockfile {lockpath.name}: {exc}"
            ) from exc

    # We hold the lock. Heartbeat until released; stop early if we get stolen.
    stop = threading.Event()

    def _heartbeat() -> None:
        while not stop.wait(heartbeat_interval):
            holder = _read_holder(lockpath)
            if holder is not None and holder[0] != os.getpid():
                return  # lost the lock (reclaimed by a peer) -> stop refreshing
            # Retry transient write failures (AV lock, momentary sharing violation)
            # so a single hiccup doesn't kill a healthy holder's heartbeat.
            for attempt in range(_HEARTBEAT_WRITE_RETRIES):
                try:
                    _rewrite(lockpath)
                    break
                except OSError as exc:
                    if attempt + 1 == _HEARTBEAT_WRITE_RETRIES:
                        logger.warning(
                            "[browser-lock] heartbeat write failed %d times (%s); "
                            "holder may be reclaimed as stale in ~%.0fs",
                            _HEARTBEAT_WRITE_RETRIES,
                            exc,
                            stale_ttl,
                        )
                    else:
                        stop.wait(_HEARTBEAT_RETRY_BACKOFF)

    beat = threading.Thread(target=_heartbeat, name="browser-lock-heartbeat", daemon=True)
    beat.start()
    try:
        yield True
    finally:
        stop.set()
        # Only remove the lock if WE still hold it. An unreadable holder (None)
        # must not authorize deletion -- it may be a peer mid-rewrite after
        # reclaiming us; leave it for the stale-TTL/pid-liveness path.
        holder = _read_holder(lockpath)
        if holder is not None and holder[0] == os.getpid():
            with contextlib.suppress(OSError):
                os.unlink(str(lockpath))
