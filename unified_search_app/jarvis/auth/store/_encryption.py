# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""
Machine-bound encryption for the token cache.

Encrypts token values with Fernet (AES-128-CBC + HMAC-SHA256) using a
master key stored in the OS credential store (Windows Credential Manager
via DPAPI, macOS Keychain). Copying the SQLite DB to another machine is
useless without access to the local credential store.

Design mirrors Chrome's os_crypt layer:
- Chrome: DPAPI-protected master key in Local State -> AES-256-GCM per value
- Here:   keyring-protected master key in OS store  -> Fernet per value

Encrypted values in the DB carry an "enc:1:" prefix for version detection.
Plaintext values (legacy, pre-encryption) lack the prefix and are read as-is;
``cache`` re-encrypts them in place once per process (see
``cache._migrate_plaintext_rows``).

Writes FAIL CLOSED: when the encryption layer is unavailable (missing
dependency, broken OS keyring) storing a credential raises
:class:`EncryptionUnavailableError` instead of silently degrading to
plaintext. Development machines can opt in to plaintext storage explicitly
with ``JARVIS_AUTH_ALLOW_PLAINTEXT=1``.
"""

import base64
import contextlib
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = "jarvis-auth"
_KEYRING_USERNAME = "token-cache-key"
_PREFIX = "enc:1:"
_ALLOW_PLAINTEXT_ENV = "JARVIS_AUTH_ALLOW_PLAINTEXT"

_warned_plaintext = False


class EncryptionUnavailableError(RuntimeError):
    """A credential write required encryption but the layer is unavailable."""


def _plaintext_allowed() -> bool:
    return os.environ.get(_ALLOW_PLAINTEXT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def available() -> bool:
    """True when the encryption layer is usable (deps import + key accessible)."""
    return _get_fernet() is not None


# Lazy-initialized Fernet instance
_fernet = None
_init_attempted = False
_init_lock = threading.Lock()


@contextlib.contextmanager
def _keygen_file_lock():
    """Serialize first-run key creation across processes so two concurrent first
    runs cannot mint different keys (last writer would orphan the other's data).

    Best-effort: yields even if the lock cannot be taken, so a stuck lock never
    blocks startup -- the keyring get-or-create is idempotent enough that the
    worst case is the rare original race, not a hang.
    """
    from .cache import _DB_DIR

    lockpath = os.path.join(_DB_DIR, "token-cache-key.lock")
    os.makedirs(_DB_DIR, exist_ok=True)
    fd = None
    deadline = time.time() + 10.0
    while fd is None and time.time() < deadline:
        try:
            fd = os.open(lockpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            time.sleep(0.1)
        except OSError:
            break
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(lockpath)


def _get_fernet():
    """Get or create the Fernet cipher, lazy-initialized (thread- and
    process-safe on first-run key creation)."""
    global _fernet, _init_attempted
    if _fernet is not None:
        return _fernet
    with _init_lock:
        if _fernet is not None:
            return _fernet
        if _init_attempted:
            return None
        _init_attempted = True

        try:
            import keyring
            from cryptography.fernet import Fernet
        except ImportError as e:
            logger.warning(
                "Token encryption unavailable (missing dependency: %s). "
                "Credential writes will fail unless %s=1 is set. "
                "Install with: uv add keyring cryptography",
                e.name,
                _ALLOW_PLAINTEXT_ENV,
            )
            return None

        try:
            key_b64 = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
            if key_b64 is None:
                with _keygen_file_lock():
                    # Re-read inside the lock: a peer may have created it while we waited.
                    key_b64 = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
                    if key_b64 is None:
                        key_b64 = base64.b64encode(os.urandom(32)).decode("ascii")
                        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, key_b64)
                        logger.info(
                            "Generated new token encryption key (stored in OS credential manager)"
                        )

            fernet_key = base64.urlsafe_b64encode(base64.b64decode(key_b64))
            _fernet = Fernet(fernet_key)
            return _fernet

        except Exception as e:
            logger.warning(
                "Token encryption unavailable (keyring error: %s). "
                "Credential writes will fail unless %s=1 is set.",
                e,
                _ALLOW_PLAINTEXT_ENV,
            )
            return None


def encrypt_value(plaintext: str) -> str:
    """Encrypt a token value (fail closed).

    Returns prefixed ciphertext. When the encryption layer is unavailable the
    write raises :class:`EncryptionUnavailableError` instead of silently
    degrading to plaintext - encryption at rest is the cache's contract.
    Setting ``JARVIS_AUTH_ALLOW_PLAINTEXT=1`` opts a machine into plaintext
    storage explicitly (development only).
    """
    global _warned_plaintext
    fernet = _get_fernet()
    if fernet is None:
        if _plaintext_allowed():
            if not _warned_plaintext:
                _warned_plaintext = True
                logger.warning(
                    "Token encryption unavailable and %s is set - storing "
                    "credentials in PLAINTEXT.",
                    _ALLOW_PLAINTEXT_ENV,
                )
            return plaintext
        raise EncryptionUnavailableError(
            "Token encryption is unavailable (keyring/cryptography import or the OS "
            "credential store failed) - refusing to store credentials in plaintext. "
            "Repair the OS keyring / reinstall the 'keyring' and 'cryptography' "
            f"packages, or set {_ALLOW_PLAINTEXT_ENV}=1 to explicitly accept "
            "plaintext storage on this machine."
        )
    encrypted = fernet.encrypt(plaintext.encode("utf-8"))
    return _PREFIX + encrypted.decode("ascii")


def decrypt_value(stored: str) -> str | None:
    """Decrypt a stored value. Handles both encrypted (prefixed) and legacy plaintext."""
    if not stored.startswith(_PREFIX):
        # Legacy plaintext value -- return as-is
        return stored

    fernet = _get_fernet()
    if fernet is None:
        logger.error(
            "Cannot decrypt token: encryption layer unavailable. "
            "Ensure 'keyring' and 'cryptography' are installed."
        )
        return None

    try:
        ciphertext = stored[len(_PREFIX) :].encode("ascii")
        return fernet.decrypt(ciphertext).decode("utf-8")
    except Exception as e:
        logger.error("Token decryption failed: %s", e)
        return None


def is_encrypted(value: str) -> bool:
    """Check if a value is already encrypted."""
    return value.startswith(_PREFIX)
