from __future__ import annotations

import json
import unittest
from pathlib import Path

from host.backend.pairing import decode_verification_nonce, derive_pairing_code
from host.backend.protocol import ProtocolError, validate_route_body


# base64url of bytes(range(16)); the pinned known-answer vector below is shared
# with the Omarchy client so both sides derive identical digits.
NONCE_VECTOR = "AAECAwQFBgcICQoLDA0ODw"
FINGERPRINT_A = "sha256:" + "a" * 64
FINGERPRINT_B = "sha256:" + "b" * 64


class ProtocolTests(unittest.TestCase):
    def test_fixtures_are_json_and_have_protocol_version(self):
        for path in sorted((Path(__file__).parents[1] / "protocol" / "fixtures").glob("*.json")):
            value = json.loads(path.read_text())
            self.assertEqual(value["protocol_version"], 1, path.name)

    def test_sunshine_route_has_no_command_escape_hatch(self):
        with self.assertRaises(ProtocolError):
            validate_route_body("POST", "/v1/sunshine/restart", {"request_id": "r", "command": "start"})

    def test_preview_requires_exact_target(self):
        with self.assertRaises(ProtocolError):
            validate_route_body("POST", "/v1/display/preview", {"request_id": "r", "output_id": "1", "mode_id": "2"})

    def test_pair_request_accepts_exactly_one_bootstrap_form(self):
        common = {"client_id": "client-test", "client_name": "Test", "scopes": ["status.read"]}
        nonce = NONCE_VECTOR
        value = validate_route_body("POST", "/v1/pair/request", {**common, "verification_nonce": nonce})
        self.assertEqual(value["verification_nonce"], nonce)
        value = validate_route_body("POST", "/v1/pair/request", {**common, "verification_nonce": nonce, "pairing_session": "session-value-123456"})
        self.assertEqual(value["pairing_session"], "session-value-123456")
        legacy = validate_route_body("POST", "/v1/pair/request", {**common, "pairing_code": "1234-5678"})
        self.assertEqual(legacy["pairing_code"], "1234-5678")
        with self.assertRaises(ProtocolError):
            validate_route_body("POST", "/v1/pair/request", {**common, "verification_nonce": nonce, "pairing_code": "1234-5678"})
        with self.assertRaises(ProtocolError):
            validate_route_body("POST", "/v1/pair/request", {**common, "pairing_code": "1234-567"})

    def test_legacy_client_chosen_verification_code_is_refused(self):
        common = {"client_id": "client-test", "client_name": "Test", "scopes": ["status.read"]}
        with self.assertRaises(ProtocolError) as refused:
            validate_route_body("POST", "/v1/pair/request", {**common, "verification_code": "1234-5678"})
        self.assertEqual(refused.exception.status, 400)
        self.assertEqual(refused.exception.code, "pairing_method_unsupported")
        self.assertEqual(
            str(refused.exception),
            "this host requires an updated SteamOS Remote client; "
            "the client-chosen pairing code is no longer accepted",
        )
        # Pairing it with a valid nonce must not launder the legacy key through.
        with self.assertRaises(ProtocolError) as both:
            validate_route_body(
                "POST", "/v1/pair/request",
                {**common, "verification_code": "1234-5678", "verification_nonce": NONCE_VECTOR},
            )
        self.assertEqual(both.exception.code, "pairing_method_unsupported")

    def test_malformed_verification_nonce_is_rejected(self):
        common = {"client_id": "client-test", "client_name": "Test", "scopes": ["status.read"]}
        for bad in (
            None,
            12345678,
            "",
            "short",
            "AAECAwQFBgcICQoLDA0ODw==",          # padded, over the length bound
            "AAECAwQFBgcICQoLDA0O",              # 15 bytes
            "AAECAwQFBgcICQoLDA0ODxA",           # 17 bytes
            "AAECAwQFBgcICQoLDA0OD*",            # illegal base64url character
            "AAECAwQFBgcICQoLDA0OD/",            # standard-alphabet base64
        ):
            with self.subTest(nonce=bad):
                with self.assertRaises(ValueError):
                    decode_verification_nonce(bad)
                with self.assertRaises(ProtocolError):
                    validate_route_body("POST", "/v1/pair/request", {**common, "verification_nonce": bad})


class PairingDerivationTests(unittest.TestCase):
    """The displayed code is bound to the host certificate, never transmitted."""

    def test_known_answer_vector_pins_the_shared_derivation(self):
        # Hard-coded so the Decky host and the Omarchy client cannot drift.
        self.assertEqual(derive_pairing_code(NONCE_VECTOR, FINGERPRINT_A), "04164056")

    def test_derivation_is_deterministic(self):
        first = derive_pairing_code(NONCE_VECTOR, FINGERPRINT_A)
        second = derive_pairing_code(NONCE_VECTOR, FINGERPRINT_A)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9]{8}$")

    def test_relay_with_its_own_certificate_derives_a_different_code(self):
        # The rogue listener receives the nonce and derives with its own
        # certificate; the real host derives with its own. The owner compares
        # two different strings, so the relay attack is visible.
        rogue = derive_pairing_code(NONCE_VECTOR, FINGERPRINT_A)
        honest_host = derive_pairing_code(NONCE_VECTOR, FINGERPRINT_B)
        self.assertNotEqual(rogue, honest_host)

    def test_derivation_rejects_a_malformed_certificate_fingerprint(self):
        for bad in (None, "", "sha256:" + "A" * 64, "sha256:" + "a" * 63, "a" * 64):
            with self.subTest(fingerprint=bad):
                with self.assertRaises(ValueError):
                    derive_pairing_code(NONCE_VECTOR, bad)
