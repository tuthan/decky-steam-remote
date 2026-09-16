import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from host.backend.gamescope import GamescopeError, GamescopeOutputManager, _host_subprocess_environment


class GamescopeOutputTests(unittest.TestCase):
    def test_host_subprocess_environment_restores_pre_pyinstaller_library_path(self):
        frozen_environment = {
            "LD_LIBRARY_PATH": "/tmp/_MEI123:/usr/lib",
            "LD_LIBRARY_PATH_ORIG": "/usr/lib",
            "LD_PRELOAD": "/tmp/_MEI123/libshim.so",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }
        with mock.patch.dict(os.environ, frozen_environment, clear=True):
            environment = _host_subprocess_environment()

        self.assertEqual(environment["LD_LIBRARY_PATH"], "/usr/lib")
        self.assertNotIn("LD_PRELOAD", environment)
        self.assertEqual(environment["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertEqual(environment["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/1000/bus")

    def test_host_subprocess_environment_clears_injected_library_path_without_original(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI123"}, clear=True):
            environment = _host_subprocess_environment()

        self.assertNotIn("LD_LIBRARY_PATH", environment)
        runtime_dir = f"/run/user/{os.getuid()}"
        self.assertEqual(environment["XDG_RUNTIME_DIR"], runtime_dir)
        self.assertEqual(environment["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={runtime_dir}/bus")

    def test_apply_writes_guarded_wrapper_and_user_dropin(self):
        with tempfile.TemporaryDirectory() as state_directory, tempfile.TemporaryDirectory() as user_directory, tempfile.TemporaryDirectory() as vendor_directory:
            original = Path(vendor_directory) / "gamescope-session"
            original.write_text("#!/usr/bin/env bash\nexec gamescope -O '*',eDP-1\n", encoding="utf-8")
            calls = []
            manager = GamescopeOutputManager(
                state_directory,
                user_root=user_directory,
                original_script=original,
                gamescopectl=Path(vendor_directory) / "gamescopectl",
                systemctl_runner=lambda arguments: calls.append(arguments),
            )

            self.assertEqual(manager.support(), (True, ""))
            result = manager.apply_order(["DP-1", "HDMI-A-2"])

            self.assertEqual(result["configured_connector"], "DP-1")
            self.assertEqual(result["configured_connectors"], ["DP-1", "HDMI-A-2"])
            self.assertTrue(result["requires_restart"])
            self.assertEqual(manager.configured_connector(), "DP-1")
            self.assertEqual(manager.configured_connectors(), ["DP-1", "HDMI-A-2"])
            wrapper = manager.script_path.read_text(encoding="utf-8")
            self.assertIn('GAME_MODE_DISPLAY_ORDER="DP-1,HDMI-A-2"', wrapper)
            self.assertIn("-O DP-1,HDMI-A-2,'*',eDP-1", wrapper)
            self.assertIn("eDP-1", wrapper)
            syntax = subprocess.run(["bash", "-n"], input=wrapper, text=True, capture_output=True, check=False)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            self.assertTrue(os.access(manager.script_path, os.X_OK))
            dropin = manager.dropin_path.read_text(encoding="utf-8")
            self.assertIn(str(manager.script_path), dropin)
            self.assertEqual(calls, [["daemon-reload"]])

            manager.clear()
            self.assertFalse(manager.script_path.exists())
            self.assertFalse(manager.dropin_path.exists())
            self.assertEqual(calls, [["daemon-reload"], ["daemon-reload"]])

    def test_restart_session_uses_fixed_non_blocking_user_target(self):
        with tempfile.TemporaryDirectory() as state_directory, tempfile.TemporaryDirectory() as vendor_directory:
            original = Path(vendor_directory) / "gamescope-session"
            original.write_text("#!/usr/bin/env bash\nexec gamescope -O '*',eDP-1\n", encoding="utf-8")
            calls = []
            manager = GamescopeOutputManager(
                state_directory,
                original_script=original,
                systemctl_runner=lambda arguments: calls.append(arguments),
            )

            result = manager.restart_session()

            self.assertEqual(result, {"accepted": True, "restart_scope": "gamescope-session"})
            self.assertEqual(calls, [["--no-block", "restart", "gamescope-session.target"]])

    def test_unsupported_vendor_script_is_read_only(self):
        with tempfile.TemporaryDirectory() as state_directory, tempfile.TemporaryDirectory() as vendor_directory:
            original = Path(vendor_directory) / "gamescope-session"
            original.write_text("#!/usr/bin/env bash\nexec gamescope\n", encoding="utf-8")
            manager = GamescopeOutputManager(state_directory, original_script=original, gamescopectl=Path(vendor_directory) / "gamescopectl")

            available, reason = manager.support()
            self.assertFalse(available)
            self.assertIn("not supported safely", reason)
            with self.assertRaises(GamescopeError):
                manager.apply("DP-1")
            self.assertFalse(manager.script_path.exists())

    def test_connector_validation_rejects_shell_syntax(self):
        with self.assertRaises(GamescopeError):
            GamescopeOutputManager.validate_connector("DP-1; reboot")
        with self.assertRaises(GamescopeError):
            GamescopeOutputManager.validate_connectors(["DP-1", "DP-1"])


if __name__ == "__main__":
    unittest.main()
