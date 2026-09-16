"""Safe SteamOS Gamescope session output preference management."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable


CONNECTOR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
ORIGINAL_SESSION_SCRIPT = Path("/usr/lib/steamos/gamescope-session")
GAMESCOPECTL = Path("/usr/bin/gamescopectl")
SESSION_SENTINEL = "-O '*',eDP-1"
CONFIGURED_OUTPUT_RE = re.compile(r'^GAME_MODE_DISPLAY="([A-Za-z][A-Za-z0-9_.:-]{0,127})"$', re.MULTILINE)
CONFIGURED_OUTPUT_ORDER_RE = re.compile(r'^GAME_MODE_DISPLAY_ORDER="([A-Za-z0-9_.:,-]+)"$', re.MULTILINE)
ACTIVE_CONNECTOR_RE = re.compile(r"Connector Name:\s*([A-Za-z][A-Za-z0-9_.:-]{0,127})")


class GamescopeError(RuntimeError):
    """Raised when the SteamOS session override cannot be managed safely."""


def _host_subprocess_environment() -> dict[str, str]:
    """Return an environment suitable for SteamOS host executables.

    Decky Loader is packaged with PyInstaller, which prepends its temporary
    bundle directory to ``LD_LIBRARY_PATH``.  Host programs such as systemctl
    must instead load the SteamOS libraries they were built against.
    """
    environment = {str(key): str(value) for key, value in os.environ.items()}
    original_library_path = environment.get("LD_LIBRARY_PATH_ORIG")
    if original_library_path is None:
        environment.pop("LD_LIBRARY_PATH", None)
    else:
        environment["LD_LIBRARY_PATH"] = original_library_path
    environment.pop("LD_PRELOAD", None)

    # Decky launches plugin backends from a system service, so these usual
    # user-session variables can be absent even though the deck user's
    # systemd manager and D-Bus socket are running.
    runtime_dir = environment.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    environment["XDG_RUNTIME_DIR"] = runtime_dir
    environment.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    return environment


def _run_user_systemctl(arguments: list[str]) -> None:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
            env=_host_subprocess_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GamescopeError(f"user systemd is unavailable: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise GamescopeError(f"user systemd command failed: {detail[:180]}")


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, mode)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _restore_file(path: Path, previous: bytes | None, mode: int = 0o700) -> None:
    if previous is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.restore.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(previous)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, mode)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


class GamescopeOutputManager:
    """Manage a per-user Gamescope output preference.

    SteamOS currently hard-codes ``-O '*',eDP-1`` in its session launcher.
    The generated wrapper only injects a validated connector when the vendor
    script still has that exact known shape and the connector exists. Every
    other case falls back to the vendor script unchanged.
    """

    def __init__(
        self,
        state_root: str | os.PathLike[str],
        *,
        user_root: str | os.PathLike[str] | None = None,
        original_script: str | os.PathLike[str] = ORIGINAL_SESSION_SCRIPT,
        gamescopectl: str | os.PathLike[str] = GAMESCOPECTL,
        systemctl_runner: Callable[[list[str]], Any] | None = None,
    ):
        self.state_root = Path(state_root)
        self.user_root = Path.home() if user_root is None else Path(user_root)
        self.original_script = Path(original_script)
        self.gamescopectl = Path(gamescopectl)
        self._systemctl_runner = systemctl_runner or _run_user_systemctl
        self.script_path = self.state_root / "gamescope-session"
        self.dropin_path = (
            self.user_root
            / ".config"
            / "systemd"
            / "user"
            / "gamescope-session.service.d"
            / "steamos-remote.conf"
        )

    @staticmethod
    def validate_connector(value: Any) -> str:
        if not isinstance(value, str) or not CONNECTOR_RE.fullmatch(value):
            raise GamescopeError("Gamescope connector is invalid")
        return value

    @classmethod
    def validate_connectors(cls, values: Any) -> list[str]:
        if not isinstance(values, list) or not 1 <= len(values) <= 16:
            raise GamescopeError("Gamescope output order must contain 1 to 16 connectors")
        connectors = [cls.validate_connector(value) for value in values]
        if len(set(connectors)) != len(connectors):
            raise GamescopeError("Gamescope output order contains duplicate connectors")
        return connectors

    def _original_text(self) -> str | None:
        try:
            value = self.original_script.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        if len(value) > 256 * 1024:
            return None
        return value

    def support(self) -> tuple[bool, str]:
        text = self._original_text()
        if text is None:
            return False, "SteamOS gamescope-session is unavailable"
        if text.count(SESSION_SENTINEL) != 1:
            return False, "This SteamOS gamescope-session format is not supported safely"
        return True, ""

    def configured_connector(self) -> str | None:
        connectors = self.configured_connectors()
        return connectors[0] if connectors else None

    def configured_connectors(self) -> list[str]:
        try:
            text = self.script_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return []
        order_match = CONFIGURED_OUTPUT_ORDER_RE.search(text)
        if order_match:
            try:
                return self.validate_connectors(order_match.group(1).split(","))
            except GamescopeError:
                return []
        match = CONFIGURED_OUTPUT_RE.search(text)
        return [match.group(1)] if match else []

    def _gamescope_environment(self) -> dict[str, str]:
        environment = _host_subprocess_environment()
        runtime_dir = environment.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        display = environment.get("GAMESCOPE_WAYLAND_DISPLAY", "")
        if not display:
            try:
                environment_file = Path(runtime_dir) / "gamescope-environment"
                for line in environment_file.read_text(encoding="utf-8").splitlines()[:4096]:
                    if line.startswith("GAMESCOPE_WAYLAND_DISPLAY="):
                        display = line.split("=", 1)[1].strip()
                        break
            except (OSError, UnicodeError):
                pass
        if not display and (Path(runtime_dir) / "gamescope-0").exists():
            display = "gamescope-0"
        if display and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", display):
            environment["GAMESCOPE_WAYLAND_DISPLAY"] = display
        return environment

    def active_connector(self) -> str | None:
        """Read Gamescope's current scanout connector, when its control socket exists."""
        if not self.gamescopectl.is_file() or not os.access(self.gamescopectl, os.X_OK):
            return None
        environment = self._gamescope_environment()
        if "GAMESCOPE_WAYLAND_DISPLAY" not in environment:
            return None
        try:
            result = subprocess.run(
                [str(self.gamescopectl)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        match = ACTIVE_CONNECTOR_RE.search(result.stdout or "")
        return match.group(1) if match else None

    def status(self) -> dict[str, Any]:
        available, reason = self.support()
        configured_connectors = self.configured_connectors()
        active = self.active_connector()
        return {
            "available": available,
            "configured_connector": configured_connectors[0] if configured_connectors else None,
            "configured_connectors": configured_connectors,
            "active_connector": active,
            "readback_available": active is not None,
            "requires_restart": True,
            "restart_available": available,
            "adapter": "gamescope-session-prefer-output" if available else None,
            "reason": reason,
        }

    def restart_session(self) -> dict[str, Any]:
        """Request a non-blocking restart of the fixed Gaming Mode target."""
        available, reason = self.support()
        if not available:
            raise GamescopeError(reason)
        self._systemctl_runner(["--no-block", "restart", "gamescope-session.target"])
        return {
            "accepted": True,
            "restart_scope": "gamescope-session",
        }

    @staticmethod
    def _wrapper_script(connectors: list[str]) -> str:
        # The connectors have already passed CONNECTOR_RE, so embedding them
        # in this fixed shell template cannot introduce shell syntax.
        configured_order = ",".join(connectors)
        launch_order = [*connectors, "'*'"]
        if "eDP-1" not in connectors:
            launch_order.append("eDP-1")
        launch_argument = ",".join(launch_order)
        return f'''#!/usr/bin/env bash

# Generated by SteamOS Remote. The vendor session remains the source of truth.
GAME_MODE_DISPLAY_ORDER="{configured_order}"
ORIGINAL_SCRIPT="/usr/lib/steamos/gamescope-session"

connected_output=""
IFS=',' read -r -a preferred_outputs <<< "$GAME_MODE_DISPLAY_ORDER"
for preferred_output in "${{preferred_outputs[@]}}"; do
    for connector_path in /sys/class/drm/card*-"$preferred_output"; do
        connector_status=""
        if [[ -r "$connector_path/status" ]]; then
            read -r connector_status < "$connector_path/status" || connector_status=""
        fi
        if [[ "$connector_status" == "connected" ]]; then
            connected_output="$preferred_output"
            break 2
        fi
    done
done

if [[ -n "$connected_output" ]] && grep -Fq -- "{SESSION_SENTINEL}" "$ORIGINAL_SCRIPT"; then
    source <(sed "s|-O '\\\\*',eDP-1|-O {launch_argument}|" "$ORIGINAL_SCRIPT")
    exit $?
fi

# No preferred output is connected, or the vendor script changed: use defaults.
exec "$ORIGINAL_SCRIPT"
'''

    def _dropin(self) -> str:
        script = str(self.script_path)
        return f'''[Service]
ExecStart=
ExecStart=/bin/bash -c 'if [[ -x "{script}" ]]; then exec "{script}"; else exec /usr/lib/steamos/gamescope-session; fi'
'''

    @staticmethod
    def _existing(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GamescopeError(f"cannot read managed file: {path.name}") from exc

    def apply(self, connector: str) -> dict[str, Any]:
        return self.apply_order([connector])

    def apply_order(self, connectors: list[str]) -> dict[str, Any]:
        connectors = self.validate_connectors(connectors)
        available, reason = self.support()
        if not available:
            raise GamescopeError(reason)
        previous_script = self._existing(self.script_path)
        previous_dropin = self._existing(self.dropin_path)
        try:
            _atomic_write(self.script_path, self._wrapper_script(connectors), 0o700)
            _atomic_write(self.dropin_path, self._dropin(), 0o600)
            self._systemctl_runner(["daemon-reload"])
        except Exception as exc:
            try:
                _restore_file(self.script_path, previous_script, 0o700)
                _restore_file(self.dropin_path, previous_dropin, 0o600)
            except Exception as rollback_exc:
                raise GamescopeError(f"Gamescope override failed and rollback failed: {rollback_exc}") from exc
            if isinstance(exc, GamescopeError):
                raise
            raise GamescopeError(str(exc)[:220]) from exc
        return {
            "configured_connector": connectors[0],
            "configured_connectors": connectors,
            "requires_restart": True,
            "restart_scope": "gamescope-session",
        }

    def clear(self) -> dict[str, Any]:
        previous_script = self._existing(self.script_path)
        previous_dropin = self._existing(self.dropin_path)
        if previous_script is None and previous_dropin is None:
            return {
                "configured_connector": None,
                "configured_connectors": [],
                "requires_restart": False,
                "restart_scope": "gamescope-session",
            }
        try:
            for path in (self.script_path, self.dropin_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            self._systemctl_runner(["daemon-reload"])
        except Exception as exc:
            try:
                _restore_file(self.script_path, previous_script, 0o700)
                _restore_file(self.dropin_path, previous_dropin, 0o600)
            except Exception as rollback_exc:
                raise GamescopeError(f"Gamescope override clear failed and rollback failed: {rollback_exc}") from exc
            if isinstance(exc, GamescopeError):
                raise
            raise GamescopeError(str(exc)[:220]) from exc
        return {
            "configured_connector": None,
            "configured_connectors": [],
            "requires_restart": True,
            "restart_scope": "gamescope-session",
        }
