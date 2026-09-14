"""Decky entry point for the SteamOS Remote host plugin."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import decky


def _log(level: str, message: str, *args) -> None:
    """Write diagnostics to Decky's plugin log without breaking test imports."""
    logger = getattr(decky, "logger", None)
    method = getattr(logger, level, None)
    if not callable(method):
        return
    try:
        method(message, *args)
    except Exception:
        # Logging must never prevent the plugin from loading or serving RPCs.
        pass


# Decky's sandbox does not always add the installed plugin directory to
# sys.path. Make sibling packages such as backend importable regardless of
# the loader's current working directory.
PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


try:
    from backend import __version__ as BACKEND_VERSION
    from backend.service import HostService
except Exception:
    _log("exception", "SteamOS Remote failed to import backend.service")
    raise


def _state_root() -> Path:
    for name in ("DECKY_PLUGIN_SETTINGS_DIR", "DECKY_PLUGIN_RUNTIME_DIR", "DECKY_PLUGIN_DATA_DIR"):
        value = getattr(decky, name, None)
        if value:
            return Path(value) / "steamos-remote"
    return Path("/tmp") / "steamos-remote-decky-state"


def _build_service() -> HostService:
    root = _state_root()
    version = getattr(decky, "DECKY_PLUGIN_VERSION", None) or BACKEND_VERSION
    _log(
        "info",
        "SteamOS Remote loading version=%s api_version=0 mode=legacy state_root=%s",
        version,
        root,
    )
    try:
        service = HostService(root)
    except Exception:
        _log("exception", "SteamOS Remote failed to construct HostService")
        raise
    _log("info", "SteamOS Remote host identity loaded host_id=%s", service.host_id)
    return service


def _with_diagnostics(result):
    if not isinstance(result, dict):
        return result
    enriched = dict(result)
    enriched["diagnostics"] = {
        "version": getattr(decky, "DECKY_PLUGIN_VERSION", None) or BACKEND_VERSION,
        "api_mode": "legacy",
        "log_path": getattr(decky, "DECKY_PLUGIN_LOG", None),
    }
    return enriched


async def _threaded_call(method: str, function, *args):
    """Run blocking service work while logging failures without request data."""
    try:
        return await asyncio.to_thread(function, *args)
    except Exception:
        _log("exception", "SteamOS Remote RPC failed method=%s", method)
        raise


class Plugin:
    # plugin.json uses the legacy Decky API because the dependency-free
    # frontend calls the legacy serverAPI.callPluginMethod adapter. In that
    # mode Decky passes the Plugin class as self instead of instantiating it,
    # so the service must be a class attribute.
    service = _build_service()

    async def _main(self):
        _log("info", "SteamOS Remote backend startup begin")
        try:
            status = await _threaded_call("startup", self.service.start)
        except Exception:
            _log("exception", "SteamOS Remote backend startup failed")
            return
        listener = status.get("listener", {}) if isinstance(status, dict) else {}
        tls = status.get("tls", {}) if isinstance(status, dict) else {}
        _log(
            "info",
            "SteamOS Remote backend startup complete listener_running=%s listener_error=%s tls_ready=%s",
            listener.get("running"),
            listener.get("error"),
            tls.get("ready"),
        )

    async def get_settings(self):
        result = await _threaded_call("get_settings", self.service.get_settings)
        return _with_diagnostics(result)

    async def update_settings(self, changes):
        return await _threaded_call("update_settings", self.service.update_settings, changes)

    async def create_pairing(self, requested_scopes=None):
        return await _threaded_call("create_pairing", self.service.create_pairing, requested_scopes)

    async def create_pairing_code(self, requested_scopes=None):
        return await _threaded_call("create_pairing_code", self.service.create_pairing_code, requested_scopes)

    async def list_pairings(self):
        return self.service.list_pairings()

    async def approve_pairing(self, pairing_id, scopes=None):
        return self.service.approve_pairing(pairing_id, scopes)

    async def reject_pairing(self, pairing_id):
        return self.service.reject_pairing(pairing_id)

    async def revoke_client(self, client_id):
        return self.service.revoke_client(client_id)

    async def set_sunshine_provider(self, provider):
        return self.service.set_sunshine_provider(provider)

    async def report_sunshine_owner(self, report):
        return self.service.report_sunshine_owner(report)

    async def next_bridge_command(self):
        return self.service.next_bridge_command()

    async def report_bridge_result(self, command_id, result):
        return self.service.report_bridge_result(command_id, result)

    async def report_bridge_snapshot(self, snapshot):
        return self.service.report_bridge_snapshot(snapshot)

    async def local_status(self):
        result = await _threaded_call("local_status", self.service.get_local_status)
        return _with_diagnostics(result)

    async def _unload(self):
        _log("info", "SteamOS Remote backend shutdown begin")
        try:
            await _threaded_call("shutdown", self.service.stop)
        finally:
            _log("info", "SteamOS Remote backend shutdown complete")
