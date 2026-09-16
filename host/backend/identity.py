"""Persistent host identity and locally-owned TLS material."""

from __future__ import annotations

import base64
import hashlib
import math
import os
import platform
import secrets
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def opaque_id(prefix: str = "") -> str:
    value = base64.urlsafe_b64encode(secrets.token_bytes(18)).decode().rstrip("=")
    return f"{prefix}{value}"


def read_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        value = ""
    return value[:128] or opaque_id("boot-")


def system_uptime_seconds() -> int | None:
    try:
        value = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return max(0, int(value))


def read_cpu_temperature(root: str | os.PathLike[str] = "/sys/class/hwmon") -> dict[str, Any] | None:
    """Read a conservative CPU temperature from Linux hwmon sensors.

    Decky's bundled Steam frontend does not expose a stable CPU-temperature
    API. The backend can still report it without a subprocess by using the
    kernel's hwmon files and ignoring unrelated GPU, NVMe, and ACPI sensors.
    """
    root_path = Path(root)
    candidates: list[tuple[int, float, str]] = []
    try:
        devices = sorted(root_path.glob("hwmon*"))
    except OSError:
        return None
    for device in devices:
        try:
            device_name = device.name
            sensor_name = device.joinpath("name").read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            device_name = device.name
            sensor_name = ""
        source = f"{device_name} {sensor_name}".lower()
        cpu_source = any(term in source for term in ("k10temp", "coretemp", "zenpower", "cpu"))
        try:
            inputs = sorted(device.glob("temp*_input"))
        except OSError:
            continue
        for input_path in inputs:
            try:
                celsius = float(input_path.read_text(encoding="ascii").strip()) / 1000.0
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if not math.isfinite(celsius) or not -20.0 <= celsius <= 125.0:
                continue
            label_path = input_path.with_name(input_path.name.replace("_input", "_label"))
            try:
                label = label_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                label = ""
            label_lower = label.lower()
            explicit_cpu = any(term in label_lower for term in ("tdie", "tctl", "package id", "cpu", "core"))
            unrelated = any(term in f"{source} {label_lower}" for term in ("amdgpu", "gpu", "nvme", "acpitz"))
            if unrelated and not explicit_cpu:
                continue
            if not cpu_source and not explicit_cpu:
                continue
            score = 0 if explicit_cpu else 1
            candidates.append((score, celsius, label or sensor_name or "CPU"))
    if not candidates:
        return None
    _, celsius, label = sorted(candidates, key=lambda item: item[0])[0]
    return {"celsius": round(celsius, 1), "label": label[:64]}


def certificate_fingerprint(path: str | os.PathLike[str]) -> str:
    encoded = Path(path).read_bytes()
    der = ssl.PEM_cert_to_DER_cert(encoded.decode("ascii"))
    digest = hashlib.sha256(der).hexdigest()
    return f"sha256:{digest}"


def _safe_material_file(path: Path, mode: int) -> None:
    if os.path.lexists(path) and path.is_symlink():
        raise RuntimeError(f"refusing symlink TLS material: {path}")
    if path.exists() and not path.is_file():
        raise RuntimeError(f"refusing non-file TLS material: {path}")
    if path.exists():
        os.chmod(path, mode)


def _openssl_failure(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "openssl timed out after 15 seconds"
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = exc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        detail = " ".join(str(stderr or "").split())[:220]
        return f"openssl exited with status {exc.returncode}" + (f": {detail}" if detail else "")
    return str(exc)[:220]


def _openssl_environment() -> dict[str, str]:
    """Run system OpenSSL outside Decky's bundled PyInstaller libraries."""
    environment = os.environ.copy()
    for name in (
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "LD_AUDIT",
        "OPENSSL_CONF",
        "OPENSSL_MODULES",
        "OPENSSL_ENGINES",
    ):
        environment.pop(name, None)
    return environment


def ensure_tls_material(root: str | os.PathLike[str], host_id: str) -> dict[str, Any]:
    """Create a self-signed certificate once, using only fixed openssl args."""
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert = root_path / "host-cert.pem"
    key = root_path / "host-key.pem"
    _safe_material_file(cert, 0o644)
    _safe_material_file(key, 0o600)
    if not cert.exists() or not key.exists():
        openssl = shutil.which("openssl")
        if not openssl:
            return {"ready": False, "reason": "openssl is unavailable; cannot create host certificate"}
        common_name = f"steamos-companion-{host_id[:32]}"
        with tempfile.TemporaryDirectory(prefix="steamos-companion-tls-", dir=root_path) as temp_dir:
            temp = Path(temp_dir)
            temp_cert = temp / "cert.pem"
            temp_key = temp / "key.pem"
            command_base = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-batch"]
            output_args = [
                "-keyout", str(temp_key), "-out", str(temp_cert), "-days", "3650",
                "-subj", f"/CN={common_name}",
            ]
            commands = [
                command_base + output_args,
                # Do not depend on a system openssl.cnf inside Decky's sandbox.
                # -subj supplies the complete distinguished name we need.
                command_base + ["-config", "/dev/null"] + output_args,
            ]
            failures = []
            environment = _openssl_environment()
            for command in commands:
                temp_key.unlink(missing_ok=True)
                temp_cert.unlink(missing_ok=True)
                try:
                    subprocess.run(
                        command,
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=15,
                        env=environment,
                    )
                    break
                except (OSError, subprocess.SubprocessError) as exc:
                    failures.append(_openssl_failure(exc))
            else:
                detail = "; ".join(failures)
                return {"ready": False, "reason": f"certificate generation failed: {detail[:360]}"}
            os.chmod(temp_key, 0o600)
            os.chmod(temp_cert, 0o644)
            os.replace(temp_key, key)
            os.replace(temp_cert, cert)
    try:
        fingerprint = certificate_fingerprint(cert)
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(cert), str(key))
    except (OSError, ValueError, ssl.SSLError) as exc:
        return {"ready": False, "reason": f"host certificate is unusable: {str(exc)[:160]}"}
    return {
        "ready": True,
        "certificate_path": str(cert),
        "key_path": str(key),
        "fingerprint": fingerprint,
        "python": platform.python_version(),
    }
