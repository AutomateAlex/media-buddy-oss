"""Encrypted API-key storage backed by the OS keychain.

Phase 2.11g — replaces plaintext .env storage. On Windows we use the
Credential Manager via DPAPI; macOS uses Keychain; Linux uses Secret
Service if available.

Public API:
    get(env_var)            → Optional[str]
    set(env_var, value)     → None
    delete(env_var)         → None
    list_keys()             → list[str]
    is_available()          → bool   # True iff backend is real (not "fail")
    migrate_env_file(path)  → list[str]  # one-time migrate from .env to keyring

We also re-populate ``os.environ`` whenever a key is read/set/deleted so
the rest of the code (which still reads ``os.environ.get(...)`` per
provider) keeps working unchanged.

If the OS keychain is unavailable (headless Linux without secret-service,
inside Docker, etc.) we fall back to a permission-700 file under
``~/.media-buddy-oss/secrets.json``. That's still better than committing
keys to ``.env`` in the project tree, and never leaves the user account.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Service name used in the keychain. All Media Buddy keys live under this
# service so users can find/wipe them in the OS keychain UI.
SERVICE_NAME = "media-buddy-oss"

# Fallback file (used iff keyring backend is unavailable).
_FALLBACK_DIR = Path.home() / ".media-buddy-oss"
_FALLBACK_FILE = _FALLBACK_DIR / "secrets.json"

# Cloud desktop auth tokens are short-lived app state, not user-supplied
# provider master keys. In packaged Windows smoke tests WinVault accepted
# writes without surfacing errors, but the next CloudAuth instance could not
# read those values back, so /api/auth/pair returned 200 and /api/auth/status
# immediately fell back to unauthenticated. Keep these tokens in the local
# fallback file for deterministic packaged-app behavior.
_FORCE_FALLBACK_PREFIXES = ("media_buddy.",)


def _force_fallback(env_var: str) -> bool:
    return env_var.startswith(_FORCE_FALLBACK_PREFIXES)


def _keyring_available() -> bool:
    """Return True iff a real keyring backend is wired up."""
    try:
        import keyring  # noqa: F401
        from keyring.backends.fail import Keyring as FailKeyring
        backend = keyring.get_keyring()
        # The 'fail' backend is keyring's no-op fallback; it's a "available
        # but does nothing" placeholder. Treat it as unavailable.
        return not isinstance(backend, FailKeyring)
    except Exception as e:
        logger.warning("keyring backend probe failed: %s", e)
        return False


def is_available() -> bool:
    return _keyring_available()


# ---------- File-fallback helpers ----------

def _read_fallback() -> dict[str, str]:
    if not _FALLBACK_FILE.exists():
        return {}
    try:
        return json.loads(_FALLBACK_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("fallback secrets read failed: %s", e)
        return {}


def _write_fallback(data: dict[str, str]) -> None:
    _FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    _FALLBACK_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Best-effort owner-only permission. Windows ignores chmod but ACLs
    # already restrict ~/.media-buddy-oss to the user.
    try:
        os.chmod(_FALLBACK_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


# ---------- Public API ----------

def get(env_var: str) -> Optional[str]:
    if _force_fallback(env_var):
        return _read_fallback().get(env_var) or None
    if _keyring_available():
        try:
            import keyring
            v = keyring.get_password(SERVICE_NAME, env_var)
            if v:
                return v
        except Exception as e:
            logger.warning("keyring get(%s) failed: %s — using fallback", env_var, e)
    return _read_fallback().get(env_var) or None


def set(env_var: str, value: str) -> None:  # noqa: A001
    if _force_fallback(env_var):
        data = _read_fallback()
        data[env_var] = value
        _write_fallback(data)
        os.environ[env_var] = value
        return
    if _keyring_available():
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, env_var, value)
            os.environ[env_var] = value
            return
        except Exception as e:
            logger.warning("keyring set(%s) failed: %s — using fallback", env_var, e)
    data = _read_fallback()
    data[env_var] = value
    _write_fallback(data)
    os.environ[env_var] = value


def delete(env_var: str) -> None:
    if _force_fallback(env_var):
        data = _read_fallback()
        data.pop(env_var, None)
        _write_fallback(data)
        os.environ.pop(env_var, None)
        return
    if _keyring_available():
        try:
            import keyring
            try:
                keyring.delete_password(SERVICE_NAME, env_var)
            except Exception:
                pass  # already gone
            os.environ.pop(env_var, None)
            return
        except Exception as e:
            logger.warning("keyring delete(%s) failed: %s — using fallback", env_var, e)
    data = _read_fallback()
    data.pop(env_var, None)
    _write_fallback(data)
    os.environ.pop(env_var, None)


def list_keys() -> list[str]:
    """Return env-var names currently stored. With keychain we can't
    enumerate (most OS keychains lack a query API), so we return what's
    in the fallback file plus what's already in os.environ that we
    recognize as known keys."""
    keys = set(_read_fallback().keys())
    # Also include any os.environ keys that were loaded by ``populate_environ``.
    for k in os.environ:
        if "_API_KEY" in k or k.endswith("_KEY") or k.endswith("_TOKEN") or k == "OPENROUTER_API_KEY":
            keys.add(k)
    return sorted(keys)


def populate_environ(known_keys: list[str]) -> int:
    """At startup: for each known key name, copy keychain → os.environ.

    Returns count of keys populated.
    """
    n = 0
    for k in known_keys:
        if os.environ.get(k):
            continue  # already set (e.g. via system env) — don't override
        v = get(k)
        if v:
            os.environ[k] = v
            n += 1
    return n


def migrate_env_file(env_path: Path, key_names: list[str]) -> list[str]:
    """One-time migration: copy any secrets from a legacy .env file into
    the keychain, then remove them from the .env (leaves non-secret keys
    like OLLAMA_BASE_URL alone).

    Returns the list of env vars migrated. Idempotent — running twice is
    safe.
    """
    if not env_path.exists():
        return []
    migrated: list[str] = []
    try:
        from dotenv import dotenv_values
        existing = dotenv_values(str(env_path))
    except Exception as e:
        logger.warning("dotenv read failed during migration: %s", e)
        return []
    for k in key_names:
        v = (existing.get(k) or "").strip()
        if not v:
            continue
        # Skip placeholder / sentinel values
        if v.startswith(("...", "<", "your_")) or len(v) < 4:
            continue
        try:
            set(k, v)
            migrated.append(k)
        except Exception as e:
            logger.warning("migration set(%s) failed: %s", k, e)
    if migrated:
        # Remove migrated keys from the .env (leave non-secret entries)
        try:
            from dotenv import unset_key
            for k in migrated:
                unset_key(str(env_path), k)
            logger.info(
                "Migrated %d secret(s) from %s into keychain: %s",
                len(migrated), env_path, migrated,
            )
        except Exception as e:
            logger.warning("could not clean migrated keys from %s: %s", env_path, e)
    return migrated
