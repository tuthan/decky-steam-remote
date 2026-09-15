"""Thread-safe bridge between the Decky backend and its Steam frontend."""

from __future__ import annotations

import collections
import threading
import time
from typing import Any

from .display import DisplayError, normalize_snapshot
from .identity import opaque_id


class BridgeError(RuntimeError):
    pass


class BridgeBroker:
    """Commands are fixed verbs; the frontend is the only Steam caller."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._commands: collections.deque[dict[str, Any]] = collections.deque()
        self._pending: dict[str, tuple[threading.Event, dict[str, Any] | None]] = {}
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_at = 0.0
        self._stopped = False

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._commands.clear()
            pending = list(self._pending.values())
            self._pending.clear()
            for event, _ in pending:
                event.set()

    def start(self) -> None:
        """Re-arm the broker after a role transition or plugin reload."""
        with self._lock:
            self._stopped = False

    def report_snapshot(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            normalized = normalize_snapshot(value)
        except DisplayError as exc:
            raise BridgeError(str(exc)) from exc
        with self._lock:
            if self._stopped:
                raise BridgeError("bridge is stopped")
            self._snapshot = normalized
            self._snapshot_at = self._clock()
            return {"accepted": True, "reported_at": self._snapshot_at}

    def snapshot(self, max_age: float = 8.0) -> tuple[dict[str, Any] | None, int | None]:
        with self._lock:
            if self._snapshot is None:
                return None, None
            age_ms = max(0, int((self._clock() - self._snapshot_at) * 1000))
            if age_ms > int(max_age * 1000):
                return None, age_ms
            return self._copy(self._snapshot), age_ms

    def request(self, kind: str, payload: dict[str, Any], operation_id: str, timeout: float = 8.0) -> dict[str, Any]:
        if kind not in {"set_mode", "power", "sunshine_status", "sunshine_restart"}:
            raise BridgeError("unsupported bridge command")
        if not isinstance(payload, dict):
            raise BridgeError("bridge command payload is invalid")
        if kind == "power" and payload.get("action") not in {"suspend", "restart", "shutdown"}:
            raise BridgeError("power action is unsupported")
        command_id = opaque_id("bridge-")
        event = threading.Event()
        with self._lock:
            if self._stopped:
                raise BridgeError("bridge is stopped")
            self._pending[command_id] = (event, None)
            self._commands.append({
                "command_id": command_id,
                "operation_id": operation_id,
                "kind": kind,
                "payload": self._copy(payload),
                "expires_at": self._clock() + timeout,
            })
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(command_id, None)
            raise BridgeError("frontend bridge command timed out")
        with self._lock:
            pair = self._pending.pop(command_id, None)
        if pair is None or pair[1] is None:
            raise BridgeError("frontend bridge ended without a result")
        return pair[1]

    def next_command(self) -> dict[str, Any] | None:
        with self._lock:
            now = self._clock()
            while self._commands:
                command = self._commands.popleft()
                if command["expires_at"] <= now:
                    pair = self._pending.pop(command["command_id"], None)
                    if pair:
                        pair[0].set()
                    continue
                return self._copy(command)
            return None

    def report_result(self, command_id: str, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise BridgeError("bridge result is invalid")
        with self._lock:
            pair = self._pending.get(command_id)
            if pair is None:
                raise BridgeError("bridge command is unknown or expired")
            bounded = self._copy(result)
            event = pair[0]
            self._pending[command_id] = (event, bounded)
            event.set()
            return {"accepted": True}

    @staticmethod
    def _copy(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): BridgeBroker._copy(item) for key, item in value.items()}
        if isinstance(value, list):
            return [BridgeBroker._copy(item) for item in value[:256]]
        if isinstance(value, str):
            return value[:65536]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:256]
