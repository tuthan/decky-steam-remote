"""Short-lived, copyable pairing payloads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any


PREFIX = "steamos-companion:v1:"
MAX_PAYLOAD_LENGTH = 4096


def encode_payload(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    result = PREFIX + encoded
    if len(result) > MAX_PAYLOAD_LENGTH:
        raise ValueError("pairing payload is too large")
    return result


def decode_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.startswith(PREFIX) or len(value) > MAX_PAYLOAD_LENGTH:
        raise ValueError("pairing payload has an unsupported format")
    encoded = value[len(PREFIX):]
    if not encoded:
        raise ValueError("pairing payload is empty")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        result = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("pairing payload is malformed") from exc
    if not isinstance(result, dict) or result.get("protocol_version") != 1:
        raise ValueError("pairing payload protocol is unsupported")
    required = {"endpoint", "host_id", "certificate_fingerprint", "pairing_id", "secret", "expires_at"}
    if not required.issubset(result):
        raise ValueError("pairing payload is incomplete")
    return result


SAS_SALT_PREFIX = b"steamos-companion:v1:pairing-sas:"
SAS_NONCE_BYTES = 16
SAS_SCRYPT_N = 2 ** 14
SAS_SCRYPT_R = 8
SAS_SCRYPT_P = 1
SAS_MAXMEM = 64 * 1024 * 1024


def decode_verification_nonce(value: Any) -> bytes:
    """Decode the client's base64url pairing nonce."""
    if not isinstance(value, str) or not 20 <= len(value) <= 32:
        raise ValueError("verification nonce is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("verification nonce is invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("verification nonce is invalid") from exc
    if len(raw) != SAS_NONCE_BYTES:
        raise ValueError("verification nonce is invalid")
    return raw


def derive_pairing_code(nonce: Any, certificate_fingerprint: Any) -> str:
    """Derive the comparison code from the nonce and the host certificate.

    The certificate fingerprint is part of the salt, so a relay with its own
    certificate cannot make both screens show the same digits, and scrypt
    makes searching the 10^8 code space infeasible inside the request window.
    """
    raw = decode_verification_nonce(nonce)
    if not isinstance(certificate_fingerprint, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", certificate_fingerprint):
        raise ValueError("certificate fingerprint is invalid")
    derived = hashlib.scrypt(
        raw,
        salt=SAS_SALT_PREFIX + certificate_fingerprint.encode("ascii"),
        n=SAS_SCRYPT_N,
        r=SAS_SCRYPT_R,
        p=SAS_SCRYPT_P,
        maxmem=SAS_MAXMEM,
        dklen=8,
    )
    return f"{int.from_bytes(derived, 'big') % 100_000_000:08d}"
