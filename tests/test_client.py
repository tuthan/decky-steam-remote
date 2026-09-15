from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest

from host.backend.bridge import BridgeBroker
from host.backend.client import BoundedWorkQueue, ClientService
from host.backend.client_core import ClientError, body_digest
from host.backend.coordinator import DeviceCoordinator
from host.backend.service import HostService


FINGERPRINT = "sha256:" + "a" * 64
REMOTE_CANDIDATE = {
    "endpoint": "https://192.168.50.42:18443",
    "host_id": "remote-host",
    "certificate_fingerprint": FINGERPRINT,
    "name": "Living room Deck",
}


def bridge_snapshot(current: str = "3352", generation: int = 4) -> dict:
    return {
        "ready": True,
        "methods": {"suspend": True, "restart": True, "shutdown": True, "display": True},
        "outputs": [{
            "id": "1",
            "name": "HDMI-A-1",
            "description": "Reference",
            "is_internal": False,
            "current_mode_id": current,
            "modes": [
                {"id": "3352", "width": 3440, "height": 1440, "refresh_hz": 59},
                {"id": "2", "width": 3840, "height": 2160, "refresh_hz": 60},
            ],
            "generation": generation,
            "rgb_range": 0,
        }],
        "cpu_temperature": None,
    }


class FakeRemoteCore:
    def __init__(self):
        self.approved = False
        self.unknown_mutation = False
        self.mutations: list[tuple[str, dict]] = []

    def pairing_request(self, nonce, client_id, client_name, scopes, *, pairing_session=None, first_request=False):
        if first_request:
            return {
                "state": "pending",
                "pairing_id": "pair-remote-1",
                "pairing_session": "pair-session-1",
                "expires_at": time.time() + 120,
            }
        if not self.approved:
            return {"state": "pending", "pairing_id": "pair-remote-1", "expires_at": time.time() + 120}
        return {
            "state": "approved",
            "pairing_id": "pair-remote-1",
            "credential": {
                "token": "remote-secret-token",
                "scopes": list(scopes),
            },
            "wake_target": {"available": False, "reason": "not configured"},
        }

    def status(self):
        return {
            "protocol_version": 1,
            "host_id": "remote-host",
            "steam_bridge": "ready",
            "capabilities": {
                "suspend": "available",
                "restart": "available",
                "shutdown": "available",
                "display_rescue": "available",
                "sunshine_restart": "disabled",
            },
            "wake_target": {"available": False},
        }

    def outputs(self):
        return {
            "available": True,
            "generation": 4,
            "outputs": bridge_snapshot()["outputs"],
            "profiles": [],
            "preview": None,
        }

    def operation(self, operation_id):
        return {"operation": {"id": operation_id, "state": "succeeded", "outcome": "observed"}}

    def mutation(self, path, body):
        self.mutations.append((path, dict(body)))
        if self.unknown_mutation:
            raise ClientError("the request result is unknown", "network_error", unknown=True)
        return {"operation": {"id": "remote-op-%d" % len(self.mutations), "state": "accepted", "outcome": None}}


class ClientTests(unittest.TestCase):
    def test_bounded_queue_deduplicates_reads_and_settles_rejected_mutations(self):
        queue = BoundedWorkQueue(max_workers=1, max_pending=2)
        started = threading.Event()
        release = threading.Event()
        settled: list[dict] = []
        try:
            def slow_read():
                started.set()
                release.wait(1)
                return "read"

            first = queue.submit_read("status", slow_read)
            self.assertTrue(started.wait(1))
            duplicate = queue.submit_read("status", lambda: "not used")
            self.assertIs(first, duplicate)
            queued = queue.submit_read("outputs", lambda: "outputs")
            rejected = queue.submit_mutation("power", lambda: "not used", on_settled=settled.append)
            with self.assertRaises(ClientError) as error:
                rejected.result()
            self.assertEqual(error.exception.code, "work_queue_full")
            self.assertTrue(settled)
            release.set()
            self.assertEqual(first.result(1), "read")
            self.assertEqual(queued.result(1), "outputs")
        finally:
            release.set()
            queue.shutdown()

    def test_pending_pairing_survives_reload_without_leaking_private_material(self):
        with tempfile.TemporaryDirectory() as directory:
            core = FakeRemoteCore()
            factory = lambda *args, **kwargs: core
            service = ClientService(
                directory,
                local_host_id="local-host",
                local_certificate_fingerprint="sha256:" + "b" * 64,
                core_factory=factory,
            )
            service.start()
            try:
                pending = service.request_pairing(REMOTE_CANDIDATE)
                self.assertEqual(pending["status"], "pending")
                self.assertNotIn("nonce", pending)
                self.assertNotIn("pairing_session", pending)
                private_pending = service.store.get("pending_pairing")
                self.assertTrue(private_pending["nonce"])
                pending_id = pending["id"]
            finally:
                service.stop()

            restored = ClientService(
                directory,
                local_host_id="local-host",
                local_certificate_fingerprint="sha256:" + "b" * 64,
                core_factory=factory,
            )
            restored.start()
            try:
                self.assertEqual(restored.public_status()["pending_pairing"]["id"], pending_id)
                core.approved = True
                approved = restored.poll_pairing(pending_id)
                self.assertEqual(approved["state"], "approved")
                self.assertFalse(approved["needs_confirmation"])
                public = restored.public_status()
                self.assertEqual(public["remote"]["host_id"], "remote-host")
                self.assertNotIn("token", public["remote"])
                self.assertNotIn("remote-secret-token", json.dumps(public))
            finally:
                restored.stop()

    def test_discovery_deduplicates_identity_and_reports_scan_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            candidates = [
                REMOTE_CANDIDATE,
                dict(REMOTE_CANDIDATE),
                {**REMOTE_CANDIDATE, "host_id": "local-host"},
                {**REMOTE_CANDIDATE, "certificate_fingerprint": "not-a-pin"},
            ]
            service = ClientService(
                directory,
                local_host_id="local-host",
                local_certificate_fingerprint="sha256:" + "b" * 64,
                discovery_function=lambda **kwargs: candidates,
            )
            service.start()
            try:
                found = service.discover()
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["host_id"], "remote-host")
            finally:
                service.stop()

            failed = ClientService(
                directory + "-failed",
                discovery_function=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("scan failed")),
            )
            failed.start()
            try:
                scan = failed.begin_discovery()
                deadline = time.time() + 1
                result = None
                while time.time() < deadline:
                    result = failed.poll_discovery(scan["scan_id"])
                    if result["state"] != "searching":
                        break
                    time.sleep(0.01)
                self.assertEqual(result["state"], "failed")
                self.assertIn("scan failed", result["error"])
            finally:
                failed.stop()

    def test_client_action_persists_exact_body_and_requires_explicit_unknown_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            core = FakeRemoteCore()
            service = ClientService(directory, core_factory=lambda *args, **kwargs: core)
            service.store.mutate(lambda state: state.__setitem__("remote", {
                **REMOTE_CANDIDATE,
                "token": "remote-secret-token",
                "scopes": ["status.read", "power.control", "display.control"],
                "status": None,
                "outputs": [],
                "profiles": [],
                "preview": None,
            }))
            service.start()
            try:
                service.read_status()
                accepted = service.power("suspend")
                self.assertEqual(accepted["state"], "accepted")
                action_id = accepted["id"]
                action = service.store.get("operations", {})[action_id]
                self.assertEqual(core.mutations[0][0], "/v1/power")
                self.assertEqual(core.mutations[0][1], action["body"])
                self.assertEqual(action["body_digest"], body_digest(action["body"]))

                checked = service.check_operation(action_id)
                self.assertEqual(checked["state"], "succeeded")
                self.assertTrue(checked["read_only"])

                core.unknown_mutation = True
                with self.assertRaises(ClientError) as unknown:
                    service.power("restart")
                self.assertTrue(unknown.exception.unknown)
                unknown_action = service.public_status()["last_action"]
                self.assertEqual(unknown_action["state"], "unknown")
                self.assertTrue(unknown_action["retry_allowed"])
                with self.assertRaises(ClientError) as ack:
                    service.resend_action(unknown_action["id"])
                self.assertEqual(ack.exception.code, "resend_ack_required")

                core.unknown_mutation = False
                resent = service.resend_action(unknown_action["id"], acknowledge_earlier_may_have_run=True)
                self.assertEqual(resent["state"], "accepted")
                self.assertEqual(core.mutations[-1][1], service.store.get("operations", {})[unknown_action["id"]]["body"])
                public_json = json.dumps(service.public_status())
                self.assertNotIn("remote-secret-token", public_json)
                self.assertNotIn('"body"', public_json)
            finally:
                service.stop()

    def test_reload_turns_an_unsettled_send_into_an_explicit_unknown_result(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ClientService(directory)
            service.store.mutate(lambda state: state.__setitem__("remote", {
                **REMOTE_CANDIDATE,
                "token": "remote-secret-token",
                "scopes": ["status.read", "power.control"],
            }))
            action = service._new_action("restart", "/v1/power", {"action": "restart"})
            service.stop()

            restored = ClientService(directory)
            try:
                visible = restored.public_status()["last_action"]
                self.assertEqual(visible["id"], action["id"])
                self.assertEqual(visible["state"], "unknown")
                self.assertTrue(visible["retry_allowed"])
                self.assertIn("reloaded", visible["reason"])
            finally:
                restored.stop()

    def test_save_current_requires_owner_confirmation_and_verified_readback(self):
        temp = tempfile.TemporaryDirectory()
        bridge = BridgeBroker()
        bridge.report_snapshot(bridge_snapshot())
        service = HostService(temp.name, bridge=bridge)
        service._tls = {"ready": True, "fingerprint": FINGERPRINT}
        try:
            created = service.create_pairing(["status.read", "display.control"])
            from host.backend.pairing import decode_payload

            payload = decode_payload(created["payload"])
            request = {
                "pairing_id": payload["pairing_id"],
                "secret": payload["secret"],
                "client_name": "Client",
                "client_id": "client-save-current",
                "scopes": ["status.read", "display.control"],
            }
            service.handle_http("POST", "/v1/pair/request", {}, request)
            service.approve_pairing(payload["pairing_id"])
            credential = service.handle_http("POST", "/v1/pair/request", {}, request)[2]["credential"]
            headers = {"Authorization": "Bearer " + credential["token"]}
            body = {"request_id": "save-current-1", "output_id": "1", "generation": 4, "visible": True}
            with self.assertRaises(Exception) as not_confirmed:
                service.handle_http("POST", "/v1/display/save-current", headers, {**body, "visible": False})
            self.assertEqual(not_confirmed.exception.code, "invalid_request")
            saved = service.handle_http("POST", "/v1/display/save-current", headers, body)[2]
            self.assertEqual(saved["operation"]["state"], "succeeded")
            self.assertEqual(saved["profile"]["mode"]["id"], "3352")
            self.assertEqual(len(service.store.get("profiles", {})), 1)

            service.store.mutate(lambda state: state.__setitem__("preview", {"preview_id": "preview-1"}))
            with self.assertRaises(Exception) as preview_active:
                service.handle_http("POST", "/v1/display/save-current", headers, {**body, "request_id": "save-current-2"})
            self.assertEqual(preview_active.exception.code, "mutation_conflict")
        finally:
            service.stop()
            temp.cleanup()

    def test_coordinator_switches_roles_without_replacing_host_state(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = DeviceCoordinator(directory)
            self.assertFalse(coordinator.get_settings()["setup_complete"])
            self.assertIsNotNone(coordinator.host)
            coordinator.host.update_settings({"listen_enabled": False})
            coordinator.start()

            def wait_for(mode: str):
                deadline = time.time() + 2
                while time.time() < deadline:
                    status = coordinator.get_settings()
                    if status["mode"]["selected"] == mode and status["mode"]["transition"] is None:
                        return status
                    time.sleep(0.01)
                self.fail("mode transition did not finish: %r" % coordinator.get_settings())

            coordinator.set_mode("client", client_name="Remote controller")
            client_status = wait_for("client")
            self.assertTrue(client_status["roles"]["client"]["running"])
            self.assertFalse(client_status["roles"]["server"]["running"])
            host_id = coordinator.host.host_id

            coordinator.set_mode("both")
            both_status = wait_for("both")
            self.assertTrue(both_status["roles"]["client"]["running"])
            self.assertTrue(both_status["roles"]["server"]["running"])

            coordinator.set_mode("server")
            server_status = wait_for("server")
            self.assertFalse(server_status["roles"]["client"]["running"])
            self.assertTrue(server_status["roles"]["server"]["running"])
            self.assertEqual(coordinator.host.host_id, host_id)

            coordinator.set_mode("client")
            final_status = wait_for("client")
            self.assertTrue(final_status["roles"]["client"]["running"])
            self.assertFalse(final_status["roles"]["server"]["running"])
            coordinator.stop()


if __name__ == "__main__":
    unittest.main()
