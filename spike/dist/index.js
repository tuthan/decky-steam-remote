/* Disposable legacy-format Decky plugin, deliberately requiring no build step.
 * It observes Steam calls and offers explicit, bounded display and suspend tests.
 * Nothing is invoked on startup; power actions require an explicit button click.
 * Remove after collecting evidence. Production should use the public modern API.
 */
(() => (serverAPI) => {
  const tag = "[SteamOS Remote Spike]";
  const restores = [];
  const queue = [];
  let stopped = false;
  let sending = false;
  let dropped = 0;
  let callNumber = 0;
  let subscription;
  let lastSnapshot = 0;
  let lastSave = "waiting";
  let actionStatus = "waiting";
  let notify = () => {};
  let latestState = null;
  let selectedDisplayId = null;
  let selectedModeId = null;
  let transaction = null;
  let transactionNumber = 0;
  let powerAction = null;
  const MAX_PROTO_BYTES = 64 * 1024;
  const READBACK_DELAY_MS = 1000;
  const AUTO_RESTORE_DELAY_MS = 15000;
  const dm = window.SteamClient?.System?.DisplayManager;
  const system = window.SteamClient?.System;

  const timeout = (promise, ms) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("backend timeout")), ms);
    Promise.resolve(promise).then(
      value => { clearTimeout(timer); resolve(value); },
      error => { clearTimeout(timer); reject(error); }
    );
  });

  function serialize(value, depth = 0, seen = new WeakSet()) {
    if (value === undefined) return {type: "undefined"};
    if (value === null || typeof value === "boolean") return value;
    if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
    if (typeof value === "bigint") return {type: "bigint", value: String(value)};
    if (typeof value === "string") return value.length <= 24000 ? value : {type: "truncated-string", prefix: value.slice(0, 24000), length: value.length};
    if (typeof value === "function") return {type: "function"};
    if (depth > 6) return {type: "depth-limit"};
    if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
      const bytes = value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
      let binary = "";
      for (const byte of bytes.subarray(0, 16000)) binary += String.fromCharCode(byte);
      return {type: "bytes", length: bytes.length, truncated: bytes.length > 16000, base64: btoa(binary)};
    }
    if (seen.has(value)) return {type: "cycle"};
    seen.add(value);
    if (Array.isArray(value)) return value.slice(0, 64).map(item => serialize(item, depth + 1, seen));
    const out = {};
    for (const key of Object.keys(value).slice(0, 64)) {
      try { out[key] = serialize(value[key], depth + 1, seen); }
      catch (error) { out[key] = {error: String(error)}; }
    }
    return out;
  }

  function concatBytes(...parts) {
    const length = parts.reduce((total, part) => total + part.length, 0);
    const result = new Uint8Array(length);
    let offset = 0;
    for (const part of parts) {
      result.set(part, offset);
      offset += part.length;
    }
    return result;
  }

  function readVarint(bytes, start) {
    let value = 0;
    let multiplier = 1;
    let offset = start;
    for (let count = 0; count < 10; count++) {
      if (offset >= bytes.length) throw new Error("truncated protobuf varint");
      const byte = bytes[offset++];
      value += (byte & 0x7f) * multiplier;
      if (!Number.isSafeInteger(value)) throw new Error("protobuf varint exceeds safe integer range");
      if ((byte & 0x80) === 0) return {value, offset};
      multiplier *= 128;
    }
    throw new Error("protobuf varint is too long");
  }

  function readMessage(value) {
    const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
    if (bytes.length > MAX_PROTO_BYTES) throw new Error("protobuf payload exceeds 64 KiB");
    const fields = [];
    let offset = 0;
    while (offset < bytes.length) {
      const key = readVarint(bytes, offset);
      offset = key.offset;
      const number = Math.floor(key.value / 8);
      const wire = key.value % 8;
      if (!Number.isSafeInteger(number) || number < 1) throw new Error("invalid protobuf field number");
      if (wire === 0) {
        const result = readVarint(bytes, offset);
        offset = result.offset;
        fields.push({number, wire, value: result.value});
      } else if (wire === 1) {
        if (offset + 8 > bytes.length) throw new Error("truncated protobuf fixed64 field");
        offset += 8;
      } else if (wire === 2) {
        const length = readVarint(bytes, offset);
        offset = length.offset;
        if (length.value > MAX_PROTO_BYTES || offset + length.value > bytes.length) throw new Error("truncated protobuf bytes field");
        fields.push({number, wire, bytes: bytes.slice(offset, offset + length.value)});
        offset += length.value;
      } else if (wire === 5) {
        if (offset + 4 > bytes.length) throw new Error("truncated protobuf fixed32 field");
        offset += 4;
      } else {
        throw new Error(`unsupported protobuf wire type ${wire}`);
      }
    }
    return fields;
  }

  function firstVarint(fields, number, fallback = null) {
    const field = fields.find(candidate => candidate.number === number && candidate.wire === 0);
    return field ? field.value : fallback;
  }

  function allBytes(fields, number) {
    return fields.filter(candidate => candidate.number === number && candidate.wire === 2).map(candidate => candidate.bytes);
  }

  function firstBytes(fields, number) {
    return allBytes(fields, number)[0] || null;
  }

  function decodeUtf8(bytes) {
    if (!bytes) return null;
    if (typeof TextDecoder === "function") return new TextDecoder().decode(bytes);
    let value = "";
    for (const byte of bytes) value += String.fromCharCode(byte);
    return value;
  }

  function base64Bytes(value) {
    if (typeof atob !== "function") throw new Error("base64 decoder unavailable");
    const binary = atob(value);
    if (binary.length > MAX_PROTO_BYTES) throw new Error("base64 payload exceeds 64 KiB");
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
    return bytes;
  }

  function bytesFromValue(value) {
    if (value instanceof ArrayBuffer) return new Uint8Array(value);
    if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    if (value && typeof value === "object" && value.type === "bytes" && typeof value.base64 === "string") return base64Bytes(value.base64);
    if (typeof value === "string") return base64Bytes(value);
    throw new Error("GetState returned neither bytes nor base64");
  }

  function base64Value(bytes) {
    if (typeof btoa !== "function") throw new Error("base64 encoder unavailable");
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  }

  function encodeVarint(value) {
    if (!Number.isSafeInteger(value) || value < 0) throw new Error("protobuf integer must be a non-negative safe integer");
    const bytes = [];
    do {
      let byte = value % 128;
      value = Math.floor(value / 128);
      if (value) byte |= 0x80;
      bytes.push(byte);
    } while (value);
    return Uint8Array.from(bytes);
  }

  function encodeVarintField(number, value) {
    return concatBytes(encodeVarint(number * 8), encodeVarint(value));
  }

  function encodeSetMode(displayId, modeId, rgbRange = 0) {
    // Steam's native Display settings call includes the explicit Automatic
    // value (field 3 = 0); preserve field presence rather than relying on the
    // protobuf default, because the host behavior is the compatibility oracle.
    const range = rgbRange === 1 || rgbRange === 2 ? rgbRange : 0;
    const fields = [
      encodeVarintField(1, displayId),
      encodeVarintField(2, modeId),
      encodeVarintField(3, range),
    ];
    return base64Value(concatBytes(...fields));
  }

  function parseMode(bytes) {
    const fields = readMessage(bytes);
    return {
      id: firstVarint(fields, 1),
      width: firstVarint(fields, 2),
      height: firstVarint(fields, 3),
      refresh_hz: firstVarint(fields, 4),
    };
  }

  function parseDisplay(bytes) {
    const fields = readMessage(bytes);
    return {
      id: firstVarint(fields, 1),
      name: decodeUtf8(firstBytes(fields, 2)),
      description: decodeUtf8(firstBytes(fields, 3)),
      is_primary: firstVarint(fields, 4),
      is_enabled: firstVarint(fields, 5),
      is_internal: firstVarint(fields, 6),
      has_mode_override: firstVarint(fields, 7),
      width_mm: firstVarint(fields, 8),
      height_mm: firstVarint(fields, 9),
      current_mode_id: firstVarint(fields, 10),
      modes: allBytes(fields, 11).map(parseMode),
      refresh_rate_min: firstVarint(fields, 12),
      refresh_rate_max: firstVarint(fields, 13),
      is_vrr_capable: firstVarint(fields, 14),
      is_vrr_output_active: firstVarint(fields, 15),
      is_hdr_capable: firstVarint(fields, 16),
      is_hdr_output_active: firstVarint(fields, 17),
      supported_refresh_rates: fields.filter(field => field.number === 18 && field.wire === 0).map(field => field.value),
      rgb_range: firstVarint(fields, 19),
    };
  }

  function unwrapStateReply(value) {
    if (value && typeof value === "object" && Object.prototype.hasOwnProperty.call(value, "reply")) {
      return {
        payload: value.reply,
        envelope: {
          result: value.result ?? null,
          message: typeof value.message === "string" ? value.message : null,
        },
      };
    }
    return {payload: value, envelope: null};
  }

  function decodeDisplayState(value) {
    const reply = unwrapStateReply(value);
    const fields = readMessage(bytesFromValue(reply.payload));
    return {
      displays: allBytes(fields, 1).map(parseDisplay),
      is_mode_switching_supported: firstVarint(fields, 2),
      compatibility_mode: firstVarint(fields, 3),
      game_resolution_override_native: firstBytes(fields, 4) ? parseResolution(firstBytes(fields, 4)) : null,
      game_resolution_override_default: firstBytes(fields, 5) ? parseResolution(firstBytes(fields, 5)) : null,
      response: reply.envelope,
    };
  }

  function parseResolution(bytes) {
    const fields = readMessage(bytes);
    return {width: firstVarint(fields, 1), height: firstVarint(fields, 2)};
  }

  function selectedDisplay(state) {
    return state?.displays?.find(display => display.id === selectedDisplayId)
      || state?.displays?.find(display => display.is_primary === 1)
      || state?.displays?.[0]
      || null;
  }

  function syncSelection(state) {
    const display = selectedDisplay(state);
    selectedDisplayId = display?.id ?? null;
    if (!display) {
      selectedModeId = null;
      return;
    }
    if (!display.modes.some(mode => mode.id === selectedModeId)) {
      selectedModeId = display.current_mode_id ?? display.modes[0]?.id ?? null;
    }
  }

  function updateState(raw, reason) {
    const state = decodeDisplayState(raw);
    latestState = state;
    syncSelection(state);
    emit("state_decoded", {reason, state});
    notify();
    return state;
  }

  async function readState(reason) {
    if (stopped || !dm || typeof dm.GetState !== "function") throw new Error("DisplayManager.GetState unavailable");
    let result;
    try {
      result = Reflect.apply(dm.GetState, dm, []);
    } catch (error) {
      throw error;
    }
    return updateState(await timeout(result, 5000), reason);
  }

  function modeLabel(mode) {
    if (!mode) return "unknown mode";
    const refresh = mode.refresh_hz == null ? "?" : `${mode.refresh_hz} Hz`;
    return `${mode.id}: ${mode.width} × ${mode.height} @ ${refresh}`;
  }

  function modeProfile(mode) {
    if (!mode) return null;
    return {
      id: mode.id ?? null,
      width: mode.width ?? null,
      height: mode.height ?? null,
      refresh_hz: mode.refresh_hz ?? null,
    };
  }

  function sameModeProfile(expected, actual) {
    return Boolean(expected && actual
      && expected.width === actual.width
      && expected.height === actual.height
      && expected.refresh_hz === actual.refresh_hz);
  }

  function resolveRestoreMode(display, baseline) {
    const byId = display.modes.find(candidate => candidate.id === baseline.id) || null;
    if (byId && sameModeProfile(byId, baseline)) {
      return {mode: byId, matched_by: "id"};
    }

    // Steam can replace a currently active mode's host-generated ID after a
    // mode switch. Resolve by stable mode properties only when the result is
    // unambiguous; never guess between multiple refresh-rate candidates.
    const sameResolution = display.modes.filter(candidate =>
      candidate.width === baseline.width && candidate.height === baseline.height);
    const sameProfile = sameResolution.filter(candidate => candidate.refresh_hz === baseline.refresh_hz);
    if (sameProfile.length === 1) {
      return {mode: sameProfile[0], matched_by: "mode_properties"};
    }
    if (sameProfile.length > 1) {
      throw new Error("original mode replacement is ambiguous; restore not sent");
    }
    if (sameResolution.length === 1) {
      return {mode: sameResolution[0], matched_by: "resolution_fallback"};
    }
    if (sameResolution.length > 1) {
      throw new Error("original mode ID disappeared and replacement is ambiguous; restore not sent");
    }
    throw new Error("original mode and stable resolution are no longer advertised; restore not sent");
  }

  function displayIdentity(display) {
    return {id: display.id, name: display.name, description: display.description, is_internal: display.is_internal};
  }

  function sameDisplayIdentity(expected, actual) {
    if (!actual || actual.id !== expected.id) return false;
    for (const key of ["name", "description", "is_internal"]) {
      if (expected[key] !== null && expected[key] !== undefined && actual[key] !== expected[key]) return false;
    }
    return true;
  }

  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

  async function invokeSetMode(tx, phase, display, mode) {
    const payload = encodeSetMode(display.id, mode.id, display.rgb_range ?? 0);
    const request = {
      transaction_id: tx.id,
      phase,
      display_id: display.id,
      mode_id: mode.id,
      mode: modeLabel(mode),
      payload_base64: payload,
    };
    emit("mode_request", request);
    let result;
    try {
      result = await timeout(Reflect.apply(dm.SetMode, dm, [payload]), 5000);
    } catch (error) {
      emit("mode_request_failed", {...request, error: String(error)});
      throw error;
    }
    emit("mode_request_returned", {...request, result});
    return {payload, result};
  }

  async function captureReadback(tx, phase, requestedMode, expectedMode = requestedMode) {
    await delay(READBACK_DELAY_MS);
    if (stopped) return null;
    try {
      const state = await readState(`mode_test_${phase}_readback`);
      const display = state.displays.find(candidate => candidate.id === tx.display_id) || null;
      const observedModeId = display?.current_mode_id ?? null;
      const observedMode = display?.modes.find(candidate => candidate.id === observedModeId) || null;
      const matchedById = observedModeId === expectedMode.id;
      const matchedByProfile = sameModeProfile(expectedMode, observedMode);
      const result = {
        transaction_id: tx.id,
        phase,
        display_id: tx.display_id,
        requested_mode_id: requestedMode.id,
        expected_mode_id: expectedMode.id,
        observed_mode_id: observedModeId,
        requested_mode: modeProfile(requestedMode),
        expected_mode: modeProfile(expectedMode),
        observed_mode: modeProfile(observedMode),
        matched_by: matchedById ? "id" : matchedByProfile ? "mode_properties" : null,
        matches: matchedById || matchedByProfile,
      };
      emit("mode_readback", result);
      return {...result, state, display};
    } catch (error) {
      emit("mode_readback_failed", {
        transaction_id: tx.id,
        phase,
        display_id: tx.display_id,
        requested_mode_id: requestedMode.id,
        expected_mode_id: expectedMode.id,
        error: String(error),
      });
      return null;
    }
  }

  function waitForRestore(tx) {
    if (tx.restoreRequested) return Promise.resolve("manual");
    return new Promise(resolve => {
      tx.resolveRestore = reason => {
        if (tx.restoreTimer) clearTimeout(tx.restoreTimer);
        tx.resolveRestore = null;
        resolve(reason);
      };
      tx.restoreTimer = setTimeout(() => tx.resolveRestore?.("timeout"), AUTO_RESTORE_DELAY_MS);
    });
  }

  async function restoreTransaction(tx, reason) {
    if (tx.restoreStarted || stopped) return null;
    tx.restoreStarted = true;
    actionStatus = `Restoring ${modeLabel(tx.baseline_mode)} (${reason})…`;
    notify();
    try {
      const state = await readState("mode_test_restore_preflight");
      const display = state.displays.find(candidate => candidate.id === tx.display_id) || null;
      if (!sameDisplayIdentity(tx.display_identity, display)) {
        throw new Error("display identity changed or disappeared; restore not sent");
      }
      const resolved = resolveRestoreMode(display, tx.baseline_mode);
      const mode = resolved.mode;
      emit("mode_restore_resolved", {
        transaction_id: tx.id,
        matched_by: resolved.matched_by,
        baseline_mode: modeProfile(tx.baseline_mode),
        resolved_mode: modeProfile(mode),
      });
      actionStatus = `Restoring ${modeLabel(mode)} (${reason})…`;
      notify();
      await invokeSetMode(tx, `restore_${reason}`, display, mode);
      const readback = await captureReadback(tx, "restore", mode, tx.baseline_mode);
      const status = readback?.matches
        ? "Original display mode restored and confirmed"
        : "Restore sent; readback did not confirm the original mode";
      actionStatus = status;
      emit("mode_test_finished", {
        transaction_id: tx.id,
        reason,
        restored: Boolean(readback?.matches),
        readback: readback ? {
          observed_mode_id: readback.observed_mode_id,
          matched_by: readback.matched_by,
          matches: readback.matches,
        } : null,
      });
      notify();
      return readback;
    } catch (error) {
      actionStatus = `Restore failed: ${String(error)}`;
      emit("mode_restore_failed", {transaction_id: tx.id, reason, error: String(error)});
      notify();
      throw error;
    }
  }

  async function startModeTest() {
    if (transaction || stopped || !dm || typeof dm.SetMode !== "function") return;
    const tx = {id: ++transactionNumber, restoreRequested: false, restoreStarted: false, applyStarted: false};
    transaction = tx;
    actionStatus = "Reading current display state…";
    notify();
    try {
      const state = await readState("mode_test_preflight");
      const display = selectedDisplay(state);
      const mode = display?.modes.find(candidate => candidate.id === selectedModeId) || null;
      const baselineMode = display?.modes.find(candidate => candidate.id === display.current_mode_id) || null;
      if (!display) throw new Error("no display is available");
      if (!mode) throw new Error("selected mode is no longer advertised");
      if (!baselineMode) throw new Error("current mode is not present in the advertised mode list");
      if (mode.id === baselineMode.id) throw new Error("choose a mode different from the current mode");
      tx.display_id = display.id;
      tx.display_identity = displayIdentity(display);
      tx.baseline_mode = baselineMode;
      tx.target_mode = mode;
      emit("mode_test_started", {
        transaction_id: tx.id,
        display: tx.display_identity,
        baseline_mode: baselineMode,
        target_mode: mode,
        auto_restore_after_ms: AUTO_RESTORE_DELAY_MS,
      });
      actionStatus = `Applying ${modeLabel(mode)}…`;
      notify();
      tx.applyStarted = true;
      await invokeSetMode(tx, "apply", display, mode);
      await captureReadback(tx, "apply", mode);
      if (stopped) return;
      let reason;
      if (tx.restoreRequested) {
        reason = "manual";
      } else {
        actionStatus = `Mode applied; automatic restore in ${AUTO_RESTORE_DELAY_MS / 1000} seconds`;
        notify();
        reason = await waitForRestore(tx);
      }
      if (reason !== "unload" && !stopped) await restoreTransaction(tx, reason);
    } catch (error) {
      emit("mode_test_failed", {transaction_id: tx.id, error: String(error)});
      if (tx.applyStarted && tx.baseline_mode && !tx.restoreStarted && !stopped) {
        try { await restoreTransaction(tx, "error"); } catch (_) { /* failure is recorded */ }
      }
      actionStatus = `Mode test failed: ${String(error)}`;
      notify();
    } finally {
      if (tx.restoreTimer) clearTimeout(tx.restoreTimer);
      if (transaction === tx) transaction = null;
      notify();
    }
  }

  function requestRestore() {
    if (!transaction) {
      actionStatus = "No active mode test to restore";
      notify();
      return;
    }
    transaction.restoreRequested = true;
    transaction.resolveRestore?.("manual");
    actionStatus = "Restore requested…";
    notify();
  }

  async function flushEvidence(maxMs = 750) {
    const deadline = Date.now() + maxMs;
    while (!stopped && (queue.length || sending) && Date.now() < deadline) {
      if (!sending) void drain();
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      await delay(Math.min(10, remaining));
    }
  }

  async function requestPower(method) {
    if (stopped || transaction || powerAction || typeof system?.[method] !== "function") return;
    powerAction = method;
    actionStatus = `${method} requested…`;
    notify();
    emit("power_test_started", {method});
    try {
      // Give the start record a short chance to reach the local evidence file
      // before the host suspends and pauses this plugin process.
      await flushEvidence();
      const result = Reflect.apply(system[method], system, []);
      if (result && typeof result.then === "function") await timeout(result, 5000);
      emit("power_test_finished", {method, returned: result});
      actionStatus = `${method} returned; host may suspend now`;
    } catch (error) {
      emit("power_test_failed", {method, error: String(error)});
      actionStatus = `${method} failed: ${String(error)}`;
    } finally {
      powerAction = null;
      notify();
    }
  }

  async function drain() {
    if (sending || stopped) return;
    sending = true;
    try {
      while (queue.length && !stopped) {
        const event = queue.shift();
        try {
          const reply = await timeout(serverAPI.callPluginMethod("record_frontend", {event}), 5000);
          if (reply?.success === false) throw new Error(String(reply.result));
          lastSave = "saved";
        } catch (error) {
          lastSave = "save failed; see console";
          console.warn(tag, "Evidence save failed", error);
          // Keep console evidence, but never accumulate an unbounded backlog.
        }
        notify();
      }
    } finally { sending = false; }
  }

  function emit(kind, data) {
    // Instrumentation must never prevent or change a Steam call.
    try {
      if (stopped) return;
      let event = {kind: `frontend.${kind}`, data: serialize(data)};
      if (JSON.stringify(event).length > 30000) event = {kind: `frontend.${kind}`, data: {error: "serialized capture exceeds 30K characters"}};
      console.info(tag, event);
      if (queue.length >= 32) { dropped++; return; }
      queue.push(event);
      void drain();
    } catch (error) { console.warn(tag, "Observer failed", error); }
  }

  function observe(object, name) {
    if (!object || typeof object[name] !== "function") {
      emit("method_missing", {name}); return;
    }
    const original = object[name];
    const ownDescriptor = Object.getOwnPropertyDescriptor(object, name);
    const wrapped = function (...args) {
      const callId = ++callNumber;
      emit("call", {call_id: callId, method: name, args});
      try {
        const result = Reflect.apply(original, this, args);
        // Return exactly the original value/promise and preserve thrown errors.
        try {
          if (result && typeof result.then === "function") {
            result.then(
              value => emit("result", {call_id: callId, method: name, value}),
              error => emit("rejected", {call_id: callId, method: name, error: String(error)})
            );
          } else emit("result", {call_id: callId, method: name, value: result});
        } catch (observerError) { emit("observer_error", {method: name, error: String(observerError)}); }
        return result;
      } catch (error) {
        emit("threw", {call_id: callId, method: name, error: String(error)});
        throw error;
      }
    };
    try {
      object[name] = wrapped;
      if (object[name] !== wrapped) throw new Error("method is not writable");
      restores.push(() => {
        if (object[name] !== wrapped) return; // Never overwrite a later plugin patch.
        if (ownDescriptor) Object.defineProperty(object, name, ownDescriptor);
        else delete object[name];
      });
      emit("observing", {name});
    } catch (error) { emit("hook_failed", {name, error: String(error)}); }
  }

  function snapshot(reason = "snapshot") {
    if (stopped || !dm || typeof dm.GetState !== "function") return;
    lastSnapshot = Date.now();
    void readState(reason).catch(error => emit("snapshot_failed", {reason, error: String(error)}));
  }

  emit("loaded", {
    power_methods: ["SuspendPC", "RestartPC", "ShutdownPC"].filter(name => typeof system?.[name] === "function"),
    display_methods: ["GetState", "SetMode", "ClearModeOverride", "SetCompatibilityMode", "RegisterForStateChanges", "SetGamescopeInternalResolution"].filter(name => typeof dm?.[name] === "function"),
    note: "power methods are observed; SuspendPC has an explicit test button; mode tests require an explicit button press and auto-restore",
  });
  ["GetState", "SetMode", "ClearModeOverride", "SetCompatibilityMode"].forEach(name => observe(dm, name));
  ["SuspendPC", "RestartPC", "ShutdownPC"].forEach(name => observe(system, name));
  if (typeof dm?.RegisterForStateChanges === "function") {
    try {
      subscription = dm.RegisterForStateChanges((...args) => {
        emit("display_state_changed", {args});
        if (Date.now() - lastSnapshot > 1000) snapshot();
      });
    } catch (error) { emit("subscription_failed", {error: String(error)}); }
  }
  snapshot();

  const React = window.SP_REACT;
  function Content() {
    const [, refresh] = React.useState(0);
    React.useEffect(() => {
      notify = () => refresh(value => value + 1);
      return () => { notify = () => {}; };
    }, []);
    const display = selectedDisplay(latestState);
    const selectedMode = display?.modes.find(mode => mode.id === selectedModeId) || null;
    const canTest = Boolean(display && selectedMode && display.current_mode_id !== selectedMode.id && !transaction && !powerAction);
    const canSuspend = Boolean(typeof system?.SuspendPC === "function" && !transaction && !powerAction);
    const stateText = latestState
      ? `${latestState.displays.length} display(s); ${display ? `current ${display.current_mode_id}` : "no selected display"}`
      : "not captured yet";
    return React.createElement("div", {style: {padding: "12px", lineHeight: "1.5"}},
      React.createElement("p", null, "Local evidence recorder. Power tests require an explicit click; Suspend sends immediately. Mode tests restore automatically."),
      React.createElement("p", null, `Recorder: ${lastSave}; dropped events: ${dropped}`),
      React.createElement("p", null, `State: ${stateText}; ${actionStatus}`),
      React.createElement("button", {onClick: () => snapshot("manual"), disabled: Boolean(transaction || powerAction)}, "Capture display state"),
      React.createElement("label", {style: {display: "block", marginTop: "8px"}},
        "Display: ",
        React.createElement("select", {
          value: selectedDisplayId ?? "",
          disabled: Boolean(transaction || powerAction) || !latestState?.displays.length,
          onChange: event => { selectedDisplayId = Number(event.target.value); selectedModeId = null; syncSelection(latestState); notify(); },
        }, (latestState?.displays || []).map(candidate => React.createElement("option", {key: candidate.id, value: candidate.id}, `${candidate.id}: ${candidate.name || candidate.description || "display"}`)))),
      React.createElement("label", {style: {display: "block", marginTop: "8px"}},
        "Mode: ",
        React.createElement("select", {
          value: selectedModeId ?? "",
          disabled: Boolean(transaction || powerAction) || !display?.modes.length,
          onChange: event => { selectedModeId = Number(event.target.value); notify(); },
        }, (display?.modes || []).map(mode => React.createElement("option", {key: mode.id, value: mode.id}, modeLabel(mode))))),
      React.createElement("button", {onClick: () => { void startModeTest(); }, disabled: !canTest, style: {display: "block", marginTop: "8px"}}, "Test selected mode (restore in 15s)"),
      React.createElement("button", {onClick: requestRestore, disabled: !transaction, style: {display: "block", marginTop: "8px"}}, "Restore original mode now"),
      React.createElement("button", {onClick: () => { void requestPower("SuspendPC"); }, disabled: !canSuspend, style: {display: "block", marginTop: "8px"}}, "Suspend PC (test)"),
      React.createElement("p", null, "Evidence: Decky data / SteamOS Remote Spike / spike-evidence.jsonl"),
      React.createElement("p", null, "Use the Suspend PC button for a controlled power observation and a separate LAN device for WoL. Decky Sunshine owns Sunshine status/restart. Capture continues with this panel closed. Remove the probe after testing.")
    );
  }
  return {
    name: "SteamOS Remote Spike",
    icon: React ? React.createElement("span", null, "P") : null,
    content: React ? React.createElement(Content) : null,
    onDismount() {
      emit("unloading", {dropped, queued: queue.length});
      stopped = true;
      if (transaction?.restoreTimer) clearTimeout(transaction.restoreTimer);
      transaction?.resolveRestore?.("unload");
      queue.length = 0;
      notify = () => {};
      try { subscription?.unregister?.(); } catch (error) { console.warn(tag, error); }
      for (const restore of restores.reverse()) {
        try { restore(); } catch (error) { console.warn(tag, "Restore failed", error); }
      }
    },
  };
})()
