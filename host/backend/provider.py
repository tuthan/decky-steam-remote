"""Narrow Sunshine provider boundary; process ownership stays with Decky Sunshine."""

from __future__ import annotations

import concurrent.futures
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Protocol

from .identity import opaque_id


class ProviderError(RuntimeError):
    pass


class DeckySunshineProcessObserver:
    """Read-only Sunshine observation used before the owner bridge is loaded.

    Decky Sunshine owns process control. This observer only checks the process
    name so the host can report a useful running/stopped state immediately
    after a Decky reload, when no frontend owner bridge has registered yet.
    """

    provider_name = "decky-sunshine"
    contract_version = "process-observer-v1"

    def get_status(self) -> dict[str, Any]:
        try:
            result = subprocess.run(
                ["pgrep", "-x", "sunshine"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProviderError(f"local Sunshine process observation failed: {str(exc)[:180]}") from exc
        if result.returncode not in (0, 1):
            detail = (result.stderr or "").strip()
            suffix = f": {detail[:160]}" if detail else ""
            raise ProviderError(f"local Sunshine process observation failed{suffix}")
        return {"running": result.returncode == 0, "reason": ""}

    def ensure_running(self) -> dict[str, Any]:
        raise ProviderError("Decky Sunshine owner bridge is not connected")


class SunshineProvider(Protocol):
    provider_name: str
    contract_version: str

    def get_status(self) -> dict[str, Any]:
        """Return {running: bool}; the remote adapter must not trigger recovery."""

    def ensure_running(self) -> dict[str, Any]:
        """Ask the owning plugin to ensure the process is running."""


class BridgeSunshineProvider:
    """Call the installed Decky Sunshine owner through the Steam bridge.

    Decky plugins do not have a stable backend-to-backend API. The frontend
    therefore calls the owner plugin through Decky Loader and reports the
    bounded result back through ``BridgeBroker``. This class deliberately
    exposes only the two passive/owner methods used by ``SunshineMonitor``.
    """

    provider_name = "decky-sunshine"
    contract_version = "loader-call-v1"

    def __init__(self, bridge: Any):
        self.bridge = bridge

    def get_status(self) -> dict[str, Any]:
        result = self.bridge.request(
            "sunshine_status", {}, opaque_id("sunshine-status-"), timeout=4
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            reason = result.get("reason", "owner status call failed") if isinstance(result, dict) else "owner status call failed"
            raise ProviderError(str(reason)[:256])
        if not isinstance(result.get("running"), bool):
            raise ProviderError("owner returned an invalid running state")
        return {"running": result["running"], "reason": str(result.get("reason", ""))[:256]}

    def ensure_running(self) -> dict[str, Any]:
        result = self.bridge.request(
            "sunshine_restart", {}, opaque_id("sunshine-restart-"), timeout=8
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            reason = result.get("reason", "owner restart call failed") if isinstance(result, dict) else "owner restart call failed"
            raise ProviderError(str(reason)[:256])
        return {"accepted": True, "outcome": str(result.get("outcome", "method_returned"))[:64]}


class ProviderAdapter:
    """Runtime adapter for a companion/owner callback with explicit methods."""

    def __init__(self, owner: Any, provider_name: str = "decky-sunshine", contract_version: str = "1"):
        if not callable(getattr(owner, "get_status", None)) or not callable(getattr(owner, "ensure_running", None)):
            raise ProviderError("provider must expose passive get_status and ensure_running")
        self.owner = owner
        self.provider_name = provider_name
        self.contract_version = contract_version

    def get_status(self) -> dict[str, Any]:
        value = self.owner.get_status()
        if not isinstance(value, dict) or not isinstance(value.get("running"), bool):
            raise ProviderError("provider returned an invalid status")
        return {"running": value["running"], "reason": str(value.get("reason", ""))[:256]}

    def ensure_running(self) -> dict[str, Any]:
        value = self.owner.ensure_running()
        if value is None:
            return {"accepted": True}
        if not isinstance(value, dict):
            raise ProviderError("provider returned an invalid recovery result")
        return {str(key): value[key] for key in list(value)[:16]}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SunshineMonitor:
    """One host sample for all clients, with explicit stale/unknown states."""

    SAMPLE_INTERVAL = 5.0
    STALE_AFTER = 15.0
    PROVIDER_TIMEOUT = 2.0

    def __init__(self, clock=time.monotonic, wall_clock=time.time):
        self.clock = clock
        self.wall_clock = wall_clock
        self._lock = threading.RLock()
        self._provider: SunshineProvider | None = None
        self._observer: SunshineProvider | None = None
        self._enabled = False
        self._sample: dict[str, Any] | None = None
        self._active_operation_id: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="sunshine-provider")

    def close(self) -> None:
        with self._lock:
            thread = self._thread
        self.set_enabled(False)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def set_provider(self, provider: SunshineProvider | None) -> None:
        with self._lock:
            self._provider = provider
            if self._enabled:
                self._sample = None

    def set_observer(self, observer: SunshineProvider | None) -> None:
        with self._lock:
            self._observer = observer
            if self._enabled and self._provider is None:
                self._sample = None

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self._lock:
            self._enabled = enabled
            if not enabled:
                self._active_operation_id = None
                self._sample = None
                self._stop.set()
                thread = self._thread
                self._thread = None
            else:
                self._stop.clear()
                if getattr(self._executor, "_shutdown", False):
                    # A role transition can stop the host monitor and later
                    # enable Server again on the same service object.
                    self._executor = concurrent.futures.ThreadPoolExecutor(
                        max_workers=2, thread_name_prefix="sunshine-provider"
                    )
                thread = None
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._run, name="sunshine-monitor", daemon=True)
                    thread = self._thread
        if thread is not None and enabled:
            thread.start()

    def set_operation(self, operation_id: str | None) -> None:
        with self._lock:
            self._active_operation_id = operation_id

    def provider_ready(self) -> bool:
        with self._lock:
            return self._provider is not None and self._enabled

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def provider(self) -> SunshineProvider | None:
        with self._lock:
            return self._provider

    def status_provider(self) -> SunshineProvider | None:
        with self._lock:
            return self._provider or self._observer

    def refresh_now(self, *, include_operation: bool = True) -> dict[str, Any]:
        with self._lock:
            provider = self._provider
            observer = self._observer
            enabled = self._enabled
        if not enabled:
            return self.public(include_operation=include_operation)
        status_provider = provider or observer
        if status_provider is None:
            with self._lock:
                self._sample = {"state": "unavailable", "checked_monotonic": self.clock(), "checked_at": None, "reason": "Decky Sunshine provider is not connected"}
            return self.public(include_operation=include_operation)
        try:
            result = self._call(status_provider.get_status)
            now = self.clock()
            with self._lock:
                self._sample = {
                    "state": "running" if result.get("running") else "stopped",
                    "checked_monotonic": now,
                    "checked_at": utc_now(),
                    "reason": result.get("reason") or None,
                }
        except Exception as exc:
            with self._lock:
                self._sample = {
                    "state": "unknown",
                    "checked_monotonic": self.clock(),
                    "checked_at": None,
                    "reason": f"provider status unavailable: {str(exc)[:180]}",
                }
        return self.public(include_operation=include_operation)

    def public(self, *, include_operation: bool = True) -> dict[str, Any]:
        with self._lock:
            enabled = self._enabled
            provider = self._provider
            observer = self._observer
            sample = dict(self._sample) if self._sample else None
            active = self._active_operation_id
        status_provider = provider or observer
        if not enabled:
            return {
                "enabled": False, "provider": None, "state": "disabled", "checked_at": None,
                "age_ms": None, "stale": False, "reason": None, "operation_id": None,
            }
        if active and include_operation:
            state = "restarting"
        elif status_provider is None:
            state = "unavailable"
        elif sample is None:
            state = "unknown"
        else:
            age_ms = max(0, int((self.clock() - float(sample.get("checked_monotonic", self.clock()))) * 1000))
            if age_ms > int(self.STALE_AFTER * 1000):
                state = "unknown"
            else:
                state = sample.get("state", "unknown")
        age_ms = None
        checked_at = None
        reason = None
        stale = False
        if sample:
            age_ms = max(0, int((self.clock() - float(sample.get("checked_monotonic", self.clock()))) * 1000))
            checked_at = sample.get("checked_at")
            stale = age_ms > int(self.STALE_AFTER * 1000)
            reason = sample.get("reason")
        if state == "unknown" and stale and not reason:
            reason = "cached provider status is stale"
        if state == "unavailable" and not reason:
            reason = "Decky Sunshine provider is not connected"
        return {
            "enabled": True,
            "provider": getattr(status_provider, "provider_name", None),
            "state": state,
            "checked_at": checked_at,
            "age_ms": age_ms,
            "stale": stale,
            "reason": reason,
            "operation_id": active if include_operation else None,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh_now()
            self._stop.wait(self.SAMPLE_INTERVAL)

    def _call(self, function):
        future = self._executor.submit(function)
        return future.result(timeout=self.PROVIDER_TIMEOUT)
