"""Pluggable persistence for provider state that is created or rotated at runtime.

What goes in here, one section per provider:

* "truelayer": {access_token, access_token_expires_at, refresh_token, connected_at,
  seed_fingerprint, updated_at}. TrueLayer may ROTATE the refresh token on any
  refresh, so the provider saves after every token response. Each bank is a
  separate TrueLayer connection with its own refresh token: do not point two
  different bank connections at the same store.
* "enablebanking": the session id + consent expiry written by the link helper.

Static secrets (client secret, API keys, private keys) come from environment
variables and are never written here.

Interface (implement these two methods to plug in a cloud secret store):

    class TokenStore:
        def load(self) -> dict: ...          # whole document, {} if absent
        def save(self, data: dict) -> None   # replace the whole document

Select the implementation with NATIONWIDE_TOKEN_STORE:

    file              (default) JSON file at NATIONWIDE_TOKEN_FILE
    env               read-only JSON from NATIONWIDE_TOKEN_JSON
    memory            nothing persisted (tests)
    package.module:ClassName   your own class, constructed with no arguments

SECURITY: the last form imports and runs the code named in the environment, and
the file store keeps tokens in PLAINTEXT. The file is written atomically
(fsync + rename), with mode 0600 on POSIX and an owner-only ACL on Windows.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ConfigError, TokenStoreError

log = logging.getLogger("nationwide_mcp.token_store")

# put() is read-modify-write; one lock for the whole process keeps two threads
# from losing each other's section. (Custom stores need not call __init__, so
# the lock is not per instance.)
_PUT_LOCK = threading.RLock()


class TokenStore:
    """Base interface. Subclasses must be safe to call from one process."""

    def load(self) -> dict[str, Any]:
        raise NotImplementedError

    def save(self, data: dict[str, Any]) -> None:
        raise NotImplementedError

    # Convenience helpers built on load/save -------------------------------
    def get(self, section: str) -> dict[str, Any]:
        value = self.load().get(section)
        return dict(value) if isinstance(value, dict) else {}

    def put(self, section: str, value: Mapping[str, Any]) -> None:
        with _PUT_LOCK:
            data = self.load()
            data[section] = dict(value)
            self.save(data)

    def clear(self) -> None:
        """Forget everything (used by ``nationwide-mcp-link reset``)."""
        with _PUT_LOCK:
            self.save({})


class MemoryTokenStore(TokenStore):
    def __init__(self, data: dict[str, Any] | None = None):
        self._data: dict[str, Any] = json.loads(json.dumps(data or {}))

    def load(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))

    def save(self, data: dict[str, Any]) -> None:
        self._data = json.loads(json.dumps(data))


def _restrict_to_current_user(path: str) -> None:
    """Owner-only access: chmod 0600 on POSIX, an explicit single-user ACL on Windows."""
    if sys.platform != "win32":
        os.chmod(path, 0o600)
        return
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    if not user:
        log.warning("Could not determine the Windows user; the token file keeps its inherited permissions.")
        return
    account = domain + "\\" + user if domain else user
    try:
        done = subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{account}:(F)"],
            capture_output=True,
            timeout=15,
            check=False,
        )
        failed = done.returncode != 0
    except (OSError, subprocess.SubprocessError):
        failed = True
    if failed:
        log.warning("Could not restrict the token file's ACL with icacls; it keeps its inherited permissions.")


class FileTokenStore(TokenStore):
    """JSON file (plaintext), written atomically, readable by the owner only."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser()

    def load(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise TokenStoreError(f"Token file {self.path} is unreadable ({type(exc).__name__}).") from None
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tokens-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                _restrict_to_current_user(tmp)  # before the rename: the ACL travels with the file
            except OSError:
                log.warning("Could not restrict permissions on the token file.")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise TokenStoreError(f"Token file {self.path} could not be written ({type(exc).__name__}).") from None
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise TokenStoreError(f"Token file {self.path} could not be deleted ({type(exc).__name__}).") from None


class EnvTokenStore(TokenStore):
    """Read-only store for platforms where state is injected as a secret.

    Reads JSON from NATIONWIDE_TOKEN_JSON. Writes are kept in memory for the
    life of the process and a warning is logged (without the content), because
    an env var cannot be updated from inside the process.
    """

    def __init__(self, env: Mapping[str, str] | None = None, var: str = "NATIONWIDE_TOKEN_JSON"):
        raw = (env if env is not None else os.environ).get(var, "")
        try:
            self._data: dict[str, Any] = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{var} is not valid JSON") from exc
        self._var = var

    def load(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))

    def save(self, data: dict[str, Any]) -> None:
        self._data = json.loads(json.dumps(data))
        log.warning(
            "Token state changed but %s is read-only; the change lasts only until this process exits.",
            self._var,
        )


def default_token_path() -> Path:
    return Path.home() / ".nationwide-mcp" / "tokens.json"


def build_token_store(env: Mapping[str, str] | None = None) -> TokenStore:
    env = env if env is not None else os.environ
    kind = (env.get("NATIONWIDE_TOKEN_STORE") or "file").strip()
    if kind == "file":
        return FileTokenStore(env.get("NATIONWIDE_TOKEN_FILE") or default_token_path())
    if kind == "env":
        return EnvTokenStore(env)
    if kind == "memory":
        return MemoryTokenStore()
    if ":" in kind:
        module_name, _, class_name = kind.partition(":")
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
        except (ImportError, AttributeError) as exc:
            raise ConfigError(f"Cannot load token store {kind!r}: {exc}") from exc
        store = cls()
        if not (hasattr(store, "load") and hasattr(store, "save")):
            raise ConfigError(f"Token store {kind!r} must define load() and save()")
        return store
    raise ConfigError(f"Unknown NATIONWIDE_TOKEN_STORE {kind!r}; use file, env, memory or package.module:ClassName")
