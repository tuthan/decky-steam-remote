"""Dependency-free client core for the SteamOS Remote v1 protocol.

The Decky plugin is both a host and, optionally, a client.  This module is
deliberately independent from Decky's frontend and from the host service so
that the network boundary can be tested without a running Steam client.  It
owns TLS pin verification, the certificate-bound pairing exchange, bounded
LAN discovery, and the small set of enumerated v1 routes.

No bearer credential, pairing nonce, or request body is ever included in an
exception message or log produced here.
"""

from __future__ import annotations

import base64
import binascii
import concurrent.futures
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .pairing import derive_pairing_code
from .protocol import MAX_JSON_BYTES, ProtocolError, canonical_digest, identifier, validate_host, validate_mac, validate_scopes


MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_PORT = 18443
DISCOVERY_TIMEOUT = 1.25
DISCOVERY_TOTAL_BUDGET = 15.0
DISCOVERY_MAX_ADDRESSES = 256
REQUEST_TIMEOUT = 8.0
PAIRING_TIMEOUT = 8.0
PAIRING_EXPIRY_SECONDS = 120.0
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ClientError(RuntimeError):
    """A bounded, user-safe client failure."""

    def __init__(
        self,
        message: str,
        code: str = "client_error",
        status: int | None = None,
        *,
        retry_after: float | None = None,
        unknown: bool = False,
    ):
        super().__init__(str(message)[:256])
        self.code = str(code)[:64]
        self.status = status
        self.retry_after = retry_after
        self.unknown = bool(unknown)


class IdentityMismatch(ClientError):
    def __init__(self, message: str = "the device identity changed"):
        super().__init__(message, "identity_mismatch", 495)


class ClientResponseError(ClientError):
    """A response rejected by the remote v1 API."""

    def __init__(self, status: int, code: str, message: str, retry_after: float | None = None):
        super().__init__(message, code, status, retry_after=retry_after, unknown=False)


@dataclass(frozen=True)
class DiscoveryCandidate:
    endpoint: str
    host_id: str
    certificate_fingerprint: str
    service: str = "steamos-remote"
    name: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "host_id": self.host_id,
            "certificate_fingerprint": self.certificate_fingerprint,
            "service": self.service,
            "name": self.name,
        }


def certificate_fingerprint(der_certificate: bytes) -> str:
    if not isinstance(der_certificate, bytes) or not der_certificate:
        raise ClientError("the remote certificate was unavailable", "identity_mismatch")
    return "sha256:" + hashlib.sha256(der_certificate).hexdigest()


def _validate_fingerprint(value: Any) -> str:
    if not isinstance(value, str) or not _FINGERPRINT_RE.fullmatch(value):
        raise ClientError("the saved certificate identity is invalid", "identity_mismatch")
    return value


def validate_certificate_fingerprint(value: Any) -> str:
    """Validate the serialized certificate pin at a public module boundary."""
    return _validate_fingerprint(value)


def _parse_endpoint(endpoint: Any) -> tuple[str, int, str]:
    if not isinstance(endpoint, str) or len(endpoint) > 512:
        raise ClientError("the device address is invalid", "invalid_endpoint")
    value = endpoint.strip()
    if not value.startswith("https://"):
        raise ClientError("the device address must use HTTPS", "invalid_endpoint")
    try:
        parsed = urlsplit(value)
        parsed_hostname = parsed.hostname
    except ValueError as exc:
        raise ClientError("the device address is invalid", "invalid_endpoint") from exc
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ClientError("the device address is invalid", "invalid_endpoint")
    if parsed.path not in ("", "/") or not parsed_hostname:
        raise ClientError("the device address is invalid", "invalid_endpoint")
    try:
        host = validate_host(parsed_hostname)
        port = parsed.port or DEFAULT_PORT
    except (ValueError, TypeError) as exc:
        raise ClientError("the device address is invalid", "invalid_endpoint") from exc
    if not 1 <= port <= 65535:
        raise ClientError("the device port is invalid", "invalid_endpoint")
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return host, port, f"https://{rendered_host}:{port}"


def _retry_after(headers: dict[str, str]) -> float | None:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and 0 <= parsed <= 3600 else None


def _json_bytes(value: dict[str, Any] | None) -> bytes:
    if value is None:
        return b""
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ClientError("request body is not valid JSON", "invalid_request") from exc
    if len(raw) > MAX_JSON_BYTES:
        raise ClientError("request body is too large", "body_too_large")
    return raw


def _safe_response(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ClientError("the device response was too large", "response_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientError("the device returned invalid JSON", "invalid_response") from exc
    if not isinstance(value, dict):
        raise ClientError("the device returned an invalid response", "invalid_response")
    return value


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def new_verification_nonce() -> str:
    return _b64url(os.urandom(16))


def _channel_binding(connection: Any) -> str | None:
    getter = getattr(getattr(connection, "sock", connection), "get_channel_binding", None)
    version_getter = getattr(getattr(connection, "sock", connection), "version", None)
    if not callable(getter) or not callable(version_getter):
        return None
    try:
        version = version_getter()
        value = getter("tls-unique")
    except (ValueError, OSError, ssl.SSLError):
        return None
    if version != "TLSv1.2" or not isinstance(value, bytes) or len(value) < 12:
        return None
    return base64.urlsafe_b64encode(value).decode("ascii")


class PinnedTransport:
    """One bounded HTTPS request path with certificate pinning."""

    def __init__(
        self,
        endpoint: str,
        pinned_fingerprint: str | None = None,
        *,
        timeout: float = REQUEST_TIMEOUT,
        connection_factory: Callable[..., Any] | None = None,
    ):
        host, port, normalized = _parse_endpoint(endpoint)
        self.host = host
        self.port = port
        self.endpoint = normalized
        self.pinned_fingerprint = _validate_fingerprint(pinned_fingerprint) if pinned_fingerprint else None
        self.timeout = max(0.1, min(float(timeout), 30.0))
        self.connection_factory = connection_factory
        self.last_fingerprint: str | None = None
        self.last_channel_binding: str | None = None

    @staticmethod
    def _context(pairing: bool) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # tls-unique is defined for TLS 1.2.  The first pairing request is the
        # only request that needs the binding; subsequent polls authenticate by
        # the short-lived pairing session handle.
        if pairing:
            context.maximum_version = ssl.TLSVersion.TLSv1_2
        return context

    def _new_connection(self, pairing: bool) -> Any:
        context = self._context(pairing)
        if self.connection_factory is None:
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=context)
        try:
            return self.connection_factory(self.host, self.port, context, self.timeout)
        except TypeError:
            return self.connection_factory(self.endpoint, context, self.timeout)

    def _connect(self, pairing: bool) -> Any:
        try:
            connection = self._new_connection(pairing)
        except (OSError, ssl.SSLError, TimeoutError, ValueError) as exc:
            raise ClientError("could not establish a secure connection", "network_error", unknown=pairing) from exc
        try:
            connect = getattr(connection, "connect", None)
            if callable(connect):
                connect()
            sock = getattr(connection, "sock", None)
            peer_fingerprint = getattr(connection, "peer_fingerprint", None)
            if peer_fingerprint is None and sock is not None:
                peer_certificate = sock.getpeercert(binary_form=True)
                peer_fingerprint = certificate_fingerprint(peer_certificate)
            if not isinstance(peer_fingerprint, str):
                raise IdentityMismatch("the device certificate could not be inspected")
            peer_fingerprint = _validate_fingerprint(peer_fingerprint)
            if self.pinned_fingerprint and peer_fingerprint != self.pinned_fingerprint:
                raise IdentityMismatch()
            self.last_fingerprint = peer_fingerprint
            binding = _channel_binding(connection)
            self.last_channel_binding = binding
            if pairing and not binding:
                raise ClientError("pairing requires a direct TLS 1.2 connection", "pairing_transport_unavailable", 503)
            return connection
        except ClientError:
            try:
                connection.close()
            except Exception:
                pass
            raise
        except (OSError, ssl.SSLError, ValueError) as exc:
            try:
                connection.close()
            except Exception:
                pass
            raise ClientError("could not establish a secure connection", "network_error", unknown=pairing) from exc

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        pairing: bool = False,
    ) -> dict[str, Any]:
        method = str(method).upper()
        if method not in {"GET", "POST"} or not path.startswith("/v1/") or "?" in path or "#" in path:
            raise ClientError("the requested route is not supported", "invalid_request")
        raw = _json_bytes(body if method == "POST" else None)
        headers = {"Accept": "application/json", "Cache-Control": "no-store"}
        if raw:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(raw))
        if token:
            if len(token) > 512:
                raise ClientError("saved credential is invalid", "unauthorized")
            headers["Authorization"] = f"Bearer {token}"
        connection = self._connect(pairing)
        if pairing and self.last_channel_binding:
            headers["X-SteamOS-Remote-TLS-Binding"] = self.last_channel_binding
        try:
            connection.request(method, path, body=raw or None, headers=headers)
            response = connection.getresponse()
            response_headers = {str(key): str(value) for key, value in response.getheaders()}
            length_value = response_headers.get("Content-Length") or response_headers.get("content-length")
            try:
                if length_value is not None and int(length_value) > MAX_RESPONSE_BYTES:
                    raise ClientError("the device response was too large", "response_too_large")
            except ValueError as exc:
                raise ClientError("the device response length was invalid", "invalid_response") from exc
            raw_response = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw_response) > MAX_RESPONSE_BYTES:
                raise ClientError("the device response was too large", "response_too_large")
            value = _safe_response(raw_response)
            if int(response.status) >= 400:
                error = value.get("error") if isinstance(value.get("error"), str) else "remote_error"
                message = value.get("message") if isinstance(value.get("message"), str) else "the device rejected the request"
                raise ClientResponseError(int(response.status), error, message, _retry_after(response_headers))
            return value
        except ClientError:
            raise
        except (OSError, ssl.SSLError, TimeoutError, http.client.HTTPException) as exc:
            raise ClientError("the device could not be reached", "network_error", unknown=method == "POST") from exc
        finally:
            try:
                connection.close()
            except Exception:
                pass


class RemoteClient:
    """Typed adapter for a single pinned remote host."""

    def __init__(
        self,
        endpoint: str,
        host_id: str,
        certificate_fingerprint: str,
        *,
        token: str | None = None,
        transport_factory: Callable[..., PinnedTransport] | None = None,
    ):
        _, _, normalized = _parse_endpoint(endpoint)
        if not isinstance(host_id, str) or not 1 <= len(host_id) <= 128:
            raise ClientError("the device identity is invalid", "identity_mismatch")
        try:
            host_id = identifier(host_id, "host_id")
        except ProtocolError as exc:
            raise ClientError("the device identity is invalid", "identity_mismatch") from exc
        if token is not None and (not isinstance(token, str) or not token or len(token) > 512):
            raise ClientError("saved credential is invalid", "unauthorized")
        self.endpoint = normalized
        self.host_id = host_id
        self.certificate_fingerprint = _validate_fingerprint(certificate_fingerprint)
        self.token = token
        self.transport_factory = transport_factory

    def _transport(self, *, pairing: bool = False) -> PinnedTransport:
        kwargs = {"timeout": PAIRING_TIMEOUT if pairing else REQUEST_TIMEOUT}
        if self.transport_factory is not None:
            try:
                return self.transport_factory(self.endpoint, self.certificate_fingerprint, **kwargs)
            except TypeError:
                return self.transport_factory(self.endpoint, self.certificate_fingerprint)
        return PinnedTransport(self.endpoint, self.certificate_fingerprint, **kwargs)

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, token: str | None = None, pairing: bool = False) -> dict[str, Any]:
        return self._transport(pairing=pairing).request(method, path, body, token=self.token if token is None else token, pairing=pairing)

    def status(self) -> dict[str, Any]:
        return self.request("GET", "/v1/status")

    def outputs(self) -> dict[str, Any]:
        return self.request("GET", "/v1/display/outputs")

    def operation(self, operation_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/operations/{operation_id}")

    def mutation(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", path, body)

    def pairing_request(
        self,
        nonce: str,
        client_id: str,
        client_name: str,
        scopes: list[str],
        *,
        pairing_session: str | None = None,
        first_request: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "verification_nonce": nonce,
            "client_id": client_id,
            "client_name": client_name,
            "scopes": validate_scopes(scopes),
        }
        if pairing_session:
            body["pairing_session"] = pairing_session
        return self.request("POST", "/v1/pair/request", body, token=None, pairing=first_request or not pairing_session)

    def revoke_self(self, request_id: str) -> dict[str, Any]:
        return self.mutation("/v1/pair/revoke-self", {"request_id": request_id})


def _interface_ipv4_and_mask(interface: str) -> tuple[str, str] | None:
    """Read one Linux interface address without requiring third-party APIs."""
    if os.name != "posix":
        return None
    try:
        import fcntl

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            packed = struct.pack("256s", interface.encode("ascii")[:15])
            address = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), 0x8915, packed)[20:24])
            netmask = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), 0x891b, packed)[20:24])
            return address, netmask
    except (OSError, UnicodeEncodeError, ValueError):
        return None


def local_ipv4_networks() -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    addresses: list[tuple[str, str]] = []
    try:
        interfaces = socket.if_nameindex()
    except (AttributeError, OSError):
        interfaces = []
    for _, name in interfaces:
        value = _interface_ipv4_and_mask(name)
        if value:
            addresses.append(value)
    if not addresses:
        try:
            infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            infos = []
        addresses.extend((str(info[4][0]), "255.255.255.0") for info in infos if info and info[4])
    for address, netmask in addresses:
        try:
            ip = ipaddress.IPv4Address(address)
            mask = ipaddress.IPv4Address(netmask)
            if ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast:
                continue
            network = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
        except ValueError:
            continue
        if network not in networks:
            networks.append(network)
    return networks


def discovery_endpoints(
    *,
    port: int = DEFAULT_PORT,
    networks: Iterable[ipaddress.IPv4Network] | None = None,
    max_addresses: int = DISCOVERY_MAX_ADDRESSES,
) -> list[str]:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ClientError("the discovery port is invalid", "invalid_endpoint")
    result: list[str] = []
    seen: set[str] = set()
    remaining = max(1, min(int(max_addresses), 4096))
    for network in list(networks) if networks is not None else local_ipv4_networks():
        if not isinstance(network, ipaddress.IPv4Network):
            continue
        for address in network.hosts():
            if len(result) >= remaining:
                return result
            endpoint = f"https://{address}:{port}"
            if endpoint not in seen:
                seen.add(endpoint)
                result.append(endpoint)
    return result


def probe_device(endpoint: str, *, timeout: float = DISCOVERY_TIMEOUT) -> dict[str, Any]:
    """Probe only the unauthenticated service marker and certificate identity."""
    transport = PinnedTransport(endpoint, timeout=timeout)
    value = transport.request("GET", "/v1/discovery")
    if value.get("protocol_version") != 1 or value.get("service") != "steamos-remote":
        raise ClientError("the endpoint is not a SteamOS Remote server", "not_remote_server")
    host_id = value.get("host_id")
    fingerprint = value.get("certificate_fingerprint") or transport.last_fingerprint
    if not isinstance(host_id, str) or not host_id or not isinstance(fingerprint, str):
        raise ClientError("the server identity is incomplete", "identity_mismatch")
    fingerprint = _validate_fingerprint(fingerprint)
    if transport.last_fingerprint and fingerprint != transport.last_fingerprint:
        raise IdentityMismatch()
    endpoint_value = transport.endpoint
    return DiscoveryCandidate(
        endpoint=endpoint_value,
        host_id=host_id,
        certificate_fingerprint=fingerprint,
        service="steamos-remote",
        name=value.get("name") if isinstance(value.get("name"), str) else None,
    ).public()


def discover_local_devices(
    *,
    port: int = DEFAULT_PORT,
    networks: Iterable[ipaddress.IPv4Network] | None = None,
    endpoints: Iterable[str] | None = None,
    timeout: float = DISCOVERY_TIMEOUT,
    total_budget: float = DISCOVERY_TOTAL_BUDGET,
    max_addresses: int = DISCOVERY_MAX_ADDRESSES,
    probe: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Run an explicit, bounded discovery scan and return stable-order results."""
    targets = list(endpoints) if endpoints is not None else discovery_endpoints(port=port, networks=networks, max_addresses=max_addresses)
    targets = targets[: max(1, min(int(max_addresses), 4096))]
    if not targets:
        return []
    probe_function = probe or (lambda endpoint: probe_device(endpoint, timeout=timeout))
    deadline = time.monotonic() + max(0.1, min(float(total_budget), 60.0))
    found: dict[int, dict[str, Any]] = {}
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(16, max(1, len(targets))),
        thread_name_prefix="steamos-remote-discovery",
    )
    futures = {executor.submit(probe_function, endpoint): index for index, endpoint in enumerate(targets)}
    pending = set(futures)
    try:
        while pending and time.monotonic() < deadline:
            done, pending = concurrent.futures.wait(
                pending,
                timeout=max(0.01, deadline - time.monotonic()),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                index = futures[future]
                try:
                    value = future.result()
                except Exception:
                    continue
                if isinstance(value, dict) and value.get("endpoint"):
                    found[index] = value
        for future in pending:
            future.cancel()
    finally:
        # A timed-out network probe must not hold the Decky RPC open.  The
        # individual transport still has its own timeout; this non-waiting
        # shutdown only detaches work that was already running.
        executor.shutdown(wait=False, cancel_futures=True)
    return [found[index] for index in sorted(found)]


def build_wake_packet(mac: str) -> bytes:
    normalized = validate_mac(mac)
    address = bytes.fromhex(normalized)
    return b"\xff" * 6 + address * 16


def active_route_ipv4() -> str | None:
    for destination in (("192.0.2.1", 9), ("255.255.255.255", 9)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(destination)
                value = probe.getsockname()[0]
            address = ipaddress.IPv4Address(value)
            if not address.is_loopback and not address.is_unspecified and not address.is_link_local:
                return str(address)
        except (OSError, ValueError, IndexError):
            continue
    return None


def send_wake_packet(mac: str, *, broadcast: str = "255.255.255.255", source_address: str | None = None) -> dict[str, Any]:
    packet = build_wake_packet(mac)
    try:
        destination = str(ipaddress.IPv4Address(broadcast))
    except ValueError as exc:
        raise ClientError("the wake broadcast address is invalid", "wake_send_failed") from exc
    source = None
    if source_address:
        try:
            source = str(ipaddress.IPv4Address(source_address))
        except ValueError as exc:
            raise ClientError("the active network address is invalid", "wake_send_failed") from exc
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if source:
                sender.bind((source, 0))
            sender.sendto(packet, (destination, 9))
    except OSError as exc:
        raise ClientError("could not send the wake packet: check the active network", "wake_send_failed") from exc
    return {"sent": True, "mac": validate_mac(mac), "broadcast": destination, "source_address": source}


def body_digest(body: dict[str, Any]) -> str:
    """Expose the same canonical digest used by host idempotency records."""
    return canonical_digest(body)


def pairing_comparison_code(nonce: str, fingerprint: str) -> str:
    return derive_pairing_code(nonce, _validate_fingerprint(fingerprint))
