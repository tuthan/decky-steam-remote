"""The Decky host service: authenticated API, adapters, pairing, and recovery."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import math
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .bridge import BridgeBroker, BridgeError
from .display import (
    DisplayError,
    display_identity,
    mode_profile,
    resolve_restore_mode,
    same_display_identity,
    same_mode_profile,
)
from .identity import ensure_tls_material, opaque_id, read_boot_id, read_cpu_temperature, system_uptime_seconds
from .operations import ACTIVE_STATES, OperationJournal, iso_timestamp
from .pairing import derive_pairing_code, encode_payload
from .protocol import (
    ProtocolError,
    canonical_digest,
    client_name,
    identifier,
    normalize_pairing_code,
    pairing_session,
    verification_nonce,
    request_id,
    validate_mac,
    validate_host,
    validate_route_body,
    validate_scopes,
)
from .provider import DeckySunshineProcessObserver, BridgeSunshineProvider, ProviderAdapter, SunshineMonitor
from .storage import StateStore


DEFAULT_SETTINGS = {
    "listen_enabled": True,
    "listen_address": "0.0.0.0",
    "listen_port": 18443,
    "advertised_host": "",
    "monitor_sunshine": False,
}

_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_CHANNEL_BINDING_RE = re.compile(r"^[A-Za-z0-9_-]{16,510}={0,2}$")
MAX_PAIRING_RECORDS = 256
PAIRING_RETENTION_SECONDS = 3600.0
MAX_CLIENTS = 4


class ApiError(ProtocolError):
    pass


def _endpoint(host: str, port: int) -> str:
    rendered = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"https://{rendered}:{port}"


def _usable_ipv4_host(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        address = ipaddress.IPv4Address(value)
    except (TypeError, ValueError):
        return False
    return not (
        address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_multicast
    )


def _route_selected_ipv4() -> str | None:
    """Return the source address selected by the OS for the active route.

    UDP connect only selects a local route; it does not send a packet. The
    reserved TEST-NET destination avoids depending on an Internet service,
    while the limited-broadcast fallback still works on a host with only a
    local network route.
    """
    for destination in (("192.0.2.1", 9), ("255.255.255.255", 9)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(destination)
                host = probe.getsockname()[0]
        except (OSError, IndexError, TypeError):
            continue
        if _usable_ipv4_host(host):
            return host
    return None


def _default_route_interface() -> str | None:
    """Return the Linux interface carrying the first default IPv4 route."""
    try:
        lines = Path("/proc/net/route").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000" or not _INTERFACE_RE.fullmatch(fields[0]):
            continue
        try:
            flags = int(fields[3], 16)
        except ValueError:
            continue
        if flags & 0x1:
            return fields[0]
    return None


def _interface_mac(interface: str) -> str | None:
    if not _INTERFACE_RE.fullmatch(interface) or interface == "lo":
        return None
    try:
        value = (Path("/sys/class/net") / interface / "address").read_text(encoding="ascii").strip()
        value = validate_mac(value)
    except (OSError, UnicodeDecodeError, ProtocolError, TypeError):
        return None
    if value == "000000000000" or int(value[:2], 16) & 1:
        return None
    return value


def _active_wake_target() -> dict[str, Any]:
    """Describe the host NIC that should receive a LAN magic packet.

    This is local, read-only network inspection. The interface name is sent
    as diagnostic information; the Omarchy client selects its own active LAN
    route when transmitting the packet because interface names differ between
    machines.
    """
    route_interface = _default_route_interface()
    candidates: list[str] = []
    if route_interface:
        candidates.append(route_interface)
    try:
        candidates.extend(name for _, name in socket.if_nameindex() if name not in candidates)
    except (AttributeError, OSError):
        pass
    try:
        candidates.extend(path.name for path in Path("/sys/class/net").iterdir() if path.name not in candidates)
    except OSError:
        pass
    for interface in candidates:
        try:
            operstate = (Path("/sys/class/net") / interface / "operstate").read_text(encoding="ascii").strip().lower()
        except (OSError, UnicodeDecodeError):
            operstate = ""
        if interface != route_interface and operstate not in {"up", "unknown"}:
            continue
        mac = _interface_mac(interface)
        if mac:
            return {
                "available": True,
                "mac": mac,
                "interface": interface,
                "source_address": _route_selected_ipv4(),
                "reason": None,
            }
    return {
        "available": False,
        "mac": "",
        "interface": "",
        "source_address": _route_selected_ipv4(),
        "reason": "No active network interface with a readable hardware address",
    }


def _pairing_qr_svg_base64(payload: str) -> str | None:
    """Render the local pairing payload without placing the secret in argv."""
    encoder = "/usr/bin/qrencode"
    if not os.path.isfile(encoder):
        return None
    try:
        result = subprocess.run(
            [encoder, "-t", "SVG", "-o", "-", "-l", "L", "-m", "2"],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if not 1 <= len(result.stdout) <= 128 * 1024 or b"<svg" not in result.stdout[:1024]:
        return None
    return base64.b64encode(result.stdout).decode("ascii")


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_channel_binding(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("ascii", "strict")).hexdigest()


def _valid_channel_binding(value: Any) -> str | None:
    if not isinstance(value, str) or not _CHANNEL_BINDING_RE.fullmatch(value):
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError, binascii.Error):
        return None
    if len(decoded) < 12:
        return None
    return value


def _token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _pairing_code() -> str:
    return f"{secrets.randbelow(100_000_000):08d}"


def _display_pairing_code(value: str) -> str:
    return f"{value[:4]}-{value[4:]}"


def _constant_time_hash_match(value: str, expected_hash: str) -> bool:
    return hmac.compare_digest(_hash_secret(value), expected_hash)


def _pairing_expiry(value: Any) -> float:
    if not isinstance(value, dict):
        return 0.0
    try:
        expiry = float(value.get("expires_at", 0))
    except (TypeError, ValueError):
        return 0.0
    return expiry if math.isfinite(expiry) else 0.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _result_ok(result: Any) -> bool:
    return isinstance(result, dict) and result.get("ok", True) is True


class HostService:
    """A testable service object used by Decky's async plugin entry point."""

    def __init__(
        self,
        state_root: str | os.PathLike[str],
        *,
        bridge: BridgeBroker | None = None,
        provider: Any = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.clock = clock
        self.monotonic = monotonic
        self.store = StateStore(state_root, self._initial_state)
        stored_host_id = self.store.get("host_id")
        if not isinstance(stored_host_id, str) or not stored_host_id:
            stored_host_id = opaque_id("host-")
            self.store.mutate(lambda state: state.__setitem__("host_id", stored_host_id))
        self.host_id = stored_host_id
        self.boot_id = read_boot_id()
        self.bridge = bridge or BridgeBroker(monotonic)
        self.journal = OperationJournal(self.store, clock)
        self.monitor = SunshineMonitor(monotonic, clock)
        self.monitor.set_observer(DeckySunshineProcessObserver())
        self._sunshine_owner_reason: str | None = None
        self._mutation_lock = threading.RLock()
        self._pairing_lock = threading.RLock()
        self._rate_lock = threading.RLock()
        self._rate_buckets: dict[str, tuple[float, int]] = {}
        self._approved_tokens: dict[str, str] = {}
        self._workers: set[threading.Thread] = set()
        self._running = False
        self._server = None
        self._server_error = ""
        stored_tls = self.store.get("tls", {})
        self._tls: dict[str, Any] = stored_tls if isinstance(stored_tls, dict) else {}
        self._tls_lock = threading.RLock()
        self._tls_initialized = False
        self._preview_stop = threading.Event()
        self._preview_thread: threading.Thread | None = None
        self._prune_pairings()
        if provider is not None:
            self.set_sunshine_provider(provider)

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "host_id": opaque_id("host-"),
            "settings": dict(DEFAULT_SETTINGS),
            "clients": {},
            "pairings": {},
            "operations": {},
            "profiles": {},
            "preview": None,
            "tls": {},
        }

    # ---- lifecycle -----------------------------------------------------

    def _ensure_tls_material(self, *, force: bool = False) -> dict[str, Any]:
        with self._tls_lock:
            if self._tls_initialized and not force:
                return self._tls
            try:
                tls = ensure_tls_material(self.store.root, self.host_id)
            except Exception as exc:
                tls = {"ready": False, "reason": f"TLS initialization failed: {str(exc)[:220]}"}
            self._tls = tls
            self._tls_initialized = True
            self.store.mutate(lambda state: state.__setitem__("tls", {
                "fingerprint": tls.get("fingerprint"),
                "ready": tls.get("ready") is True,
                "reason": tls.get("reason"),
            }))
            return self._tls

    def start(self, *, start_server: bool = True) -> dict[str, Any]:
        if self._running:
            return self.get_local_status()
        self._running = True
        self._preview_stop.clear()
        # Generate/revalidate identity material in the service worker before
        # the settings RPC is polled. Status reads remain non-blocking.
        self._ensure_tls_material(force=True)
        if self.store.get("settings", {}).get("monitor_sunshine") is True:
            self.monitor.set_enabled(True)
        self._start_preview_watchdog()
        if start_server and self.store.get("settings", {}).get("listen_enabled", True):
            self._start_server()
        return self.get_local_status()

    def stop(self) -> None:
        self._running = False
        self._preview_stop.set()
        self.monitor.close()
        self.bridge.stop()
        for worker in list(self._workers):
            if worker is not threading.current_thread():
                worker.join(timeout=1)
        if self._server is not None:
            self._server.stop()
            self._server = None
        self._server_error = ""

    def _start_server(self) -> None:
        self._ensure_tls_material()
        if not self._tls.get("ready"):
            self._server_error = str(self._tls.get("reason", "TLS material is unavailable"))[:256]
            return
        try:
            from .server import HostHttpServer

            settings = self.store.get("settings", {})
            self._server = HostHttpServer(
                self,
                str(settings.get("listen_address", DEFAULT_SETTINGS["listen_address"])),
                int(settings.get("listen_port", DEFAULT_SETTINGS["listen_port"])),
                str(self._tls["certificate_path"]),
                str(self._tls["key_path"]),
            )
            self._server.start()
            self._server_error = ""
        except Exception as exc:
            self._server = None
            self._server_error = f"HTTPS listener unavailable: {str(exc)[:220]}"

    def _restart_server(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None
        self._server_error = ""
        if self._running and self.store.get("settings", {}).get("listen_enabled", True):
            self._start_server()

    # ---- local Decky methods ------------------------------------------

    def _prune_pairings(self) -> None:
        """Keep unauthenticated pairing state bounded and short-lived."""
        now = self.clock()
        pairings = self.store.get("pairings", {})
        if not isinstance(pairings, dict):
            return
        needs_prune = len(pairings) > MAX_PAIRING_RECORDS
        if not needs_prune:
            for value in pairings.values():
                if not isinstance(value, dict):
                    needs_prune = True
                    break
                expires_at = _pairing_expiry(value)
                status = value.get("status")
                if status in {"created", "pending"} and expires_at <= now:
                    needs_prune = True
                    break
                if expires_at <= now - PAIRING_RETENTION_SECONDS:
                    needs_prune = True
                    break
        if not needs_prune:
            return

        with self._pairing_lock:
            removed: list[str] = []

            def mutate(state: dict[str, Any]) -> None:
                records = state.setdefault("pairings", {})
                for pairing_id, value in list(records.items()):
                    if not isinstance(value, dict):
                        removed.append(pairing_id)
                        records.pop(pairing_id, None)
                        continue
                    expires_at = _pairing_expiry(value)
                    if value.get("status") in {"created", "pending"} and expires_at <= now:
                        value["status"] = "expired"
                    if expires_at <= now - PAIRING_RETENTION_SECONDS:
                        removed.append(pairing_id)
                        records.pop(pairing_id, None)

                if len(records) > MAX_PAIRING_RECORDS:
                    # Prefer removing terminal and oldest records. If a burst
                    # consists entirely of active requests, the oldest ones
                    # are still discarded to preserve a hard state bound.
                    ordered = sorted(
                        records.items(),
                        key=lambda item: (
                            item[1].get("status") in {"created", "pending", "approved"},
                            _pairing_expiry(item[1]),
                            item[1].get("created_at", ""),
                        ),
                    )
                    for pairing_id, _ in ordered[: max(0, len(records) - MAX_PAIRING_RECORDS)]:
                        removed.append(pairing_id)
                        records.pop(pairing_id, None)

            self.store.mutate(mutate)
            for pairing_id in removed:
                self._approved_tokens.pop(pairing_id, None)

    def get_local_status(self) -> dict[str, Any]:
        self._prune_pairings()
        stored_settings = self.store.get("settings", {})
        settings = {**DEFAULT_SETTINGS, **stored_settings} if isinstance(stored_settings, dict) else dict(DEFAULT_SETTINGS)
        clients = self.store.get("clients", {})
        pairings = self.store.get("pairings", {})
        pairing_host = self._resolve_pairing_host(settings)
        stored_tls = self.store.get("tls", {})
        tls = self._tls if self._tls else (stored_tls if isinstance(stored_tls, dict) else {})
        return {
            "protocol_version": 1,
            "host_id": self.host_id,
            "settings": settings,
            "pairing_host": pairing_host,
            "pairing_host_auto_detected": not bool(settings.get("advertised_host")),
            "wake_target": self._wake_target(),
            "tls": {
                "ready": tls.get("ready") is True,
                "fingerprint": tls.get("fingerprint"),
                "reason": tls.get("reason"),
            },
            "listener": {
                "running": self._server is not None,
                "address": settings.get("listen_address"),
                "port": settings.get("listen_port"),
                "error": self._server_error or None,
            },
            "provider": self.provider_compatibility(),
            "clients": [self._public_client(value) for value in clients.values()],
            "pending_pairings": [
                self._public_pairing(value)
                for value in pairings.values()
                if isinstance(value, dict)
                and value.get("status") == "pending"
                and _pairing_expiry(value) > self.clock()
            ],
            "bridge": self._bridge_public(),
            "sunshine": self.monitor.public(),
        }

    def get_settings(self) -> dict[str, Any]:
        return self.get_local_status()

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(changes, dict):
            raise ApiError("settings must be an object")
        allowed = set(DEFAULT_SETTINGS)
        if set(changes) - allowed:
            raise ApiError("unsupported setting")
        validated: dict[str, Any] = {}
        if "listen_enabled" in changes:
            if not isinstance(changes["listen_enabled"], bool):
                raise ApiError("listen_enabled must be boolean")
            validated["listen_enabled"] = changes["listen_enabled"]
        if "listen_address" in changes:
            validated["listen_address"] = validate_host(changes["listen_address"])
        if "listen_port" in changes:
            port = changes["listen_port"]
            if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
                raise ApiError("listen_port must be between 1024 and 65535")
            validated["listen_port"] = port
        if "advertised_host" in changes:
            host = changes["advertised_host"]
            validated["advertised_host"] = "" if host in (None, "") else validate_host(host)
        if "monitor_sunshine" in changes:
            if not isinstance(changes["monitor_sunshine"], bool):
                raise ApiError("monitor_sunshine must be boolean")
            validated["monitor_sunshine"] = changes["monitor_sunshine"]
        self.store.mutate(lambda state: state.setdefault("settings", DEFAULT_SETTINGS).update(validated))
        settings = self.store.get("settings", DEFAULT_SETTINGS)
        if "monitor_sunshine" in validated:
            self.monitor.set_enabled(bool(settings.get("monitor_sunshine")))
            if not settings.get("monitor_sunshine"):
                self.journal.cancel_kind("sunshine.", "cancelled because Monitor Sunshine was disabled")
        if any(key in validated for key in ("listen_enabled", "listen_address", "listen_port")):
            self._restart_server()
        return self.get_local_status()

    def create_pairing(self, requested_scopes: Any = None) -> dict[str, Any]:
        scopes = validate_scopes(requested_scopes)
        self._prune_pairings()
        self._ensure_tls_material()
        settings = self.store.get("settings", DEFAULT_SETTINGS)
        host = self._resolve_pairing_host(settings)
        port = int(settings.get("listen_port", DEFAULT_SETTINGS["listen_port"]))
        fingerprint = self._tls.get("fingerprint") or self.store.get("tls", {}).get("fingerprint")
        if not fingerprint:
            raise ApiError("host certificate is not ready; start the listener first", 503, "tls_unavailable")
        pairing_id = opaque_id("pair-")
        secret = _token()
        expires_at = self.clock() + 120
        endpoint = _endpoint(host, port)
        payload = encode_payload({
            "protocol_version": 1,
            "endpoint": endpoint,
            "host_id": self.host_id,
            "certificate_fingerprint": fingerprint,
            "pairing_id": pairing_id,
            "secret": secret,
            "expires_at": expires_at,
        })
        record = {
            "pairing_id": pairing_id,
            "secret_hash": _hash_secret(secret),
            "pairing_method": "payload",
            "requested_scopes": scopes,
            "created_at": _utc_now(),
            "expires_at": expires_at,
            "status": "created",
            "client_name": "Omarchy client",
            "client_id": None,
        }
        with self._pairing_lock:
            self.store.mutate(lambda state: state.setdefault("pairings", {}).__setitem__(pairing_id, record))
        return {
            "pairing_id": pairing_id,
            "payload": payload,
            "expires_at": expires_at,
            "endpoint": endpoint,
            "host_id": self.host_id,
            "certificate_fingerprint": fingerprint,
            "requested_scopes": scopes,
            "wake_target": self._wake_target(),
            "qr_svg_base64": _pairing_qr_svg_base64(payload),
        }

    def create_pairing_code(self, requested_scopes: Any = None) -> dict[str, Any]:
        """Create a short bootstrap code; the client still needs local approval."""
        scopes = validate_scopes(requested_scopes)
        self._prune_pairings()
        self._ensure_tls_material()
        settings = self.store.get("settings", DEFAULT_SETTINGS)
        host = self._resolve_pairing_host(settings)
        port = int(settings.get("listen_port", DEFAULT_SETTINGS["listen_port"]))
        fingerprint = self._tls.get("fingerprint") or self.store.get("tls", {}).get("fingerprint")
        if not fingerprint:
            raise ApiError("host certificate is not ready; start the listener first", 503, "tls_unavailable")
        pairing_id = opaque_id("pair-")
        code = _pairing_code()
        expires_at = self.clock() + 120
        endpoint = _endpoint(host, port)

        def record_code(state: dict[str, Any]) -> None:
            # Keep one active code visible to the owner. Existing full-payload
            # pairings remain valid so the advanced fallback is not disrupted.
            for previous in state.setdefault("pairings", {}).values():
                if previous.get("pairing_method") == "code" and previous.get("status") in {"created", "pending"}:
                    previous["status"] = "superseded"
            state.setdefault("pairings", {})[pairing_id] = {
                "pairing_id": pairing_id,
                "pairing_code_hash": _hash_secret(code),
                "pairing_method": "code",
                "requested_scopes": scopes,
                "created_at": _utc_now(),
                "expires_at": expires_at,
                "status": "created",
                "client_name": "Omarchy client",
                "client_id": None,
            }

        with self._pairing_lock:
            self.store.mutate(record_code)
        return {
            "pairing_id": pairing_id,
            "pairing_code": _display_pairing_code(code),
            "expires_at": expires_at,
            "endpoint": endpoint,
            "host_id": self.host_id,
            "certificate_fingerprint": fingerprint,
            "requested_scopes": scopes,
            "wake_target": self._wake_target(),
        }

    def list_pairings(self) -> list[dict[str, Any]]:
        self._prune_pairings()
        return [self._public_pairing(value) for value in self.store.get("pairings", {}).values()]

    def approve_pairing(self, pairing_id: str, scopes: Any = None) -> dict[str, Any]:
        pairing_id = identifier(pairing_id, "pairing_id")
        with self._pairing_lock:
            pairings = self.store.get("pairings", {})
            pairing = pairings.get(pairing_id)
            if not pairing:
                raise ApiError("pairing is not pending", 404, "pairing_not_found")
            if pairing.get("status") == "created":
                raise ApiError("pairing request has not reached the host", 409, "pairing_not_requested")
            if pairing.get("status") != "pending":
                raise ApiError("pairing is not pending", 404, "pairing_not_found")
            if _pairing_expiry(pairing) <= self.clock():
                raise ApiError("pairing has expired", 410, "pairing_expired")
            allowed = set(pairing.get("requested_scopes", []))
            requested_by_client = pairing.get("requested_by_client")
            if not isinstance(requested_by_client, list):
                raise ApiError("pairing request has not reached the host", 409, "pairing_not_requested")
            requested = set(validate_scopes(requested_by_client))
            granted = validate_scopes(scopes if scopes is not None else list(requested))
            if not set(granted).issubset(allowed) or not set(granted).issubset(requested):
                raise ApiError("approval cannot grant an unrequested scope")
            client_id = pairing.get("client_id") or opaque_id("client-")
            clients = self.store.get("clients", {})
            if client_id not in clients and len(clients) >= MAX_CLIENTS:
                raise ApiError("maximum paired clients reached; revoke one before approving another", 429, "client_limit_reached")
            token = _token()
            client_label = client_name(pairing.get("client_name"))
            client_record = {
                "client_id": client_id,
                "name": client_label,
                "token_hash": _hash_secret(token),
                "scopes": granted,
                "created_at": _utc_now(),
                "last_seen": None,
            }

            def mutate(state: dict[str, Any]) -> None:
                state.setdefault("clients", {})[client_id] = client_record
                state.setdefault("pairings", {}).setdefault(pairing_id, {}).update({
                    "status": "approved",
                    "client_id": client_id,
                    "granted_scopes": granted,
                })

            self.store.mutate(mutate)
            self._approved_tokens[pairing_id] = token
            return self._public_pairing(self.store.get("pairings", {})[pairing_id])

    def reject_pairing(self, pairing_id: str) -> dict[str, Any]:
        pairing_id = identifier(pairing_id, "pairing_id")
        changed = self.store.mutate(lambda state: self._set_pairing_status(state, pairing_id, "rejected"))
        if not changed:
            raise ApiError("pairing is not pending", 404, "pairing_not_found")
        self._approved_tokens.pop(pairing_id, None)
        return {"pairing_id": pairing_id, "status": "rejected"}

    def revoke_client(self, client_id: str) -> dict[str, Any]:
        client_id = identifier(client_id, "client_id")
        removed = self.store.mutate(lambda state: state.setdefault("clients", {}).pop(client_id, None))
        if removed is None:
            raise ApiError("client is not paired", 404, "client_not_found")
        self.journal.cancel_for_client(client_id, "sunshine.", "cancelled because the credential was revoked")
        return {"client_id": client_id, "revoked": True}

    def set_sunshine_provider(self, provider: Any) -> dict[str, Any]:
        if provider is not None and not isinstance(provider, ProviderAdapter):
            provider = ProviderAdapter(provider)
        if provider is not None:
            self._sunshine_owner_reason = None
        self.monitor.set_provider(provider)
        return self.provider_compatibility()

    def report_sunshine_owner(self, report: Any) -> dict[str, Any]:
        """Register the guarded frontend connection to Decky Sunshine.

        The frontend first probes the owner plugin through Decky Loader. A
        successful probe installs the narrow bridge-backed provider; a failed
        probe clears it so the UI reports an unavailable owner instead of
        pretending that monitoring is active.
        """
        if not isinstance(report, dict) or not isinstance(report.get("available"), bool):
            raise ApiError("sunshine owner report is invalid")
        if report["available"]:
            self._sunshine_owner_reason = None
            return self.set_sunshine_provider(BridgeSunshineProvider(self.bridge))
        self._sunshine_owner_reason = str(report.get("reason") or "Decky Sunshine owner plugin was not reachable")[:256]
        self.monitor.set_provider(None)
        return self.provider_compatibility()

    def provider_compatibility(self) -> dict[str, Any]:
        provider = self.monitor.provider()
        if provider is None:
            return {
                "ready": False,
                "provider": None,
                "contract_version": None,
                "reason": self._sunshine_owner_reason or "No narrow Decky Sunshine owner adapter is connected",
            }
        return {
            "ready": True,
            "provider": str(getattr(provider, "provider_name", "provider"))[:128],
            "contract_version": str(getattr(provider, "contract_version", "unknown"))[:64],
            "reason": None,
        }

    # ---- internal bridge methods --------------------------------------

    def next_bridge_command(self) -> dict[str, Any] | None:
        return self.bridge.next_command()

    def report_bridge_result(self, command_id: str, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(command_id, str) or len(command_id) > 128:
            raise ApiError("command_id is invalid")
        if isinstance(result, dict) and isinstance(result.get("snapshot"), dict):
            self.report_bridge_snapshot(result["snapshot"])
        try:
            return self.bridge.report_result(command_id, result)
        except BridgeError as exc:
            raise ApiError(str(exc), 404, "bridge_command_not_found") from exc

    def report_bridge_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.bridge.report_snapshot(snapshot)
        except BridgeError as exc:
            raise ApiError(str(exc), 400, "invalid_bridge_snapshot") from exc

    def _bridge_public(self) -> dict[str, Any]:
        snapshot, age_ms = self.bridge.snapshot()
        return {
            "ready": snapshot is not None and snapshot.get("ready") is True,
            "age_ms": age_ms,
            "reason": None if snapshot is not None else "Decky frontend bridge has not reported readiness",
            "methods": snapshot.get("methods", {}) if snapshot else {},
        }

    # ---- authenticated route handling --------------------------------

    def handle_http(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        *,
        peer_address: str | None = None,
        channel_binding: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        headers = headers or {}
        method = method.upper()
        if "?" in path or "#" in path or not path.startswith("/v1/"):
            raise ApiError("route is not supported", 404, "not_found")
        body = validate_route_body(method, path, body)
        if method == "POST" and path == "/v1/pair/request":
            if peer_address is not None and not channel_binding:
                raise ApiError("TLS channel binding is unavailable; pairing must use a direct TLS connection", 503, "pairing_transport_unavailable")
            binding_header = next(
                (
                    value
                    for key, value in headers.items()
                    if isinstance(key, str) and key.lower() == "x-steamos-remote-tls-binding"
                ),
                None,
            )
            client_channel_binding = _valid_channel_binding(binding_header)
            if peer_address is not None and not body.get("pairing_session") and (
                not client_channel_binding
                or not hmac.compare_digest(str(channel_binding), client_channel_binding)
            ):
                raise ApiError("pairing channel verification failed; select the host again and retry", 401, "pairing_channel_mismatch")
            is_poll = bool(body.get("pairing_session"))
            self._enforce_rate_limit("pair:" + (peer_address or "direct"), 120 if is_poll else 12, 60.0)
            return 200, {}, self._handle_pair_request(
                body,
                channel_binding=channel_binding,
                client_channel_binding=client_channel_binding,
            )
        if method == "GET" and path == "/v1/status":
            client = self._authenticate(headers, "status.read", peer_address)
            return 200, {}, self.status_for(client["client_id"], client["scopes"])
        if method == "GET" and path == "/v1/display/outputs":
            client = self._authenticate(headers, "status.read", peer_address)
            return 200, {}, self.display_outputs(client["client_id"])
        if method == "GET" and path.startswith("/v1/operations/"):
            client = self._authenticate(headers, None, peer_address)
            operation_id = identifier(path.rsplit("/", 1)[-1], "operation_id")
            operation = self.journal.get(operation_id, client["client_id"])
            if operation is None:
                raise ApiError("operation is not available to this credential", 404, "operation_not_found")
            return 200, {}, {"protocol_version": 1, "operation": operation}
        if method == "POST" and path == "/v1/pair/revoke-self":
            client = self._authenticate(headers, None, peer_address)
            operation = self._revoke_self(client, body)
            return 202, {}, {"protocol_version": 1, "operation": operation}
        client = self._authenticate(headers, self._scope_for_route(path), peer_address)
        if path == "/v1/power":
            return self._power(client, body)
        if path == "/v1/display/preview":
            return self._display_preview(client, body)
        if path == "/v1/display/confirm":
            return self._display_confirm(client, body)
        if path == "/v1/display/restore":
            return self._display_restore(client, body)
        if path == "/v1/sunshine/restart":
            return self._sunshine_restart(client, body)
        raise ApiError("route is not supported", 404, "not_found")

    def _authenticate(self, headers: dict[str, str], scope: str | None, peer_address: str | None = None) -> dict[str, Any]:
        authorization = headers.get("Authorization") or headers.get("authorization") or ""
        if not authorization.startswith("Bearer ") or len(authorization) > 512:
            raise ApiError("authentication required", 401, "unauthorized")
        supplied = authorization[7:]
        clients = self.store.get("clients", {})
        for client in clients.values():
            if _constant_time_hash_match(supplied, str(client.get("token_hash", ""))):
                if scope is not None and scope not in set(client.get("scopes", [])):
                    raise ApiError("credential does not have the required scope", 403, "forbidden")
                self._enforce_rate_limit("client:" + str(client.get("client_id")), 120, 60.0)
                return client
        raise ApiError("authentication failed", 401, "unauthorized")

    @staticmethod
    def _scope_for_route(path: str) -> str:
        if path == "/v1/power":
            return "power.control"
        if path.startswith("/v1/display/"):
            return "display.control"
        if path == "/v1/sunshine/restart":
            # Sunshine monitoring is a Decky-owned setting. A normally paired
            # client with the mutation scope may request recovery when Decky
            # exposes a stopped, provider-confirmed process.
            return "power.control"
        return "status.read"

    def _pairing_certificate_fingerprint(self) -> str:
        """The host's own certificate fingerprint, used to bind the shown code."""
        fingerprint = self._tls.get("fingerprint") or self.store.get("tls", {}).get("fingerprint")
        if not fingerprint:
            raise ApiError("host certificate is not ready", 503, "tls_unavailable")
        return str(fingerprint)

    def _find_pairing_by_code(self, pairing_code: str) -> tuple[str, dict[str, Any]]:
        """Resolve a legacy host-issued code without exposing it."""
        match: tuple[str, dict[str, Any]] | None = None
        for candidate_id, candidate in self.store.get("pairings", {}).items():
            expected_hash = candidate.get("pairing_code_hash")
            if isinstance(expected_hash, str) and _constant_time_hash_match(pairing_code, expected_hash):
                match = (candidate_id, candidate)
        if match is None:
            raise ApiError("pairing code is invalid or expired", 401, "unauthorized")
        return match

    def _find_verification_pairing(self, client_id: str, nonce: str) -> tuple[str, dict[str, Any]] | None:
        """Find the client-created request, rejecting an ambiguous nonce reuse.

        Matching happens on the stored nonce hash, so the slow certificate-bound
        derivation never runs inside this loop.
        """
        nonce_hash = _hash_secret(nonce)
        match: tuple[str, dict[str, Any]] | None = None
        for candidate_id, candidate in self.store.get("pairings", {}).items():
            if candidate.get("pairing_method") != "verification" or candidate.get("status") in {
                "rejected", "expired", "consumed", "superseded",
            }:
                continue
            if _pairing_expiry(candidate) <= self.clock():
                continue
            candidate_hash = candidate.get("verification_nonce_hash")
            if not isinstance(candidate_hash, str) or not hmac.compare_digest(nonce_hash, candidate_hash):
                continue
            if candidate.get("client_id") != client_id:
                raise ApiError("pairing verification code is already in use", 409, "pairing_code_conflict")
            match = (candidate_id, candidate)
        return match

    def _handle_pair_request(
        self,
        body: dict[str, Any],
        *,
        channel_binding: str | None = None,
        client_channel_binding: str | None = None,
    ) -> dict[str, Any]:
        self._prune_pairings()
        pairing_code = normalize_pairing_code(body["pairing_code"]) if "pairing_code" in body else None
        nonce = verification_nonce(body["verification_nonce"]) if "verification_nonce" in body else None
        pairing_session_value = pairing_session(body["pairing_session"]) if "pairing_session" in body else None
        name = client_name(body.get("client_name"))
        requested = validate_scopes(body.get("scopes"))
        client_id = identifier(body.get("client_id"), "client_id")
        with self._pairing_lock:
            secret = None
            pairing_session_token: str | None = None
            newly_created = False
            if nonce is not None:
                found = self._find_verification_pairing(client_id, nonce)
                if found is None:
                    active_requests = sum(
                        1
                        for candidate in self.store.get("pairings", {}).values()
                        if candidate.get("pairing_method") == "verification"
                        and candidate.get("status") == "pending"
                        and _pairing_expiry(candidate) > self.clock()
                    )
                    if active_requests >= 32:
                        raise ApiError("too many pending pairing requests", 429, "pairing_queue_full")
                    pairing_id = opaque_id("pair-")
                    newly_created = True
                    expires_at = self.clock() + 120
                    if channel_binding and client_channel_binding:
                        pairing_session_token = _token()
                    # The comparison digits are derived from the client nonce
                    # and this host's own certificate fingerprint, so a relay
                    # presenting a different certificate cannot make both
                    # screens agree. scrypt runs once, here only.
                    derived_code = derive_pairing_code(nonce, self._pairing_certificate_fingerprint())
                    record = {
                        "pairing_id": pairing_id,
                        # This is a human-verification string, not a bearer
                        # credential. It is kept only until the request expires
                        # so the local Decky UI can show the comparison value.
                        "verification_code": _display_pairing_code(derived_code),
                        "verification_nonce_hash": _hash_secret(nonce),
                        "pairing_method": "verification",
                        "requested_scopes": requested,
                        "created_at": _utc_now(),
                        "expires_at": expires_at,
                        "status": "pending",
                        "client_name": name,
                        "client_id": client_id,
                        "requested_by_client": requested,
                    }
                    if pairing_session_token:
                        record.update({
                            "pairing_session_hash": _hash_secret(pairing_session_token),
                            "channel_binding_hash": _hash_channel_binding(channel_binding),
                        })
                    self.store.mutate(lambda state: state.setdefault("pairings", {}).__setitem__(pairing_id, record))
                else:
                    pairing_id, _ = found
            elif pairing_code is not None:
                pairing_id, _ = self._find_pairing_by_code(pairing_code)
            else:
                pairing_id = identifier(body.get("pairing_id"), "pairing_id")
                secret = str(body.get("secret"))
            pairing = self.store.get("pairings", {}).get(pairing_id)
            if not pairing or pairing.get("status") in {"rejected", "expired", "consumed", "superseded"}:
                raise ApiError("pairing is not available", 404, "pairing_not_found")
            if _pairing_expiry(pairing) <= self.clock():
                self.store.mutate(lambda state: self._set_pairing_status(state, pairing_id, "expired"))
                raise ApiError("pairing has expired", 410, "pairing_expired")
            if (
                not newly_created
                and pairing.get("status") == "created"
                and not pairing.get("pairing_session_hash")
                and channel_binding
                and client_channel_binding
            ):
                pairing_session_token = _token()
            expected_session_hash = pairing.get("pairing_session_hash")
            if expected_session_hash:
                if newly_created:
                    pass
                elif pairing_session_value is None:
                    raise ApiError("pairing session is required; start a new request", 409, "pairing_session_required")
                elif not _constant_time_hash_match(pairing_session_value, str(expected_session_hash)):
                    raise ApiError("pairing session is invalid", 401, "unauthorized")
            elif pairing_session_value is not None:
                raise ApiError("pairing session is invalid", 401, "unauthorized")
            if nonce is not None:
                valid_code = pairing.get("pairing_method") == "verification" and _constant_time_hash_match(
                    nonce, str(pairing.get("verification_nonce_hash", ""))
                )
                if not valid_code:
                    raise ApiError("pairing verification code is invalid", 401, "unauthorized")
            elif pairing_code is not None:
                valid_code = pairing.get("pairing_method") == "code" and _constant_time_hash_match(
                    pairing_code, str(pairing.get("pairing_code_hash", ""))
                )
                if not valid_code:
                    raise ApiError("pairing code is invalid", 401, "unauthorized")
            elif not _constant_time_hash_match(secret, str(pairing.get("secret_hash", ""))):
                raise ApiError("pairing secret is invalid", 401, "unauthorized")
            if not set(requested).issubset(set(pairing.get("requested_scopes", []))):
                raise ApiError("pairing requested an unapproved scope", 403, "forbidden")
            existing_client_id = pairing.get("client_id")
            if existing_client_id is not None and existing_client_id != client_id:
                raise ApiError("pairing is already claimed by another client", 409, "pairing_client_conflict")
            previous_requested = pairing.get("requested_by_client")
            if previous_requested is not None and set(requested) != set(previous_requested):
                raise ApiError("pairing scopes changed while approval was pending", 409, "pairing_scope_conflict")

            def record_request(state: dict[str, Any]) -> None:
                record = state.setdefault("pairings", {}).setdefault(pairing_id, {})
                record.update({"client_name": name, "requested_by_client": requested, "client_id": client_id})
                if pairing_session_token:
                    record["pairing_session_hash"] = _hash_secret(pairing_session_token)
                    record["channel_binding_hash"] = _hash_channel_binding(channel_binding)
                if record.get("status") == "created":
                    record["status"] = "pending"

            self.store.mutate(record_request)
            pairing = self.store.get("pairings", {})[pairing_id]
            if pairing.get("status") == "pending":
                response = {"protocol_version": 1, "state": "pending", "pairing_id": pairing_id, "expires_at": pairing.get("expires_at")}
                if pairing_session_token:
                    response["pairing_session"] = pairing_session_token
                return response
            if pairing.get("status") != "approved":
                raise ApiError("pairing is not approved", 409, "pairing_not_approved")
            token = self._approved_tokens.pop(pairing_id, None)
            if token is None:
                raise ApiError("approved credential has already been delivered; create a new pairing", 410, "pairing_consumed")
            self.store.mutate(lambda state: self._set_pairing_status(state, pairing_id, "consumed"))
            client_id = pairing.get("client_id")
            client = self.store.get("clients", {}).get(client_id, {})
            return {
                "protocol_version": 1,
                "state": "approved",
                "pairing_id": pairing_id,
                "host_id": self.host_id,
                "credential": {
                    "client_id": client_id,
                    "token": token,
                    "scopes": client.get("scopes", []),
                    "host_id": self.host_id,
                },
                "wake_target": self._wake_target(),
            }

    # ---- status --------------------------------------------------------

    def status_for(self, client_id: str, scopes: list[str] | None = None) -> dict[str, Any]:
        snapshot, age_ms = self.bridge.snapshot()
        ready = snapshot is not None and snapshot.get("ready") is True
        bridge_seen = snapshot is not None
        methods = snapshot.get("methods", {}) if snapshot else {}
        reason = None if ready else (snapshot.get("reason") if snapshot else "Decky frontend bridge has not reported readiness")
        profiles = self.store.get("profiles", {})
        sunshine = self.monitor.public()
        sunshine_capability = "disabled"
        if sunshine["enabled"]:
            if sunshine["state"] == "stopped" and self.monitor.provider_ready():
                sunshine_capability = "available"
            elif sunshine["state"] in {"unavailable", "unknown"}:
                sunshine_capability = "unavailable"
            else:
                sunshine_capability = "unavailable"
        return {
            "protocol_version": 1,
            "host_id": self.host_id,
            "boot_id": self.boot_id,
            "steam_bridge": "ready" if ready else "unavailable",
            "steam_reason": reason,
            "steam_age_ms": age_ms,
            "wake_target": self._wake_target(),
            "uptime_seconds": system_uptime_seconds(),
            "cpu_temperature": (snapshot.get("cpu_temperature") if snapshot else None) or read_cpu_temperature(),
            "capabilities": {
                "suspend": "available" if bridge_seen and methods.get("suspend") else "unavailable",
                "restart": "available" if bridge_seen and methods.get("restart") else "unavailable",
                "shutdown": "available" if bridge_seen and methods.get("shutdown") else "unavailable",
                "display_rescue": "available" if ready and methods.get("display") and profiles else ("unavailable" if not ready else "unverified"),
                "sunshine_restart": sunshine_capability,
            },
            "sunshine": sunshine,
        }

    def display_outputs(self, client_id: str) -> dict[str, Any]:
        snapshot, age_ms = self.bridge.snapshot()
        if snapshot is None:
            return {"protocol_version": 1, "available": False, "age_ms": age_ms, "reason": "Steam display bridge is unavailable", "outputs": []}
        profiles = self.store.get("profiles", {})
        public_profiles = [self._public_profile(value) for value in profiles.values()]
        return {
            "protocol_version": 1,
            "available": bool(snapshot.get("methods", {}).get("display")),
            "age_ms": age_ms,
            "reason": None if snapshot.get("methods", {}).get("display") else "Steam display bridge is unavailable",
            "generation": max((output.get("generation", 0) for output in snapshot.get("outputs", [])), default=0),
            "outputs": snapshot.get("outputs", []),
            "profiles": public_profiles,
            "preview": self._public_preview(self.store.get("preview")),
        }

    # ---- operations ----------------------------------------------------

    def _power(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        action = body["action"]
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return 202, {}, {"protocol_version": 1, "operation": existing}
        with self._mutation_lock:
            snapshot, _ = self.bridge.snapshot()
            if snapshot is None or not snapshot.get("methods", {}).get(action):
                raise ApiError(f"Steam {action} bridge is unavailable", 503, "bridge_unavailable")
            self._reject_if_conflicting("power")
            created, operation = self.journal.begin(client["client_id"], request_id(body), f"power.{action}", body)
            if created:
                self._spawn(operation["id"], lambda: self._run_power(operation["id"], action))
            return 202, {}, {"protocol_version": 1, "operation": operation}

    def _run_power(self, operation_id: str, action: str) -> None:
        with self._mutation_lock:
            self.journal.update(operation_id, state="dispatched", target={"action": action})
            try:
                result = self.bridge.request("power", {"action": action}, operation_id, timeout=8)
                if not _result_ok(result):
                    self.journal.update(operation_id, state="failed", reason=str(result.get("reason", "Steam rejected the power request")))
                    return
                self.journal.update(operation_id, state="observed_return", outcome="method_returned", reason=f"Steam returned; physical {action} transition is not confirmed by this response")
            except Exception as exc:
                self.journal.update(operation_id, state="unknown", reason=f"power result is unknown: {str(exc)[:180]}")

    def _display_preview(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return 202, {}, {
                "protocol_version": 1,
                "operation": existing,
                "preview": self._public_preview(self.store.get("preview")),
            }
        with self._mutation_lock:
            snapshot, _ = self.bridge.snapshot()
            if snapshot is None or not snapshot.get("methods", {}).get("display"):
                raise ApiError("Steam display bridge is unavailable", 503, "bridge_unavailable")
            self._reject_if_conflicting("display_preview")
            output = self._find_output(snapshot, body["output_id"])
            if output is None:
                raise ApiError("output is no longer advertised", 409, "stale_target")
            if output["generation"] != body["generation"]:
                raise ApiError("display target generation is stale", 409, "stale_target")
            mode = self._find_mode(output, body["mode_id"])
            baseline = self._find_mode(output, output.get("current_mode_id"))
            if mode is None or baseline is None:
                raise ApiError("selected display mode is unavailable", 409, "stale_target")
            if mode["id"] == baseline["id"]:
                raise ApiError("selected mode is already current", 409, "no_change")
            operation_body = dict(body)
            created, operation = self.journal.begin(client["client_id"], request_id(body), "display.preview", operation_body)
            preview_id = operation["id"]
            if created:
                deadline = self.clock() + 15
                preview = {
                    "preview_id": preview_id,
                    "operation_id": preview_id,
                    "owner_client_id": client["client_id"],
                    "output_identity": display_identity(output),
                    "output_id": output["id"],
                    "generation": output["generation"],
                    "baseline_mode": baseline,
                    "target_mode": mode,
                    "deadline": deadline,
                    "restore_started": False,
                    "restore_state": None,
                    "created_at": _utc_now(),
                }
                self.store.mutate(lambda state: state.__setitem__("preview", preview))
                self.journal.update(preview_id, preview_id=preview_id, target={"output_id": output["id"], "mode_id": mode["id"], "generation": output["generation"]})
                operation = self.journal.get(preview_id, client["client_id"]) or operation
                self._spawn(preview_id, lambda: self._run_preview(preview_id))
            operation["preview_id"] = preview_id
            return 202, {}, {"protocol_version": 1, "operation": operation, "preview": self._public_preview(self.store.get("preview"))}

    def _run_preview(self, operation_id: str) -> None:
        with self._mutation_lock:
            preview = self.store.get("preview")
            if not preview or preview.get("preview_id") != operation_id:
                self.journal.update(operation_id, state="failed", reason="preview intent disappeared before dispatch")
                return
            try:
                snapshot, _ = self.bridge.snapshot()
                output = self._find_output(snapshot, preview["output_id"]) if snapshot else None
                if not snapshot or not same_display_identity(preview["output_identity"], output) or output.get("generation") != preview["generation"]:
                    raise DisplayError("display identity or generation changed; preview not sent")
                mode = self._find_mode(output, preview["target_mode"]["id"])
                if mode is None:
                    raise DisplayError("selected mode disappeared; preview not sent")
                self.journal.update(operation_id, state="dispatched")
                result = self.bridge.request("set_mode", {
                    "output_id": output["id"], "mode_id": mode["id"], "generation": output["generation"], "rgb_range": output.get("rgb_range", 0),
                }, operation_id, timeout=8)
                if not _result_ok(result):
                    raise BridgeError(str(result.get("reason", "Steam rejected display mode")))
                readback = result.get("snapshot")
                if isinstance(readback, dict):
                    def remember_readback(state: dict[str, Any]) -> None:
                        active = state.get("preview")
                        if isinstance(active, dict) and active.get("preview_id") == operation_id:
                            active["readback"] = readback
                            active["readback_at"] = _utc_now()
                    self.store.mutate(remember_readback)
                self.journal.update(operation_id, state="observed_return", outcome="mode_request_returned", reason="Preview is waiting for visible-picture confirmation or host timeout")
            except Exception as exc:
                self.journal.update(operation_id, state="failed", reason=str(exc)[:256])
                self._clear_preview_if(operation_id)

    def _display_confirm(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return 202, {}, {
                "protocol_version": 1,
                "operation": existing,
                "profile": self._public_profile(self._latest_profile()),
            }
        with self._mutation_lock:
            preview = self.store.get("preview")
            if not preview or preview.get("preview_id") != body["preview_id"]:
                raise ApiError("preview is unavailable or expired", 409, "preview_not_found")
            if preview.get("owner_client_id") != client["client_id"]:
                raise ApiError("only the preview owner can confirm it", 403, "forbidden")
            if float(preview.get("deadline", 0)) <= self.clock():
                raise ApiError("preview deadline has expired", 409, "preview_expired")
            # Applying a mode may make Steam re-enumerate the same output and
            # assign new mode IDs. The target was generation-checked before
            # dispatch; confirmation relies on the stable output identity and
            # semantic mode readback instead of rejecting that expected churn.
            # Some Steam builds publish the new mode a short time after the
            # bridge command returns, so keep Save boundedly retryable rather
            # than making the user race that publication window.
            matched = False
            output = None
            current = None
            retry_deadline = min(
                self.monotonic() + 3.0,
                self.monotonic() + max(0.0, float(preview.get("deadline", 0)) - self.clock()),
            )
            while True:
                snapshot, _ = self.bridge.snapshot()
                output = self._find_output(snapshot, preview["output_id"]) if snapshot else None
                current = self._find_mode(output, output.get("current_mode_id")) if output else None
                if snapshot and same_display_identity(preview["output_identity"], output) and same_mode_profile(preview["target_mode"], current):
                    matched = True
                    break
                # Use the command-scoped readback as a fallback when it already
                # confirms the target, while still preferring a fresh snapshot.
                readback = preview.get("readback")
                readback_output = self._find_output(readback, preview["output_id"]) if isinstance(readback, dict) else None
                readback_mode = self._find_mode(readback_output, readback_output.get("current_mode_id")) if readback_output else None
                if readback and same_display_identity(preview["output_identity"], readback_output) and same_mode_profile(preview["target_mode"], readback_mode):
                    output = readback_output
                    current = readback_mode
                    matched = True
                    break
                if self.monotonic() >= retry_deadline:
                    break
                time.sleep(0.05)
            if not matched:
                raise ApiError("current Steam readback does not match the preview target", 409, "readback_not_confirmed")
            created, operation = self.journal.begin(client["client_id"], request_id(body), "display.confirm", body)
            if created:
                profile_id = opaque_id("profile-")
                profile = {
                    "id": profile_id,
                    "output_identity": preview["output_identity"],
                    "output_id": preview["output_id"],
                    "mode": current,
                    "verified_at": _utc_now(),
                    "verified_by": client["client_id"],
                }
                self.store.mutate(lambda state: (state.setdefault("profiles", {}).__setitem__(profile_id, profile), state.__setitem__("preview", None)))
                self.journal.update(operation["id"], state="succeeded", outcome="visible_picture_confirmed", target={"profile_id": profile_id})
                operation = self.journal.get(operation["id"], client["client_id"]) or operation
            return 202, {}, {"protocol_version": 1, "operation": operation, "profile": self._public_profile(self._latest_profile())}

    def _display_restore(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return 202, {}, {"protocol_version": 1, "operation": existing}
        with self._mutation_lock:
            self._reject_if_conflicting("display_restore")
            preview = self.store.get("preview")
            source = body.get("source", "verified")
            if source == "preview":
                if not preview or preview.get("owner_client_id") != client["client_id"]:
                    raise ApiError("there is no restorable preview owned by this client", 409, "preview_not_found")
                if preview.get("restore_started") or float(preview.get("deadline", 0)) <= self.clock():
                    raise ApiError("preview deadline has expired; host restore is already responsible", 409, "preview_expired")
                target = {"kind": "preview", "preview_id": preview["preview_id"]}
            else:
                profile = self._profile_for_restore(body.get("profile_id"))
                target = {"kind": "verified", "profile_id": profile["id"]}
            created, operation = self.journal.begin(client["client_id"], request_id(body), "display.restore", body)
            if created:
                self._spawn(operation["id"], lambda: self._run_restore_operation(operation["id"], target))
            return 202, {}, {"protocol_version": 1, "operation": operation}

    def _run_restore_operation(self, operation_id: str, target: dict[str, Any]) -> None:
        with self._mutation_lock:
            preview = self.store.get("preview") if target["kind"] == "preview" else None
            if target["kind"] == "preview":
                baseline = preview.get("baseline_mode") if preview else None
                identity = preview.get("output_identity") if preview else None
                output_id = preview.get("output_id") if preview else None
            else:
                profile = self.store.get("profiles", {}).get(target["profile_id"])
                baseline = profile.get("mode") if profile else None
                identity = profile.get("output_identity") if profile else None
                output_id = profile.get("output_id") if profile else None
            try:
                if not baseline or not identity:
                    raise DisplayError("restore profile is unavailable")
                self.journal.update(operation_id, state="dispatched", target={"output_id": output_id, "mode": mode_profile(baseline)})
                result = self._send_restore(output_id, identity, baseline, operation_id)
                self.journal.update(operation_id, state="succeeded", outcome="restored_and_read_back", resolved_by=result["matched_by"], restored=True)
                if target["kind"] == "preview":
                    self._clear_preview_if(preview["preview_id"])
            except DisplayError as exc:
                self.journal.update(operation_id, state="failed", reason=str(exc))
            except Exception as exc:
                self.journal.update(operation_id, state="unknown", reason=f"restore result is unknown: {str(exc)[:180]}")

    def _send_restore(self, output_id: str, identity: dict[str, Any], baseline: dict[str, Any], operation_id: str) -> dict[str, Any]:
        snapshot, _ = self.bridge.snapshot()
        output = self._find_output(snapshot, output_id) if snapshot else None
        if not snapshot or not same_display_identity(identity, output):
            raise DisplayError("display identity changed or disappeared; restore not sent")
        mode, matched_by = resolve_restore_mode(output, baseline)
        result = self.bridge.request("set_mode", {
            "output_id": output["id"], "mode_id": mode["id"], "generation": output["generation"], "rgb_range": output.get("rgb_range", 0),
        }, operation_id, timeout=8)
        if not _result_ok(result):
            raise BridgeError(str(result.get("reason", "Steam rejected display restore")))
        deadline = self.monotonic() + 5
        while self.monotonic() < deadline:
            latest, _ = self.bridge.snapshot()
            latest_output = self._find_output(latest, output_id) if latest else None
            latest_mode = self._find_mode(latest_output, latest_output.get("current_mode_id")) if latest_output else None
            if latest_output and same_display_identity(identity, latest_output) and same_mode_profile(baseline, latest_mode):
                return {"matched_by": matched_by}
            time.sleep(0.05)
        raise BridgeError("restore was sent but readback did not confirm the working mode")

    def _sunshine_restart(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return 202, {}, {"protocol_version": 1, "operation": existing}
        with self._mutation_lock:
            self._reject_if_conflicting("sunshine_restart")
            state = self.monitor.refresh_now()
            if not self.monitor.provider_ready():
                raise ApiError("Sunshine monitoring/provider is unavailable", 409, "provider_unavailable")
            if state["state"] != "stopped":
                raise ApiError("Sunshine is not freshly confirmed stopped", 409, "state_not_stopped")
            created, operation = self.journal.begin(client["client_id"], request_id(body), "sunshine.restart", body)
            if created:
                self.monitor.set_operation(operation["id"])
                self._spawn(operation["id"], lambda: self._run_sunshine_restart(operation["id"]))
            return 202, {}, {"protocol_version": 1, "operation": operation}

    def _run_sunshine_restart(self, operation_id: str) -> None:
        with self._mutation_lock:
            try:
                current = self.monitor.refresh_now(include_operation=False)
                if not self.monitor.is_enabled() or not self.monitor.provider():
                    self.journal.update(operation_id, state="failed", reason="Monitor Sunshine was disabled before dispatch")
                    return
                if current["state"] == "running":
                    self.journal.update(operation_id, state="succeeded", outcome="already_running")
                    return
                if current["state"] != "stopped":
                    self.journal.update(operation_id, state="failed", reason="live provider state was not stopped before dispatch")
                    return
                queued = self.journal.get(operation_id)
                if not queued or queued.get("state") != "accepted":
                    return
                provider = self.monitor.provider()
                if not self.monitor.is_enabled() or provider is None:
                    self.journal.update(operation_id, state="unknown", reason="provider became unavailable before dispatch")
                    return
                self.journal.update(operation_id, state="dispatched")
                result = self.monitor._call(provider.ensure_running)
                if isinstance(result, dict) and result.get("outcome") == "already_running":
                    self.journal.update(operation_id, state="succeeded", outcome="already_running")
                    return
                deadline = self.monotonic() + 30
                while self.monotonic() < deadline:
                    if not self.monitor.is_enabled():
                        self.journal.update(operation_id, state="unknown", reason="monitor disabled after recovery dispatch; no further process command issued")
                        return
                    observed = self.monitor.refresh_now(include_operation=False)
                    if observed["state"] == "running":
                        self.journal.update(operation_id, state="succeeded", outcome="running")
                        return
                    if observed["state"] == "unavailable":
                        break
                    time.sleep(0.5)
                self.journal.update(operation_id, state="unknown", reason="provider did not confirm running before the bounded recovery deadline")
            except Exception as exc:
                self.journal.update(operation_id, state="failed", reason=str(exc)[:256])
            finally:
                self.monitor.set_operation(None)

    def _revoke_self(self, client: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return existing
        with self._mutation_lock:
            if client["client_id"] not in self.store.get("clients", {}):
                raise ApiError("credential has already been revoked", 401, "unauthorized")
            created, operation = self.journal.begin(client["client_id"], request_id(body), "pair.revoke-self", body)
            if created:
                self.store.mutate(lambda state: state.setdefault("clients", {}).pop(client["client_id"], None))
                self.journal.update(operation["id"], state="succeeded", outcome="revoked")
                self.journal.cancel_for_client(client["client_id"], "sunshine.", "cancelled because the credential was revoked")
                operation = self.journal.get(operation["id"], client["client_id"]) or operation
            return operation

    def _reject_if_conflicting(self, kind: str, ignore_id: str | None = None) -> None:
        if self.store.get("preview") is not None and kind == "display_preview":
            raise ApiError("a display preview is already active", 409, "mutation_conflict")
        if self.store.get("preview") is not None and kind not in {"display_restore"}:
            raise ApiError("a display preview owns the mutation lane", 409, "mutation_conflict")
        prefixes = {
            "power": ("power.", "display.", "sunshine."),
            "display_preview": ("power.", "display.", "sunshine."),
            "display_restore": ("power.", "display."),
            "sunshine_restart": ("power.", "display.", "sunshine."),
        }.get(kind, ())
        for operation in self.journal.active():
            if ignore_id is not None and operation.get("id") == ignore_id:
                continue
            if operation.get("kind", "").startswith(prefixes):
                raise ApiError("another mutation is still in progress", 409, "mutation_conflict")

    def _spawn(self, operation_id: str, function: Callable[[], None]) -> None:
        def run() -> None:
            try:
                function()
            finally:
                self._workers.discard(thread)

        thread = threading.Thread(target=run, name=f"steamos-remote-{operation_id}", daemon=True)
        self._workers.add(thread)
        thread.start()

    def _enforce_rate_limit(self, key: str, limit: int, window: float) -> None:
        now = self.monotonic()
        with self._rate_lock:
            started, count = self._rate_buckets.get(key, (now, 0))
            if now - started >= window:
                started, count = now, 0
            if count >= limit:
                raise ApiError("request rate limit exceeded", 429, "rate_limited")
            self._rate_buckets[key] = (started, count + 1)
            if len(self._rate_buckets) > 512:
                cutoff = now - window
                self._rate_buckets = {
                    name: bucket for name, bucket in self._rate_buckets.items() if bucket[0] >= cutoff
                }

    # ---- preview watchdog and profile helpers ------------------------

    def _start_preview_watchdog(self) -> None:
        if self._preview_thread and self._preview_thread.is_alive():
            return
        self._preview_thread = threading.Thread(target=self._preview_watchdog, name="steamos-remote-preview", daemon=True)
        self._preview_thread.start()

    def _preview_watchdog(self) -> None:
        while not self._preview_stop.wait(0.25):
            preview = self.store.get("preview")
            if not preview or preview.get("restore_started"):
                continue
            if float(preview.get("deadline", 0)) <= self.clock():
                self._restore_expired_preview(preview)

    def _restore_expired_preview(self, preview: dict[str, Any]) -> None:
        with self._mutation_lock:
            current = self.store.get("preview")
            if not current or current.get("preview_id") != preview.get("preview_id") or current.get("restore_started"):
                return
            self.store.mutate(lambda state: state.setdefault("preview", {}).update({"restore_started": True, "restore_state": "dispatched"}))
            try:
                result = self._send_restore(preview["output_id"], preview["output_identity"], preview["baseline_mode"], preview["operation_id"])
                self.journal.update(preview["operation_id"], restore_state="succeeded", restored=True, resolved_by=result["matched_by"], reason="host preview deadline expired; original mode restored")
                self._clear_preview_if(preview["preview_id"])
            except Exception as exc:
                self.store.mutate(lambda state: state.setdefault("preview", {}).update({"restore_state": "unknown"}))
                self.journal.update(preview["operation_id"], restore_state="unknown", reason=f"host preview deadline expired; restore result is unknown: {str(exc)[:160]}")

    def _clear_preview_if(self, preview_id: str) -> None:
        self.store.mutate(lambda state: state.__setitem__("preview", None) if state.get("preview", {}).get("preview_id") == preview_id else None)

    def _profile_for_restore(self, profile_id: Any) -> dict[str, Any]:
        profiles = self.store.get("profiles", {})
        if profile_id is not None:
            profile_id = identifier(profile_id, "profile_id")
            profile = profiles.get(profile_id)
        else:
            profile = max(profiles.values(), key=lambda item: item.get("verified_at", ""), default=None)
        if not profile:
            raise ApiError("no owner-verified recovery profile is available", 409, "profile_not_found")
        return profile

    def _latest_profile(self) -> dict[str, Any] | None:
        return self._profile_for_restore(None) if self.store.get("profiles", {}) else None

    @staticmethod
    def _wake_target() -> dict[str, Any]:
        return _active_wake_target()

    # ---- small data helpers -------------------------------------------

    def _find_output(self, snapshot: dict[str, Any] | None, output_id: Any) -> dict[str, Any] | None:
        if not snapshot:
            return None
        return next((output for output in snapshot.get("outputs", []) if output.get("id") == str(output_id)), None)

    @staticmethod
    def _find_mode(output: dict[str, Any] | None, mode_id: Any) -> dict[str, Any] | None:
        if not output:
            return None
        return next((mode for mode in output.get("modes", []) if mode.get("id") == str(mode_id)), None)

    @staticmethod
    def _set_pairing_status(state: dict[str, Any], pairing_id: str, status: str) -> bool:
        pairing = state.setdefault("pairings", {}).get(pairing_id)
        if not pairing:
            return False
        pairing["status"] = status
        return True

    @staticmethod
    def _public_client(value: dict[str, Any]) -> dict[str, Any]:
        return {key: value.get(key) for key in ("client_id", "name", "scopes", "created_at", "last_seen")}

    @staticmethod
    def _public_pairing(value: dict[str, Any]) -> dict[str, Any]:
        requested = value.get("requested_by_client")
        return {
            "pairing_id": value.get("pairing_id"),
            "client_name": value.get("client_name"),
            "client_id": value.get("client_id"),
            "requested_scopes": requested if isinstance(requested, list) else value.get("requested_scopes", []),
            "allowed_scopes": value.get("requested_scopes", []),
            "granted_scopes": value.get("granted_scopes"),
            "created_at": value.get("created_at"),
            "expires_at": value.get("expires_at"),
            "status": value.get("status"),
            "pairing_method": value.get("pairing_method", "payload"),
            "verification_code": (
                value.get("verification_code")
                if value.get("pairing_method") == "verification"
                and value.get("status") in {"created", "pending"}
                else None
            ),
        }

    @staticmethod
    def _public_profile(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if not value:
            return None
        return {key: value.get(key) for key in ("id", "output_identity", "output_id", "mode", "verified_at")}

    @staticmethod
    def _public_preview(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if not value:
            return None
        return {key: value.get(key) for key in ("preview_id", "output_identity", "output_id", "generation", "baseline_mode", "target_mode", "deadline", "restore_state", "created_at")}

    def _default_advertised_host(self) -> str:
        route_host = _route_selected_ipv4()
        return route_host or "127.0.0.1"

    def _resolve_pairing_host(self, settings: dict[str, Any]) -> str:
        configured = settings.get("advertised_host")
        return validate_host(configured) if configured else self._default_advertised_host()
