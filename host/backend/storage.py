"""Private, atomic JSON state for the Decky host."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import copy
from pathlib import Path
from typing import Any, Callable


class StateError(RuntimeError):
    pass


def _check_owner(path: Path, expected_uid: int | None = None) -> os.stat_result:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise StateError(f"refusing symlink state path: {path}")
    if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
        raise StateError(f"refusing non-regular state path: {path}")
    uid = os.geteuid() if expected_uid is None else expected_uid
    if info.st_uid != uid:
        raise StateError(f"unexpected state owner: {path}")
    return info


class StateStore:
    """A small locked state store with 0700/0600 permissions and atomic saves."""

    def __init__(self, root: str | os.PathLike[str], initial: Callable[[], dict[str, Any]]):
        self.root = Path(root)
        self.path = self.root / "state.json"
        self._lock = threading.RLock()
        self._ensure_root()
        self._state = self._load(initial)

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = _check_owner(self.root)
        if not stat.S_ISDIR(info.st_mode):
            raise StateError("state root is not a directory")
        os.chmod(self.root, 0o700)

    def _load(self, initial: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if not os.path.lexists(self.path):
            value = initial()
            self._write(value)
            return value
        _check_owner(self.path)
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateError(f"unable to read private state: {exc}") from exc
        if not isinstance(value, dict):
            raise StateError("private state must be a JSON object")
        os.chmod(self.path, 0o600)
        return value

    def _write(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, indent=2).encode() + b"\n"
        if len(encoded) > 4 * 1024 * 1024:
            raise StateError("private state exceeds 4 MiB")
        if os.path.lexists(self.path):
            _check_owner(self.path)
        fd, temp_name = tempfile.mkstemp(prefix=".state-", dir=self.root)
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return json.loads(json.dumps(self._state.get(key, default)))

    def replace(self, value: dict[str, Any]) -> None:
        with self._lock:
            self._write(value)
            self._state = value

    def mutate(self, function: Callable[[dict[str, Any]], Any]) -> Any:
        with self._lock:
            # Apply the callback to a detached candidate. If serialization,
            # fsync, or replacement fails, the in-memory state must remain in
            # sync with the durable file so later requests cannot observe a
            # mutation that was never committed.
            candidate = copy.deepcopy(self._state)
            result = function(candidate)
            self._write(candidate)
            self._state = candidate
            return result

    @property
    def lock(self) -> threading.RLock:
        return self._lock
