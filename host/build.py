"""Build the installable Decky host artifact without downloading dependencies."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parent.parent
HOST = ROOT / "host"
FILES = (
    "plugin.json",
    "package.json",
    "main.py",
    "backend/__init__.py",
    "backend/bridge.py",
    "backend/display.py",
    "backend/identity.py",
    "backend/operations.py",
    "backend/pairing.py",
    "backend/protocol.py",
    "backend/provider.py",
    "backend/server.py",
    "backend/storage.py",
    "backend/service.py",
    "backend/client_core.py",
    "backend/client.py",
    "backend/client_service.py",
    "backend/coordinator.py",
    "dist/index.js",
    "../protocol/README.md",
    "../protocol/schema.json",
    "../protocol/fixtures/outputs-working.json",
    "../protocol/fixtures/status-steam-unavailable.json",
    "../protocol/fixtures/status-sunshine-disabled.json",
    "../protocol/fixtures/status-sunshine-running.json",
    "../protocol/fixtures/status-sunshine-stopped.json",
    "../protocol/fixtures/status-sunshine-unknown.json",
)


def build() -> Path:
    source = HOST / "frontend/index.js"
    target_js = HOST / "dist/index.js"
    target_js.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target_js)
    package = json.loads((HOST / "package.json").read_text())
    version = package["version"]
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    archive_path = output / f"steamos-remote-decky-{version}.zip"
    with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
        for relative in FILES:
            path = (HOST / relative).resolve()
            archive_name = relative[3:] if relative.startswith("../") else relative
            info = ZipInfo(f"steamos-remote/{archive_name}", date_time=(2026, 9, 13, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_path.with_suffix(".zip.sha256").write_text(f"{digest}  {archive_path.name}\n")
    print(f"{archive_path}\nSHA256 {digest}")
    return archive_path


if __name__ == "__main__":
    build()
