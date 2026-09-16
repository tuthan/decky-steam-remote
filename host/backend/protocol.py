"""Small, dependency-free validation helpers for the v1 LAN contract."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Any

from .pairing import decode_verification_nonce


PROTOCOL_VERSION = 1
MAX_JSON_BYTES = 256 * 1024
MAX_ID_LENGTH = 128
MAX_REASON_LENGTH = 256
SCOPES = frozenset({"status.read", "power.control", "display.control", "sunshine.control"})
PAIRING_SCOPES = frozenset({"status.read", "power.control", "display.control"})
MUTATING_ROUTES = frozenset({
    "/v1/power",
    "/v1/display/preview",
    "/v1/display/confirm",
    "/v1/display/restore",
    "/v1/display/save-current",
    "/v1/display/order",
    "/v1/display/order/automatic",
    "/v1/sunshine/restart",
    "/v1/pair/revoke-self",
})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9_.-]{1,253}$")
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{12}$")
_PAIRING_CODE_RE = re.compile(r"^[0-9]{8}$")
_DISPLAY_ORDER_OUTPUT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:|/-]{0,127}$")
PAIRING_SESSION_MIN_LENGTH = 16
PAIRING_SESSION_MAX_LENGTH = 256


class ProtocolError(ValueError):
    """An input rejected by the closed wire contract."""

    def __init__(self, message: str, status: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


def bounded_text(value: Any, field: str, limit: int = MAX_REASON_LENGTH, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ProtocolError(f"{field} must be a non-empty string of at most {limit} characters")
    return value


def identifier(value: Any, field: str = "identifier") -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ProtocolError(f"{field} is invalid")
    return value


def normalize_pairing_code(value: Any) -> str:
    """Accept the displayed 4-4 code while sending/storing only 8 digits."""
    if not isinstance(value, str):
        raise ProtocolError("pairing_code is invalid")
    normalized = value.strip().replace("-", "").replace(" ", "")
    if not _PAIRING_CODE_RE.fullmatch(normalized):
        raise ProtocolError("pairing_code must contain exactly 8 digits")
    return normalized


def verification_nonce(value: Any) -> str:
    """Validate the client's base64url pairing nonce without leaking detail."""
    try:
        decode_verification_nonce(value)
    except ValueError as exc:
        raise ProtocolError("verification_nonce is invalid") from exc
    return value


def pairing_session(value: Any) -> str:
    if not isinstance(value, str) or not PAIRING_SESSION_MIN_LENGTH <= len(value) <= PAIRING_SESSION_MAX_LENGTH:
        raise ProtocolError("pairing_session is invalid")
    return value


def client_name(value: Any) -> str:
    if value is None:
        return "Omarchy client"
    if not isinstance(value, str) or not value.strip() or len(value) > 96:
        raise ProtocolError("client_name is invalid")
    return value.strip()


def validate_scopes(value: Any, *, allow_sunshine: bool = True) -> list[str]:
    if value is None:
        return ["status.read", "power.control", "display.control"]
    if not isinstance(value, list) or len(value) > len(SCOPES):
        raise ProtocolError("scopes must be a bounded array")
    result: list[str] = []
    for scope in value:
        if not isinstance(scope, str) or scope not in SCOPES:
            raise ProtocolError("unsupported scope")
        if scope == "sunshine.control" and not allow_sunshine:
            raise ProtocolError("sunshine.control is not available in this pairing request")
        if scope not in result:
            result.append(scope)
    if "status.read" not in result:
        result.insert(0, "status.read")
    return result


def request_id(body: dict[str, Any]) -> str:
    return identifier(body.get("request_id"), "request_id")


def _display_order_output_keys(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ProtocolError("display order must contain 1 to 16 output keys")
    keys: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _DISPLAY_ORDER_OUTPUT_KEY_RE.fullmatch(item):
            raise ProtocolError("display order contains an invalid output key")
        keys.append(item)
    if len(set(keys)) != len(keys):
        raise ProtocolError("display order contains duplicate output keys")
    return keys


def _display_order_generation(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 2_147_483_647:
        raise ProtocolError("display order generation is invalid")
    return value


def canonical_digest(body: Any) -> str:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    if len(encoded) > MAX_JSON_BYTES:
        raise ProtocolError("request body is too large", 413, "body_too_large")
    return hashlib.sha256(encoded).hexdigest()


def parse_json_body(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_JSON_BYTES:
        raise ProtocolError("request body is too large", 413, "body_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("request body is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("request body must be a JSON object")
    return value


def validate_route_body(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a route without coupling validation to a particular adapter."""
    method = method.upper()
    body = {} if body is None else body
    if method == "GET":
        if body:
            raise ProtocolError("GET requests do not accept a body")
        return body
    if method != "POST":
        raise ProtocolError("method is not supported", 405, "method_not_allowed")
    if path == "/v1/pair/request":
        if "verification_code" in body:
            raise ProtocolError(
                "this host requires an updated SteamOS Companion client; "
                "the client-chosen pairing code is no longer accepted",
                400,
                "pairing_method_unsupported",
            )
        has_verification_nonce = "verification_nonce" in body
        has_code = "pairing_code" in body
        has_payload_auth = "pairing_id" in body or "secret" in body
        if sum((has_verification_nonce, has_code, has_payload_auth)) != 1:
            raise ProtocolError("pairing request must contain one bootstrap form")
        if has_verification_nonce:
            verification_nonce(body.get("verification_nonce"))
        elif has_code:
            normalize_pairing_code(body.get("pairing_code"))
        else:
            identifier(body.get("pairing_id"), "pairing_id")
            bounded_text(body.get("secret"), "secret", 256, required=True)
        if "pairing_session" in body:
            pairing_session(body.get("pairing_session"))
        identifier(body.get("client_id"), "client_id")
        client_name(body.get("client_name"))
        validate_scopes(body.get("scopes"))
        return body
    if path == "/v1/power":
        request_id(body)
        action = body.get("action")
        if action not in {"suspend", "restart", "shutdown"}:
            raise ProtocolError("action is unsupported")
        return body
    if path == "/v1/display/order":
        if set(body) != {"request_id", "output_keys", "generation", "restart"}:
            raise ProtocolError("display order accepts only request_id, output_keys, generation, and restart")
        request_id(body)
        _display_order_output_keys(body.get("output_keys"))
        _display_order_generation(body.get("generation"))
        if type(body.get("restart")) is not bool:
            raise ProtocolError("restart must be a boolean")
        return body
    if path == "/v1/display/order/automatic":
        if set(body) != {"request_id"}:
            raise ProtocolError("automatic display-order reset accepts only request_id")
        request_id(body)
        return body
    if path in {"/v1/display/preview", "/v1/display/confirm", "/v1/display/restore", "/v1/display/save-current"}:
        request_id(body)
        if path == "/v1/display/preview":
            identifier(body.get("output_id"), "output_id")
            identifier(body.get("mode_id"), "mode_id")
            generation = body.get("generation")
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0 or generation > 2_147_483_647:
                raise ProtocolError("generation is invalid")
        elif path == "/v1/display/confirm":
            identifier(body.get("preview_id"), "preview_id")
            if body.get("visible") is not True:
                raise ProtocolError("visible must be true to confirm a display preview")
        elif path == "/v1/display/save-current":
            if set(body) != {"request_id", "output_id", "generation", "visible"}:
                raise ProtocolError("save-current accepts only request_id, output_id, generation, and visible")
            identifier(body.get("output_id"), "output_id")
            generation = body.get("generation")
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0 or generation > 2_147_483_647:
                raise ProtocolError("generation is invalid")
            if body.get("visible") is not True:
                raise ProtocolError("visible must be true to save the current display mode")
        else:
            source = body.get("source", "verified")
            if source not in {"verified", "preview"}:
                raise ProtocolError("restore source is unsupported")
            if body.get("profile_id") is not None:
                identifier(body.get("profile_id"), "profile_id")
        return body
    if path == "/v1/sunshine/restart":
        request_id(body)
        if set(body) - {"request_id"}:
            raise ProtocolError("sunshine restart accepts only request_id")
        return body
    if path == "/v1/pair/revoke-self":
        request_id(body)
        return body
    raise ProtocolError("route is not supported", 404, "not_found")


def validate_mac(value: Any) -> str:
    if not isinstance(value, str):
        raise ProtocolError("MAC address is invalid")
    normalized = value.replace(":", "").replace("-", "")
    if not _MAC_RE.fullmatch(normalized):
        raise ProtocolError("MAC address is invalid")
    return normalized.lower()


def validate_host(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ProtocolError("host is invalid")
    if ":" in value:
        try:
            ipaddress.IPv6Address(value)
        except ValueError as exc:
            raise ProtocolError("host is invalid") from exc
    elif not _HOST_RE.fullmatch(value):
        raise ProtocolError("host is invalid")
    return value


def redact_text(value: Any, limit: int = MAX_REASON_LENGTH) -> str:
    if value is None:
        return ""
    value = str(value).replace("\x00", "")
    return value[:limit]
