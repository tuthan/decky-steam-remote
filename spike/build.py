"""Package only the four runtime files; no downloads or install hooks."""
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED, ZipInfo

ROOT = Path(__file__).resolve().parent
FILES = ("plugin.json", "package.json", "main.py", "dist/index.js")


def build():
    assert json.loads((ROOT / "plugin.json").read_text())["flags"] == []
    out = ROOT / "artifacts"
    out.mkdir(exist_ok=True)
    target = out / "steamos-remote-spike-0.0.6.zip"
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for name in FILES:
            info = ZipInfo(f"steamos-remote-spike/{name}", date_time=(2026, 9, 11, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (ROOT / name).read_bytes())
    digest = sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(f"{digest}  {target.name}\n")
    print(f"{target}\nSHA256 {digest}")


if __name__ == "__main__":
    build()
