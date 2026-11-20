"""An encrypted store for what lets the assistant act as the user: OAuth tokens (Google) and the login states
of sites it drives with a browser. Passwords are not meant to be kept here at all: the user logs in on the
real site, and only what that login produced is stored.

- Encrypted with Fernet (AES-128-CBC + HMAC-SHA256). The key is a file of its own, created on first use.
- Both files live outside the repository, by default in ~/.agentkit (AGENTKIT_SECRETS_DIR), mode 600.
- Values are never logged and never returned over the API: names() lists what is there, without values.
  A value can be replaced or deleted, not read back, except by the code that uses it (get()).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DEFAULT_DIR = Path(os.environ.get("AGENTKIT_SECRETS_DIR", Path.home() / ".agentkit"))


class SecretStoreError(Exception):
    pass


def _write_private(path: Path, data: bytes) -> None:
    """Writes atomically, readable by the owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class SecretStore:
    def __init__(self, directory: Path | str | None = None):
        self.dir = Path(directory) if directory else DEFAULT_DIR
        self.key_path = self.dir / "master.key"
        self.path = self.dir / "secrets.enc"
        self._lock = threading.Lock()
        self._fernet = None

    def __repr__(self):
        return f"SecretStore({self.dir})"   # never the values

    # ---- the key ---------------------------------------------------------------------------
    def _cipher(self) -> Fernet:
        if self._fernet is None:
            if self.key_path.exists():
                if self.key_path.stat().st_mode & 0o077:
                    raise SecretStoreError(f"{self.key_path} is readable by others; chmod 600 it")
                key = self.key_path.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                _write_private(self.key_path, key + b"\n")
            self._fernet = Fernet(key)
        return self._fernet

    # ---- the whole store -------------------------------------------------------------------
    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self._cipher().decrypt(self.path.read_bytes()))
        except InvalidToken:
            raise SecretStoreError("the secret store cannot be decrypted with this key") from None

    def _save(self, entries: dict) -> None:
        _write_private(self.path, self._cipher().encrypt(json.dumps(entries, ensure_ascii=False).encode()))

    # ---- entries ---------------------------------------------------------------------------
    def put(self, name: str, value, kind: str = "token", meta: dict | None = None) -> None:
        if not name:
            raise SecretStoreError("a secret needs a name")
        with self._lock:
            entries = self._load()
            entries[name] = {"value": value, "kind": kind, "meta": dict(meta or {}),
                             "created_at": entries.get(name, {}).get("created_at", time.time()),
                             "updated_at": time.time()}
            self._save(entries)

    def get(self, name: str, default=None):
        """For the code that uses the secret (an adapter), never for an API response."""
        with self._lock:
            entry = self._load().get(name)
        return entry["value"] if entry else default

    def delete(self, name: str) -> bool:
        with self._lock:
            entries = self._load()
            if name not in entries:
                return False
            del entries[name]
            self._save(entries)
            return True

    def names(self) -> list[dict]:
        """What is stored, without the values: [{name, kind, meta, created_at, updated_at}]."""
        with self._lock:
            entries = self._load()
        return [{"name": name, "kind": e.get("kind"), "meta": e.get("meta", {}),
                 "created_at": e.get("created_at"), "updated_at": e.get("updated_at")}
                for name, e in sorted(entries.items())]


_default = None


def get_store() -> SecretStore:
    global _default
    if _default is None:
        _default = SecretStore()
    return _default
