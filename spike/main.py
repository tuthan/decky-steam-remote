"""Local-only Decky probe. No listener, power mutations, or configuration writes.

api_version 0 is intentional: the disposable plain-JS frontend uses Decky's
existing legacy adapter, avoiding a downloaded build toolchain. Do not inherit
this packaging choice for the production application.
"""
import asyncio
import json
import os
import platform
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

import decky

MAX_EVENT_BYTES = 65536
MAX_LOG_BYTES = 4 * 1024 * 1024
POWER_QUERIES = ("CanSuspend", "CanReboot", "CanPowerOff", "CanHibernate")
_write_lock = None


def read_text(path, limit=8192):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError as exc:
        return {"error": str(exc)}


def evidence_path():
    return Path(decky.DECKY_PLUGIN_RUNTIME_DIR) / "spike-evidence.jsonl"


def parse_wol(result):
    """Extract the two read-only Wake-on-LAN fields from ethtool output."""
    stdout = result.get("stdout", "") if isinstance(result, dict) else ""

    def field(label):
        match = re.search(rf"^\s*{re.escape(label)}\s*:\s*(.*?)\s*$", stdout,
                          flags=re.IGNORECASE | re.MULTILINE)
        return match.group(1) if match else None

    return {
        "supports_wake_on": field("Supports Wake-on"),
        "wake_on": field("Wake-on"),
        "command_error": result.get("error") if isinstance(result, dict) else "invalid result",
        "exit_code": result.get("exit_code") if isinstance(result, dict) else None,
    }


async def record(kind, data):
    global _write_lock
    if _write_lock is None:
        _write_lock = asyncio.Lock()
    entry = {"utc": datetime.now(timezone.utc).isoformat(), "kind": kind, "data": data}
    encoded = (json.dumps(entry, ensure_ascii=True, allow_nan=False) + "\n").encode()
    if len(encoded) > MAX_EVENT_BYTES:
        raise ValueError("Evidence event exceeds 64 KiB; not saved")
    path = evidence_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    async with _write_lock:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError("Unexpected evidence file owner or type")
            if info.st_size + len(encoded) > MAX_LOG_BYTES:
                raise ValueError("Evidence log is full; export and remove it before recording more")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "ab", closefd=False) as handle:
                handle.write(encoded)
                handle.flush()
        finally:
            os.close(fd)
    return str(path)


async def query_command(argv):
    """Only called with fixed read-only commands, never frontend-supplied argv."""
    env = os.environ.copy()
    # Decky's bundled OpenSSL must not be injected into OS utilities. Do not
    # manufacture a user-session bus address or join a different session.
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        return {"argv": argv, "error": str(exc)}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        return {"argv": argv, "error": "timeout"}
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        raise
    return {
        "argv": argv, "exit_code": proc.returncode,
        "stdout": out[:8192].decode(errors="replace"),
        "stderr": err[:8192].decode(errors="replace"),
    }


async def capture_backend():
    identity = {
        "pid": os.getpid(), "uid": os.getuid(), "euid": os.geteuid(),
        "gid": os.getgid(), "egid": os.getegid(), "groups": os.getgroups(),
        "cgroup": read_text("/proc/self/cgroup"),
        "boot_id": read_text("/proc/sys/kernel/random/boot_id"),
        "session_environment": {name: os.environ.get(name) for name in (
            "XDG_RUNTIME_DIR", "XDG_SESSION_ID", "DBUS_SESSION_BUS_ADDRESS", "DBUS_SYSTEM_BUS_ADDRESS"
        )},
        "kernel": platform.release(), "python": platform.python_version(),
        "decky_version": getattr(decky, "DECKY_VERSION", "unknown"),
        "os_release": read_text("/etc/os-release"),
        "evidence_path": str(evidence_path()),
    }
    await record("backend.identity", identity)
    decky.logger.info("Spike identity uid=%s groups=%s cgroup=%s evidence=%s",
                      identity["euid"], identity["groups"], identity["cgroup"], evidence_path())
    if os.geteuid() == 0:
        await record("backend.refused", {"reason": "unexpected_root; install with empty flags"})
        return {"evidence_path": str(evidence_path()), "error": "unexpected_root"}
    answers = {}
    for name in POWER_QUERIES:
        result = await query_command([
            "/usr/bin/busctl", "--system", "--timeout=3s", "--no-pager", "call",
            "org.freedesktop.login1", "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager", name,
        ])
        answers[name] = result
        await record("backend.logind", {"method": name, **result})
        decky.logger.info("Spike %s: %s", name, result)
    for nic in sorted(Path("/sys/class/net").iterdir()):
        if nic.name == "lo":
            continue
        info = {"interface": nic.name, "mac": read_text(nic / "address"),
                "operstate": read_text(nic / "operstate")}
        await record("backend.nic", info)
        if not (nic / "wireless").exists() and (nic / "device").exists():
            ethtool = await query_command(["/usr/bin/ethtool", nic.name])
            await record("backend.ethtool", ethtool)
            await record("backend.wol", {**info, **parse_wol(ethtool), "checked": True})
        else:
            await record("backend.wol", {
                **info,
                "checked": False,
                "reason": "wireless or virtual interface; wired ethtool probe skipped",
            })
    return {"evidence_path": str(evidence_path()), "logind": answers}


class Plugin:
    async def _main(self):
        try:
            await capture_backend()
        except Exception:
            decky.logger.exception("Spike initial capture failed")

    async def capture(self):
        return await capture_backend()

    async def record_frontend(self, event):
        if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
            raise ValueError("Invalid frontend event")
        if not event["kind"].startswith("frontend.") or len(event["kind"]) > 96:
            raise ValueError("Invalid frontend event kind")
        return await record(event["kind"], event.get("data"))

    async def _unload(self):
        decky.logger.info("Spike backend unloading; no services or network listeners to stop")
