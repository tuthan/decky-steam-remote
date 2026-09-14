import asyncio
import base64
import importlib.util
import json
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wire = load("wire", ROOT / "decode_wire.py")


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.decky = types.SimpleNamespace(DECKY_PLUGIN_RUNTIME_DIR=self.temp.name,
                                          logger=logging.getLogger("probe-test"))
        with patch.dict(sys.modules, {"decky": self.decky}):
            self.probe = load("probe", ROOT / "main.py")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_evidence_is_private_bounded_and_rejects_symlinks(self):
        p = self.probe
        await p.record("test", {"data": 1})
        target = p.evidence_path()
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(target.read_text())["kind"], "test")
        before = target.read_bytes()
        with self.assertRaises(ValueError):
            await p.record("test", {"data": "x" * p.MAX_EVENT_BYTES})
        self.assertEqual(target.read_bytes(), before)
        with patch.object(p, "MAX_LOG_BYTES", len(before)):
            with self.assertRaises(ValueError):
                await p.record("test", {})
        target.unlink()
        other = Path(self.temp.name) / "unrelated"
        other.write_text("keep")
        target.symlink_to(other)
        with self.assertRaises(OSError):
            await p.record("test", {})
        self.assertEqual(other.read_text(), "keep")

    async def test_capture_only_queries_four_capabilities_and_read_only_nic(self):
        calls = []

        async def query(argv):
            calls.append(argv)
            return {"stdout": 's "challenge"', "exit_code": 0}

        with patch.object(self.probe, "query_command", query), patch.object(self.probe.os, "geteuid", return_value=1000), patch.object(self.probe, "record", new=unittest.mock.AsyncMock()):
            report = await self.probe.capture_backend()
        queries = [argv[-1] for argv in calls if argv[0] == "/usr/bin/busctl"]
        self.assertEqual(queries, list(self.probe.POWER_QUERIES))
        self.assertEqual(len(report["logind"]), 4)
        for argv in calls:
            self.assertNotIn("sudo", argv)
            self.assertNotIn("Suspend", argv)
            self.assertNotIn("-s", argv)

    def test_parse_wol_extracts_read_only_ethtool_fields(self):
        result = self.probe.parse_wol({
            "stdout": "Supports Wake-on: pumbg\nWake-on: g\n",
            "exit_code": 0,
        })
        self.assertEqual(result["supports_wake_on"], "pumbg")
        self.assertEqual(result["wake_on"], "g")
        self.assertEqual(result["command_error"], None)
        self.assertEqual(result["exit_code"], 0)

    def test_parse_wol_preserves_missing_command_error(self):
        result = self.probe.parse_wol({"error": "missing ethtool"})
        self.assertIsNone(result["supports_wake_on"])
        self.assertIsNone(result["wake_on"])
        self.assertEqual(result["command_error"], "missing ethtool")

    async def test_root_capture_stops_before_any_queries(self):
        command = unittest.mock.AsyncMock()
        with patch.object(self.probe.os, "geteuid", return_value=0), patch.object(self.probe, "query_command", command), patch.object(self.probe, "record", new=unittest.mock.AsyncMock()):
            result = await self.probe.capture_backend()
        self.assertEqual(result["error"], "unexpected_root")
        command.assert_not_awaited()

    async def test_only_frontend_event_namespace_is_accepted(self):
        with self.assertRaises(ValueError):
            await self.probe.Plugin.record_frontend(self.probe.Plugin, {"kind": "backend.identity"})


class WireTests(unittest.TestCase):
    def test_wire_values_remain_unlabelled(self):
        result = wire.decode(base64.b64encode(b"\x08\x96\x01\x12\x02ab").decode())
        self.assertFalse(result["schema_verified"])
        self.assertEqual(result["fields"][0]["unsigned_varint"], 150)
        self.assertEqual(result["fields"][1]["hex"], "6162")

    def test_malformed_wire_is_rejected(self):
        for raw in (b"\x80", b"\x00", b"\x12\x05x", b"\x0b", b"\x08" + b"\xff" * 11):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                wire.decode(base64.b64encode(raw).decode())


if __name__ == "__main__":
    unittest.main()
