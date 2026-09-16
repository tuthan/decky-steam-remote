"""Pure display profile helpers copied from the validated spike behavior."""

from __future__ import annotations

import re
from typing import Any


class DisplayError(ValueError):
    pass


IDENTITY_CONFIDENCES = frozenset({"unknown", "low", "medium", "high", "ambiguous"})
_CONNECTOR_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_OPAQUE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:|/-]{0,127}$")


def _opaque_key(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_KEY_RE.fullmatch(value):
        raise DisplayError(f"{field} is invalid")
    return value


def _optional_text(value: Any, field: str, limit: int = 256) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise DisplayError(f"{field} is invalid")
    return value


def _optional_connector(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _CONNECTOR_RE.fullmatch(value):
        raise DisplayError("display connector is invalid")
    return value


def _optional_bool(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise DisplayError(f"{field} is invalid")
    return value


def _mode_id(value: Any) -> str:
    if isinstance(value, int) and value >= 0:
        return str(value)
    if isinstance(value, str) and 0 < len(value) <= 128:
        return value
    raise DisplayError("display mode ID is invalid")


def normalize_mode(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DisplayError("display mode is not an object")
    mode = {
        "id": _mode_id(value.get("id")),
        "width": value.get("width"),
        "height": value.get("height"),
        "refresh_hz": value.get("refresh_hz"),
    }
    if not isinstance(mode["width"], int) or not 1 <= mode["width"] <= 16384:
        raise DisplayError("display mode width is invalid")
    if not isinstance(mode["height"], int) or not 1 <= mode["height"] <= 16384:
        raise DisplayError("display mode height is invalid")
    refresh = mode["refresh_hz"]
    if refresh is not None and (not isinstance(refresh, (int, float)) or isinstance(refresh, bool) or not 1 <= refresh <= 1000):
        raise DisplayError("display mode refresh is invalid")
    return mode


def normalize_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DisplayError("display output is not an object")
    output_id = _mode_id(value.get("id"))
    name = value.get("name")
    description = value.get("description")
    if name is not None and (not isinstance(name, str) or len(name) > 256):
        raise DisplayError("display name is invalid")
    if description is not None and (not isinstance(description, str) or len(description) > 256):
        raise DisplayError("display description is invalid")
    modes_raw = value.get("modes", [])
    if not isinstance(modes_raw, list) or len(modes_raw) > 256:
        raise DisplayError("display mode list is invalid")
    modes = [normalize_mode(mode) for mode in modes_raw]
    mode_ids = [mode["id"] for mode in modes]
    if len(set(mode_ids)) != len(mode_ids):
        raise DisplayError("display mode IDs are not unique")
    current = value.get("current_mode_id")
    if current is not None:
        current = _mode_id(current)
    generation = value.get("generation")
    if not isinstance(generation, int) or not 0 <= generation <= 2_147_483_647:
        raise DisplayError("display generation is invalid")
    rgb_range = value.get("rgb_range", 0)
    if rgb_range not in (0, 1, 2):
        raise DisplayError("display RGB range is invalid")
    is_internal = value.get("is_internal")
    if is_internal is not None and not isinstance(is_internal, bool):
        raise DisplayError("display internal flag is invalid")
    output_key = value.get("output_key", value.get("key", output_id))
    output_key = _opaque_key(output_key, "display output key")
    connector = _optional_connector(value.get("connector"))
    gpu_id = _optional_text(value.get("gpu_id"), "display GPU identity", 128)
    display_name = _optional_text(value.get("display_name"), "display name", 256)
    monitor_vendor = _optional_text(value.get("monitor_vendor"), "monitor vendor", 16)
    monitor_product_id = value.get("monitor_product_id")
    if monitor_product_id is not None and (not isinstance(monitor_product_id, int) or not 0 <= monitor_product_id <= 65535):
        raise DisplayError("monitor product ID is invalid")
    connected = value.get("connected", True)
    if not isinstance(connected, bool):
        raise DisplayError("display connected flag is invalid")
    active = _optional_bool(value.get("active"), "display active flag")
    identity_confidence = value.get("identity_confidence", "unknown")
    if not isinstance(identity_confidence, str) or identity_confidence not in IDENTITY_CONFIDENCES:
        raise DisplayError("display identity confidence is invalid")
    capabilities: dict[str, bool] = {}
    for key in ("can_switch_live", "can_set_startup_preference", "restart_required", "recovery_available"):
        candidate = value.get(key, False)
        if not isinstance(candidate, bool):
            raise DisplayError(f"display capability {key} is invalid")
        capabilities[key] = candidate
    return {
        "id": output_id,
        "output_key": output_key,
        "name": name,
        "display_name": display_name,
        "monitor_vendor": monitor_vendor,
        "monitor_product_id": monitor_product_id,
        "description": description,
        "is_internal": is_internal,
        "connector": connector,
        "gpu_id": gpu_id,
        "connected": connected,
        "active": active,
        "identity_confidence": identity_confidence,
        **capabilities,
        "current_mode_id": current,
        "modes": modes,
        "generation": generation,
        "rgb_range": rgb_range,
    }


def normalize_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DisplayError("bridge snapshot is not an object")
    outputs_raw = value.get("outputs", [])
    if not isinstance(outputs_raw, list) or len(outputs_raw) > 8:
        raise DisplayError("bridge output list is invalid")
    outputs = [normalize_output(output) for output in outputs_raw]
    output_ids = [output["id"] for output in outputs]
    if len(set(output_ids)) != len(output_ids):
        raise DisplayError("bridge output IDs are not unique")
    output_keys = [output["output_key"] for output in outputs]
    if len(set(output_keys)) != len(output_keys):
        raise DisplayError("display output keys are not unique")
    methods = value.get("methods", {})
    if not isinstance(methods, dict):
        raise DisplayError("bridge methods are invalid")
    selection_raw = value.get("selection", {})
    if not isinstance(selection_raw, dict):
        raise DisplayError("display selection state is invalid")
    explicit_active_key = value.get("active_output_key", selection_raw.get("active_output_key"))
    if explicit_active_key is not None:
        explicit_active_key = _opaque_key(explicit_active_key, "active output key")
    selection: dict[str, Any] = {"active_output_key": explicit_active_key}
    for key in ("can_switch_live", "can_set_startup_preference", "restart_required", "recovery_available"):
        candidate = selection_raw.get(key, False)
        if not isinstance(candidate, bool):
            raise DisplayError(f"display selection capability {key} is invalid")
        selection[key] = candidate
    selection_reason = selection_raw.get("reason")
    if selection_reason is not None and (not isinstance(selection_reason, str) or len(selection_reason) > 256):
        raise DisplayError("display selection reason is invalid")
    selection["reason"] = selection_reason or ""
    adapter = selection_raw.get("adapter")
    if adapter is not None and (not isinstance(adapter, str) or len(adapter) > 128):
        raise DisplayError("display selection adapter is invalid")
    selection["adapter"] = adapter
    generation = value.get("generation", max((output.get("generation", 0) for output in outputs), default=0))
    if not isinstance(generation, int) or isinstance(generation, bool) or not 0 <= generation <= 2_147_483_647:
        raise DisplayError("bridge generation is invalid")
    return {
        "ready": value.get("ready") is True,
        "reason": str(value.get("reason", ""))[:256] if value.get("reason") is not None else "",
        "methods": {
            "suspend": methods.get("suspend") is True,
            "restart": methods.get("restart") is True,
            "shutdown": methods.get("shutdown") is True,
            "display": methods.get("display") is True,
            "display_selection": methods.get("display_selection") is True,
            "preferred_monitor": methods.get("preferred_monitor") is True,
            "preferred_monitor_readback": methods.get("preferred_monitor_readback") is True,
        },
        "outputs": outputs,
        "generation": generation,
        "active_output_key": explicit_active_key,
        "selection": selection,
        "cpu_temperature": normalize_temperature(value.get("cpu_temperature")),
        "reported_at": value.get("reported_at"),
    }


def normalize_temperature(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DisplayError("CPU temperature is invalid")
    celsius = value.get("celsius")
    label = value.get("label")
    if not isinstance(celsius, (int, float)) or isinstance(celsius, bool) or not -100 <= celsius <= 250:
        raise DisplayError("CPU temperature value is invalid")
    if not isinstance(label, str) or not 0 < len(label) <= 64:
        raise DisplayError("CPU temperature label is invalid")
    return {"celsius": float(celsius), "label": label}


def display_identity(output: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": output["id"],
        "name": output.get("name"),
        "description": output.get("description"),
        "is_internal": output.get("is_internal"),
    }


def selection_identity(output: dict[str, Any]) -> dict[str, Any]:
    """Return the non-secret identity used by the local Gaming Mode adapter.

    Runtime Steam display IDs are deliberately not used as the sole identity
    here. A connector and validated GPU identity are required when available;
    an adapter may instead provide a high-confidence opaque key. EDID and
    hardware serial data never enters this structure.
    """
    return {
        "output_key": output.get("output_key"),
        "connector": output.get("connector"),
        "gpu_id": output.get("gpu_id"),
        "display_name": output.get("display_name"),
        "identity_confidence": output.get("identity_confidence", "unknown"),
    }


def same_selection_identity(expected: dict[str, Any] | None, actual: dict[str, Any] | None) -> bool:
    if not expected or not actual:
        return False
    if expected.get("identity_confidence") == "ambiguous" or actual.get("identity_confidence") == "ambiguous":
        return False
    expected_connector = expected.get("connector")
    expected_gpu = expected.get("gpu_id")
    actual_connector = actual.get("connector")
    actual_gpu = actual.get("gpu_id")
    if expected_connector is not None or expected_gpu is not None:
        return (
            expected_connector is not None
            and expected_gpu is not None
            and expected_connector == actual_connector
            and expected_gpu == actual_gpu
        )
    return (
        expected.get("output_key") is not None
        and expected.get("identity_confidence") == "high"
        and actual.get("identity_confidence") == "high"
        and expected.get("output_key") == actual.get("output_key")
    )


def resolve_active_output(
    outputs: list[dict[str, Any]], explicit_key: str | None = None,
) -> tuple[str | None, str]:
    """Resolve only explicit active-output signals.

    A current mode, output order, name, or ``connected`` flag is not evidence
    of active scanout. Conflicting signals are reported as ambiguous so a
    caller can keep mutation disabled rather than selecting the wrong screen.
    """
    connected = [output for output in outputs if output.get("connected") is True]
    explicit = [output for output in connected if output.get("active") is True]
    if len(explicit) > 1:
        return None, "ambiguous"
    if explicit_key is not None:
        keyed = [output for output in connected if output.get("output_key") == explicit_key]
        if len(keyed) != 1:
            return None, "unknown"
        if explicit and explicit[0].get("output_key") != explicit_key:
            return None, "ambiguous"
        return explicit_key, "known"
    if len(explicit) == 1:
        return explicit[0].get("output_key"), "known"
    return None, "unknown"


def same_display_identity(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    if not actual or actual.get("id") != expected.get("id"):
        return False
    for key in ("name", "description", "is_internal"):
        if expected.get(key) is not None and actual.get(key) != expected.get(key):
            return False
    return True


def same_mode_profile(expected: dict[str, Any] | None, actual: dict[str, Any] | None) -> bool:
    if not expected or not actual:
        return False
    if expected.get("width") != actual.get("width") or expected.get("height") != actual.get("height"):
        return False
    expected_refresh = expected.get("refresh_hz")
    actual_refresh = actual.get("refresh_hz")
    if expected_refresh is None or actual_refresh is None:
        return expected_refresh == actual_refresh
    try:
        # Steam commonly re-enumerates a requested 60 Hz mode as 59 Hz after
        # a mode switch. Treat only that known presentation rounding as the
        # same physical mode; a real 60/61 Hz difference remains distinct.
        expected_value = float(expected_refresh)
        actual_value = float(actual_refresh)
        return expected_value == actual_value or {expected_value, actual_value} == {59.0, 60.0}
    except (TypeError, ValueError):
        return False


def mode_profile(mode: dict[str, Any] | None) -> dict[str, Any] | None:
    if mode is None:
        return None
    return {key: mode.get(key) for key in ("id", "width", "height", "refresh_hz")}


def resolve_restore_mode(output: dict[str, Any], baseline: dict[str, Any]) -> tuple[dict[str, Any], str]:
    by_id = next((mode for mode in output.get("modes", []) if mode["id"] == baseline.get("id")), None)
    if by_id and same_mode_profile(by_id, baseline):
        return by_id, "id"
    same_resolution = [
        mode for mode in output.get("modes", [])
        if mode["width"] == baseline.get("width") and mode["height"] == baseline.get("height")
    ]
    same_profile = [mode for mode in same_resolution if same_mode_profile(mode, baseline)]
    if len(same_profile) == 1:
        return same_profile[0], "mode_properties"
    if len(same_profile) > 1:
        raise DisplayError("original mode replacement is ambiguous; restore not sent")
    if len(same_resolution) == 1:
        return same_resolution[0], "resolution_fallback"
    if len(same_resolution) > 1:
        raise DisplayError("original mode ID disappeared and replacement is ambiguous; restore not sent")
    raise DisplayError("original mode and stable resolution are no longer advertised; restore not sent")
