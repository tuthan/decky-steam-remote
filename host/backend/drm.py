"""Read-only Linux DRM connector inventory for the local display view."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable


_CONNECTOR_ENTRY_RE = re.compile(r"^card(?P<card>[0-9]+)-(?P<connector>[A-Za-z][A-Za-z0-9_.-]*)$")
_MODE_RE = re.compile(r"^(?P<width>[1-9][0-9]{0,4})x(?P<height>[1-9][0-9]{0,4})$")
# Display-order responses support up to 16 physical outputs. Keep the DRM
# inventory bound aligned so multi-port GPUs do not hide an internal panel
# behind a lexicographically later connector such as eDP-1.
_MAX_CONNECTORS = 16
_MAX_MODES = 256
_MAX_EDID_BYTES = 32 * 1024
_EDID_HEADER = b"\x00\xff\xff\xff\xff\xff\xff\x00"
_DISCONNECT_HOLD_SECONDS = 3.0


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None


def _read_edid_identity(path: Path) -> dict[str, str | int | None]:
    """Return non-sensitive display identity fields from a base EDID block."""
    try:
        data = path.read_bytes()
    except OSError:
        data = b""
    if len(data) < 128 or len(data) > _MAX_EDID_BYTES or data[:8] != _EDID_HEADER:
        return {"display_name": None, "monitor_vendor": None, "monitor_product_id": None}

    manufacturer_bits = int.from_bytes(data[8:10], "big")
    manufacturer_values = (
        (manufacturer_bits >> 10) & 0x1F,
        (manufacturer_bits >> 5) & 0x1F,
        manufacturer_bits & 0x1F,
    )
    vendor = None
    if all(1 <= value <= 26 for value in manufacturer_values):
        vendor = "".join(chr(64 + value) for value in manufacturer_values)

    display_name = None
    for offset in (54, 72, 90, 108):
        descriptor = data[offset:offset + 18]
        if len(descriptor) != 18 or descriptor[:3] != b"\x00\x00\x00" or descriptor[3] != 0xFC:
            continue
        candidate = descriptor[5:18].split(b"\n", 1)[0].split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
        if candidate and all(32 <= ord(character) <= 126 for character in candidate):
            display_name = candidate[:64]
            break

    return {
        "display_name": display_name,
        "monitor_vendor": vendor,
        "monitor_product_id": int.from_bytes(data[10:12], "little"),
    }


def _mode_list(directory: Path, output_key: str) -> list[dict[str, Any]]:
    value = _read_text(directory / "modes") or ""
    modes: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for line in value.splitlines()[:_MAX_MODES]:
        match = _MODE_RE.fullmatch(line.strip())
        if not match:
            continue
        width = int(match.group("width"))
        height = int(match.group("height"))
        if width > 16384 or height > 16384 or (width, height) in seen:
            continue
        seen.add((width, height))
        modes.append({
            "id": f"{output_key}:mode:{width}x{height}",
            "width": width,
            "height": height,
            # The DRM sysfs modes attribute does not include refresh rates.
            "refresh_hz": None,
        })
    return modes


def _is_internal_connector(connector: str) -> bool:
    return connector.lower().startswith(("edp-", "lvds-", "dsi-"))


def _read_outputs(root: Path) -> list[dict[str, Any]]:
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return []

    outputs: list[dict[str, Any]] = []
    for entry in entries:
        match = _CONNECTOR_ENTRY_RE.fullmatch(entry.name)
        if not match or match.group("connector").lower().startswith("writeback-"):
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        status = _read_text(entry / "status")
        if status not in {"connected", "disconnected"}:
            # Do not turn an unreadable/unknown connector into a false
            # disconnected result. The next refresh can include it once the
            # kernel publishes a stable status.
            continue

        card = f"card{match.group('card')}"
        connector = match.group("connector")
        output_key = f"drm:{card}:{connector}"
        identity = _read_edid_identity(entry / "edid") if status == "connected" else {
            "display_name": None,
            "monitor_vendor": None,
            "monitor_product_id": None,
        }
        display_name = identity["display_name"] or connector
        identity_details = []
        if identity["monitor_vendor"]:
            identity_details.append(str(identity["monitor_vendor"]))
        if identity["monitor_product_id"] is not None:
            identity_details.append(f"model {identity['monitor_product_id']}")
        description = " · ".join(identity_details) if identity_details else f"Linux DRM connector on {card}"
        outputs.append({
            "id": output_key,
            "output_key": output_key,
            "name": connector,
            "display_name": display_name,
            "description": description,
            "monitor_vendor": identity["monitor_vendor"],
            "monitor_product_id": identity["monitor_product_id"],
            "is_internal": _is_internal_connector(connector),
            "connector": connector,
            # The DRM card is the stable GPU-side identity available without
            # exposing EDID or hardware serial data.
            "gpu_id": f"drm:{card}",
            "connected": status == "connected",
            # DRM status is physical connection state, not Gamescope scanout.
            # Never infer active output from enabled/mode/order here.
            "active": None,
            "identity_confidence": "medium",
            "can_switch_live": False,
            "can_set_startup_preference": False,
            "restart_required": False,
            "recovery_available": False,
            "current_mode_id": None,
            "modes": _mode_list(entry, output_key),
            "generation": 0,
            "rgb_range": 0,
        })
    return outputs[:_MAX_CONNECTORS]


class DrmInventory:
    """Maintain a monotonic topology generation for a DRM inventory.

    USB-C DisplayPort hotplug can briefly remove a connector directory (or
    publish an unreadable status) while the kernel is renegotiating the link.
    Keep the last stable topology during that short loss window so callers do
    not show a monitor disappearing and reappearing. Persistent disconnects
    are still committed after the hold expires.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] = "/sys/class/drm",
        *,
        clock: Callable[[], float] = time.monotonic,
        disconnect_hold_seconds: float = _DISCONNECT_HOLD_SECONDS,
    ):
        self.root = Path(root)
        self._clock = clock
        self._disconnect_hold_seconds = max(0.0, float(disconnect_hold_seconds))
        self._lock = threading.RLock()
        self._signature: tuple[Any, ...] | None = None
        self._stable_outputs: list[dict[str, Any]] | None = None
        self._pending_signature: tuple[Any, ...] | None = None
        self._pending_outputs: list[dict[str, Any]] | None = None
        self._pending_since: float | None = None
        self._generation = 0

    @staticmethod
    def _output_signature(outputs: list[dict[str, Any]]) -> tuple[Any, ...]:
        return tuple(
            (
                output["output_key"],
                output["connector"],
                output["gpu_id"],
                output["connected"],
                output["display_name"],
                output["monitor_vendor"],
                output["monitor_product_id"],
                tuple((mode["width"], mode["height"]) for mode in output["modes"]),
            )
            for output in outputs
        )

    @staticmethod
    def _has_connected_loss(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> bool:
        current_by_key = {output.get("output_key"): output for output in current}
        return any(
            current_by_key.get(output.get("output_key"), {}).get("connected") is not True
            for output in previous
            if output.get("connected") is True
        )

    def _commit(self, outputs: list[dict[str, Any]], signature: tuple[Any, ...]) -> None:
        self._signature = signature
        self._stable_outputs = [{**output, "modes": list(output.get("modes", []))} for output in outputs]
        self._pending_signature = None
        self._pending_outputs = None
        self._pending_since = None
        self._generation += 1

    def snapshot(self) -> dict[str, Any] | None:
        outputs = _read_outputs(self.root)
        signature = self._output_signature(outputs)
        with self._lock:
            if self._stable_outputs is None:
                if not outputs:
                    return None
                self._commit(outputs, signature)
            elif signature == self._signature:
                # A hotplug candidate that returns to the stable topology
                # before the hold expires must not age into a later loss.
                self._pending_signature = None
                self._pending_outputs = None
                self._pending_since = None
            elif signature != self._signature:
                now = self._clock()
                if signature != self._pending_signature:
                    self._pending_signature = signature
                    self._pending_outputs = outputs
                    self._pending_since = now
                pending_outputs = self._pending_outputs or []
                pending_since = self._pending_since if self._pending_since is not None else now
                disconnecting = self._has_connected_loss(self._stable_outputs, pending_outputs)
                settled = now - pending_since >= self._disconnect_hold_seconds
                if not disconnecting or settled:
                    self._commit(pending_outputs, signature)
            if not self._stable_outputs:
                return None
            generation = self._generation
            snapshot_outputs = [{**output, "generation": generation} for output in self._stable_outputs]
        return {
            "ready": True,
            "reason": "",
            "methods": {
                "suspend": False,
                "restart": False,
                "shutdown": False,
                "display": True,
                "display_selection": False,
            },
            "outputs": snapshot_outputs,
            "connected_count": sum(1 for output in snapshot_outputs if output["connected"] is True),
            "generation": generation,
            "active_output_key": None,
            "selection": {
                "active_output_key": None,
                "can_switch_live": False,
                "can_set_startup_preference": False,
                "restart_required": False,
                "recovery_available": False,
                "reason": "Physical DRM connectors are detected read-only; Gaming Mode active-screen selection is not verified.",
                "adapter": "drm-sysfs-read-only",
            },
            "source": "linux-drm-sysfs",
            "reported_at": None,
        }
