/* SteamOS Remote Decky frontend.
 *
 * The backend owns the LAN API and operation journal. This module is the
 * long-lived Steam bridge: it only calls the fixed DisplayManager/System
 * methods and the two constant Decky Sunshine owner methods needed by
 * commands delivered by the backend. The bridge is kept
 * alive after the settings view closes so a remote client can still recover a
 * display. No command accepts a raw Steam method or payload from the LAN.
 */
(() => (serverAPI) => {
  const tag = "[SteamOS Remote]";
  const MAX_PROTO_BYTES = 64 * 1024;
  const MAX_STATE_BYTES = 256 * 1024;
  const commandPollMs = 500;
  const snapshotPollMs = 2000;
  const initialDisplayManager = window.SteamClient?.System?.DisplayManager;
  const initialSystem = window.SteamClient?.System;
  const React = window.SP_REACT;
  let stopped = false;
  let commandBusy = false;
  let snapshotBusy = false;
  let notify = () => {};
  let latestSnapshot = null;
  let lastSnapshotAt = 0;
  let driverTimer = null;
  let pairingWatchTimer = null;
  let pairingWatchBusy = false;
  let knownPendingPairings = null;
  let sunshineOwnerTimer = null;
  let sunshineOwnerBusy = false;
  const wakeEventNames = ["focus", "online", "pageshow"];
  const rpcLogAt = new Map();

  const POWER_METHODS = Object.freeze({
    suspend: "SuspendPC",
    restart: "RestartPC",
    shutdown: "ShutdownPC",
  });
  const SUNSHINE_OWNER_PLUGIN = "Decky Sunshine";
  const SUNSHINE_OWNER_METHODS = Object.freeze({
    // The installed Decky Sunshine plugin is a legacy Decky plugin. Keep
    // these names aligned with its public backend methods; the owner remains
    // the only process controller.
    status: "isSunshineRunning",
    start: "startSunshine",
  });
  const LOADER_API_KEY = "__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit";
  let sunshineOwnerAPI = null;

  function steamSystem() { return window.SteamClient?.System || initialSystem; }
  function displayManager() { return steamSystem()?.DisplayManager || initialDisplayManager; }
  function powerMethodName(action) { return Object.prototype.hasOwnProperty.call(POWER_METHODS, action) ? POWER_METHODS[action] : null; }

  const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  const timeout = (promise, milliseconds) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Steam bridge timeout")), milliseconds);
    Promise.resolve(promise).then(
      value => { clearTimeout(timer); resolve(value); },
      error => { clearTimeout(timer); reject(error); }
    );
  });

  function deckyBackend() {
    if (window.DeckyBackend && typeof window.DeckyBackend.call === "function") return window.DeckyBackend;
    try {
      if (typeof DeckyBackend !== "undefined" && typeof DeckyBackend.call === "function") return DeckyBackend;
    } catch (_) {}
    return null;
  }

  function sunshineLoaderAPI() {
    // Decky Sunshine 2025.10.27 is a legacy API-v0 plugin. Calling it through
    // loader/call_plugin_method makes Decky interpret the request as an
    // index-argument API call and reject it. The legacy route accepts the
    // kwargs object used by Sunshine's own frontend.
    const legacy = deckyBackend();
    if (legacy) return {kind: "legacy", backend: legacy};
    if (sunshineOwnerAPI && typeof sunshineOwnerAPI.call === "function") return {kind: "modern", api: sunshineOwnerAPI};
    const init = window[LOADER_API_KEY];
    if (init && typeof init.connect === "function") {
      try {
        const api = init.connect(1, SUNSHINE_OWNER_PLUGIN);
        if (api && typeof api.call === "function") {
          sunshineOwnerAPI = api;
          return {kind: "modern", api};
        }
      } catch (error) {
        console.warn(tag, "Decky Loader owner API unavailable", boundedString(error));
      }
    }
    return null;
  }

  async function callSunshineOwner(method) {
    const owner = sunshineLoaderAPI();
    if (!owner) throw new Error("Decky Loader owner bridge is unavailable");
    const reply = owner.kind === "legacy"
      ? await timeout(owner.backend.call("loader/call_legacy_plugin_method", SUNSHINE_OWNER_PLUGIN, method, {}), 4000)
      : await timeout(owner.api.call(method), 4000);
    if (reply?.success === false) throw new Error(boundedString(reply.result || "Decky Sunshine rejected the call"));
    return reply && Object.prototype.hasOwnProperty.call(reply, "result") ? reply.result : reply;
  }

  function ownerRunningValue(value) {
    if (typeof value === "boolean") return value;
    if (value && typeof value === "object" && typeof value.running === "boolean") return value.running;
    return null;
  }

  async function connectSunshineOwner() {
    if (stopped || sunshineOwnerBusy) return;
    sunshineOwnerBusy = true;
    try {
      const running = ownerRunningValue(await callSunshineOwner(SUNSHINE_OWNER_METHODS.status));
      if (running === null) throw new Error("Decky Sunshine returned an invalid status");
      await callBackend("report_sunshine_owner", {report: {available: true, owner: SUNSHINE_OWNER_PLUGIN}});
      console.info(tag, "connected to Decky Sunshine owner");
    } catch (error) {
      const reason = boundedString(error);
      console.warn(tag, "Decky Sunshine owner unavailable", reason);
      try { await callBackend("report_sunshine_owner", {report: {available: false, reason}}); } catch (_) {}
    } finally {
      sunshineOwnerBusy = false;
    }
  }

  function startSunshineOwnerWatcher() {
    void connectSunshineOwner();
    sunshineOwnerTimer = setInterval(() => { void connectSunshineOwner(); }, 10000);
  }

  function wakeBridge() {
    if (stopped) return;
    sunshineOwnerAPI = null;
    lastSnapshotAt = 0;
    void readSnapshot("resume", true);
    void connectSunshineOwner();
  }

  if (typeof window.addEventListener === "function") {
    for (const eventName of wakeEventNames) window.addEventListener(eventName, wakeBridge);
  }

  function boundedString(value, limit = 256) {
    return String(value ?? "").replace(/\x00/g, "").slice(0, limit);
  }

  function logBackendFailure(method, error) {
    const now = Date.now();
    const previous = rpcLogAt.get(method) || 0;
    if (now - previous < 5000) return;
    rpcLogAt.set(method, now);
    console.warn(tag, `backend RPC ${method} failed`, boundedString(error));
  }

  function serialize(value, depth = 0, seen = new WeakSet()) {
    if (value === undefined) return {type: "undefined"};
    if (value === null || typeof value === "boolean") return value;
    if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
    if (typeof value === "bigint") return {type: "bigint", value: String(value)};
    if (typeof value === "string") return value.length <= 4096 ? value : {type: "truncated-string", length: value.length};
    if (typeof value === "function") return {type: "function"};
    if (depth > 5) return {type: "depth-limit"};
    if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
      const bytes = value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
      let binary = "";
      for (const byte of bytes.subarray(0, 12000)) binary += String.fromCharCode(byte);
      return {type: "bytes", length: bytes.length, truncated: bytes.length > 12000, base64: btoa(binary)};
    }
    if (typeof value !== "object") return boundedString(value);
    if (seen.has(value)) return {type: "cycle"};
    seen.add(value);
    if (Array.isArray(value)) return value.slice(0, 48).map(item => serialize(item, depth + 1, seen));
    const result = {};
    for (const key of Object.keys(value).slice(0, 48)) {
      try { result[boundedString(key, 96)] = serialize(value[key], depth + 1, seen); }
      catch (error) { result[boundedString(key, 96)] = {error: boundedString(error)}; }
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
      if (!Number.isSafeInteger(number) || number < 1 || number > 0x1fffffff) throw new Error("invalid protobuf field number");
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

  function firstBytes(fields, number) { return allBytes(fields, number)[0] || null; }

  function decodeUtf8(bytes) {
    if (!bytes) return null;
    return typeof TextDecoder === "function" ? new TextDecoder().decode(bytes).slice(0, 256) : boundedString(String.fromCharCode(...bytes), 256);
  }

  function bytesFromValue(value) {
    if (value instanceof ArrayBuffer) return new Uint8Array(value);
    if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    if (value && typeof value === "object" && value.type === "bytes" && typeof value.base64 === "string") {
      const binary = atob(value.base64);
      if (binary.length > MAX_PROTO_BYTES) throw new Error("state payload exceeds 64 KiB");
      return Uint8Array.from(binary, character => character.charCodeAt(0));
    }
    if (typeof value === "string") {
      const binary = atob(value);
      if (binary.length > MAX_PROTO_BYTES) throw new Error("state payload exceeds 64 KiB");
      return Uint8Array.from(binary, character => character.charCodeAt(0));
    }
    throw new Error("GetState returned neither bytes nor base64");
  }

  function parseMode(bytes) {
    const fields = readMessage(bytes);
    return {
      id: String(firstVarint(fields, 1, "")),
      width: firstVarint(fields, 2),
      height: firstVarint(fields, 3),
      refresh_hz: firstVarint(fields, 4),
    };
  }

  function parseDisplay(bytes) {
    const fields = readMessage(bytes);
    return {
      id: String(firstVarint(fields, 1, "")),
      name: decodeUtf8(firstBytes(fields, 2)),
      description: decodeUtf8(firstBytes(fields, 3)),
      is_internal: firstVarint(fields, 6) === 1,
      current_mode_id: firstVarint(fields, 10) === null ? null : String(firstVarint(fields, 10)),
      modes: allBytes(fields, 11).map(parseMode).filter(mode => Number.isInteger(mode.width) && Number.isInteger(mode.height)),
      rgb_range: firstVarint(fields, 19, 0),
    };
  }

  function unwrapStateReply(value) {
    if (value && typeof value === "object" && Object.prototype.hasOwnProperty.call(value, "reply"))
      return {payload: value.reply, envelope: {result: value.result ?? null, message: typeof value.message === "string" ? value.message : null}};
    return {payload: value, envelope: null};
  }

  function decodeDisplayState(value) {
    const reply = unwrapStateReply(value);
    const fields = readMessage(bytesFromValue(reply.payload));
    return {
      displays: allBytes(fields, 1).map(parseDisplay),
      is_mode_switching_supported: firstVarint(fields, 2),
      compatibility_mode: firstVarint(fields, 3),
      response: reply.envelope,
    };
  }

  function encodeVarint(value) {
    if (!Number.isSafeInteger(value) || value < 0) throw new Error("Steam ID is invalid");
    const bytes = [];
    do {
      let byte = value % 128;
      value = Math.floor(value / 128);
      if (value) byte |= 0x80;
      bytes.push(byte);
    } while (value);
    return Uint8Array.from(bytes);
  }

  function concatBytes(...parts) {
    const result = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
    let offset = 0;
    for (const part of parts) { result.set(part, offset); offset += part.length; }
    return result;
  }

  function encodeSetMode(outputId, modeId, rgbRange = 0) {
    const display = Number(outputId);
    const mode = Number(modeId);
    if (!Number.isSafeInteger(display) || !Number.isSafeInteger(mode) || display < 0 || mode < 0) throw new Error("Steam display target is invalid");
    const range = rgbRange === 1 || rgbRange === 2 ? rgbRange : 0;
    const bytes = concatBytes(
      concatBytes(encodeVarint(8), encodeVarint(display)),
      concatBytes(encodeVarint(16), encodeVarint(mode)),
      concatBytes(encodeVarint(24), encodeVarint(range)),
    );
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  }

  function normalizedSnapshot(state, reason = "") {
    const displays = state?.displays || [];
    const currentSystem = steamSystem();
    const currentDisplayManager = displayManager();
    return {
      ready: Boolean(currentDisplayManager && typeof currentDisplayManager.GetState === "function" && typeof currentDisplayManager.SetMode === "function"),
      reason: boundedString(reason),
      methods: {
        suspend: typeof currentSystem?.SuspendPC === "function",
        restart: typeof currentSystem?.RestartPC === "function",
        shutdown: typeof currentSystem?.ShutdownPC === "function",
        display: Boolean(currentDisplayManager && typeof currentDisplayManager.GetState === "function" && typeof currentDisplayManager.SetMode === "function"),
      },
      outputs: displays.slice(0, 8).map(display => ({
        id: display.id,
        name: display.name,
        description: display.description,
        is_internal: display.is_internal,
        current_mode_id: display.current_mode_id,
        modes: display.modes.slice(0, 256),
        // Steam provides no cross-call generation in this legacy bridge. A
        // monotonic frontend generation rejects a target that was refreshed
        // after a hotplug/readback change without exposing raw Steam data.
        generation: snapshotGeneration(display),
        rgb_range: display.rgb_range === 1 || display.rgb_range === 2 ? display.rgb_range : 0,
      })),
      cpu_temperature: null,
      reported_at: new Date().toISOString(),
    };
  }

  const generations = new Map();
  function snapshotGeneration(display) {
    // A mode switch changes current_mode_id by design. Generation identifies
    // the output/mode inventory instead, so a preview remains confirmable and
    // restorable after Steam reports the switched mode.
    const signature = JSON.stringify({
      id: display.id,
      name: display.name,
      description: display.description,
      is_internal: display.is_internal,
      // Steam can assign fresh IDs while applying a mode. Stable properties
      // keep that expected re-enumeration from invalidating a live preview.
      modes: display.modes.map(mode => ({width: mode.width, height: mode.height, refresh_hz: mode.refresh_hz})),
      rgb_range: display.rgb_range,
    });
    const old = generations.get(display.id);
    if (!old || old.signature !== signature) {
      const next = {signature, generation: (old?.generation || 0) + 1};
      generations.set(display.id, next);
      return next.generation;
    }
    return old.generation;
  }

  async function callBackend(method, args = {}) {
    try {
      const reply = await timeout(serverAPI.callPluginMethod(method, args), 5000);
      if (reply?.success === false) throw new Error(boundedString(reply.result));
      return reply && Object.prototype.hasOwnProperty.call(reply, "result") ? reply.result : reply;
    } catch (error) {
      // Do not log args: pairing payloads and client tokens must stay private.
      logBackendFailure(method, error);
      throw error;
    }
  }

  function showPairingToast(item) {
    const toaster = serverAPI?.toaster;
    if (!toaster || typeof toaster.toast !== "function") return;
    const client = boundedString(item?.client_name || "Omarchy client", 96);
    const code = item?.verification_code ? `Code: ${boundedString(item.verification_code, 32)}` : "Open the plugin to review the pairing request.";
    try {
      toaster.toast({
        title: "SteamOS Remote pairing request",
        body: `${client} requested access. ${code} Open SteamOS Remote and approve only after checking the matching code.`,
        duration: 10000,
      });
    } catch (error) {
      console.warn(tag, "pairing notification failed", boundedString(error));
    }
  }

  async function readPairingRequests() {
    if (stopped || pairingWatchBusy) return;
    pairingWatchBusy = true;
    try {
      const value = await callBackend("get_settings");
      const pending = Array.isArray(value?.pending_pairings) ? value.pending_pairings : [];
      const current = new Map(pending.filter(item => item && item.pairing_id).map(item => [item.pairing_id, item]));
      if (knownPendingPairings !== null) {
        for (const [pairingId, item] of current) {
          if (!knownPendingPairings.has(pairingId)) showPairingToast(item);
        }
      }
      knownPendingPairings = current;
    } catch (_) {
      // The settings page reports backend failures; the background watcher is
      // best-effort and must not interfere with the long-lived bridge.
    } finally {
      pairingWatchBusy = false;
    }
  }

  function startPairingWatcher() {
    if (typeof serverAPI?.toaster?.toast !== "function") return;
    void readPairingRequests();
    pairingWatchTimer = setInterval(() => { void readPairingRequests(); }, 2000);
  }

  async function readSnapshot(reason = "poll", force = false) {
    if (stopped) return latestSnapshot;
    if (snapshotBusy) {
      if (!force) return latestSnapshot;
      while (snapshotBusy && !stopped) await delay(10);
      if (stopped) return latestSnapshot;
    }
    snapshotBusy = true;
    try {
      const currentDisplayManager = displayManager();
      if (!currentDisplayManager || typeof currentDisplayManager.GetState !== "function") throw new Error("DisplayManager.GetState unavailable");
      const state = decodeDisplayState(await timeout(Reflect.apply(currentDisplayManager.GetState, currentDisplayManager, []), 5000));
      latestSnapshot = normalizedSnapshot(state, "");
      lastSnapshotAt = Date.now();
      await callBackend("report_bridge_snapshot", {snapshot: latestSnapshot});
      notify();
      return latestSnapshot;
    } catch (error) {
      latestSnapshot = normalizedSnapshot(null, `${reason}: ${boundedString(error)}`);
      latestSnapshot.ready = false;
      latestSnapshot.methods.display = false;
      const currentSystem = steamSystem();
      latestSnapshot.methods.suspend = typeof currentSystem?.SuspendPC === "function";
      latestSnapshot.methods.restart = typeof currentSystem?.RestartPC === "function";
      latestSnapshot.methods.shutdown = typeof currentSystem?.ShutdownPC === "function";
      try { await callBackend("report_bridge_snapshot", {snapshot: latestSnapshot}); } catch (_) {}
      notify();
      return latestSnapshot;
    } finally {
      snapshotBusy = false;
    }
  }

  function findOutput(id) { return latestSnapshot?.outputs?.find(output => output.id === String(id)) || null; }
  function findMode(output, id) { return output?.modes?.find(mode => mode.id === String(id)) || null; }

  async function executeCommand(command) {
    if (!command || !command.command_id || !command.kind || !command.payload) return;
    let result;
    let action = "";
    try {
      if (command.kind === "set_mode") {
        const currentDisplayManager = displayManager();
        const output = findOutput(command.payload.output_id);
        const mode = findMode(output, command.payload.mode_id);
        if (!output || !mode || output.generation !== command.payload.generation)
          throw new Error("display target is stale or no longer advertised");
        const payload = encodeSetMode(output.id, mode.id, output.rgb_range);
        if (!currentDisplayManager || typeof currentDisplayManager.SetMode !== "function") throw new Error("DisplayManager.SetMode unavailable");
        const returned = await timeout(Reflect.apply(currentDisplayManager.SetMode, currentDisplayManager, [payload]), 5000);
        const snapshot = await readSnapshot("mode readback", true);
        result = {ok: true, outcome: "method_returned", returned: serialize(returned), payload_base64: payload, snapshot};
      } else if (command.kind === "power") {
        action = boundedString(command.payload.action, 32);
        const methodName = powerMethodName(action);
        const currentSystem = steamSystem();
        if (!methodName || typeof currentSystem?.[methodName] !== "function") throw new Error(`${methodName || "Power method"} is unavailable`);
        console.info(tag, "dispatching power action", action);
        const returned = await timeout(Reflect.apply(currentSystem[methodName], currentSystem, []), 5000);
        console.info(tag, "power method returned", action);
        result = {ok: true, outcome: "method_returned", returned: serialize(returned), action};
      } else if (command.kind === "sunshine_status") {
        const running = ownerRunningValue(await callSunshineOwner(SUNSHINE_OWNER_METHODS.status));
        if (running === null) throw new Error("Decky Sunshine returned an invalid status");
        result = {ok: true, running, owner: SUNSHINE_OWNER_PLUGIN};
      } else if (command.kind === "sunshine_restart") {
        const returned = await callSunshineOwner(SUNSHINE_OWNER_METHODS.start);
        if (returned === false || (returned && returned.ok === false)) throw new Error("Decky Sunshine rejected restart");
        result = {ok: true, outcome: "method_returned", returned: serialize(returned), owner: SUNSHINE_OWNER_PLUGIN};
      } else {
        throw new Error("unsupported bridge command");
      }
    } catch (error) {
      console.warn(tag, "power/display command failed", action || command.kind, boundedString(error));
      result = {ok: false, reason: boundedString(error), unknown: /timeout|suspend|restart|shutdown|power/i.test(boundedString(error))};
    }
    try { await callBackend("report_bridge_result", {command_id: command.command_id, result}); }
    catch (error) { console.warn(tag, "bridge result was not accepted", boundedString(error)); }
  }

  async function driverCycle() {
    if (stopped) return;
    if (!commandBusy) {
      commandBusy = true;
      try {
        const command = await callBackend("next_bridge_command");
        if (command) await executeCommand(command);
      } catch (error) {
        // A missing backend during Decky reload is a bounded transient; do not
        // turn it into a remote operation or log request material.
      } finally { commandBusy = false; }
    }
    if (Date.now() - lastSnapshotAt >= snapshotPollMs && !snapshotBusy) void readSnapshot("periodic");
    driverTimer = setTimeout(driverCycle, commandPollMs);
  }

  void readSnapshot("startup");
  void driverCycle();
  startPairingWatcher();
  startSunshineOwnerWatcher();

  function Content() {
    const [, refresh] = React.useState(0);
    const [now, setNow] = React.useState(() => Date.now());
    const [settings, setSettings] = React.useState(null);
    const [busy, setBusy] = React.useState(false);
    const [message, setMessage] = React.useState("");
    const [pairing, setPairing] = React.useState(null);
    const [address, setAddress] = React.useState("0.0.0.0");
    const [port, setPort] = React.useState(18443);
    const [advertisedHost, setAdvertisedHost] = React.useState("");
    const [detectedHost, setDetectedHost] = React.useState("");
    const [draftDirty, setDraftDirty] = React.useState(false);
    const draftDirtyRef = React.useRef(false);

    function markDraftDirty() {
      draftDirtyRef.current = true;
      setDraftDirty(true);
    }

    function clearDraftDirty() {
      draftDirtyRef.current = false;
      setDraftDirty(false);
    }

    function cancelDraft() {
      const saved = settings?.settings || {};
      setAddress(saved.listen_address || "0.0.0.0");
      setPort(saved.listen_port || 18443);
      setAdvertisedHost(saved.advertised_host || "");
      clearDraftDirty();
      setMessage("Unsaved listener changes discarded");
    }

    async function load() {
      try {
        const value = await callBackend("get_settings");
        setSettings(value);
        if (!draftDirtyRef.current) {
          setAddress(value?.settings?.listen_address || "0.0.0.0");
          setPort(value?.settings?.listen_port || 18443);
          setAdvertisedHost(value?.settings?.advertised_host || "");
        }
        setDetectedHost(value?.pairing_host || "");
      } catch (error) { setMessage(`Settings unavailable: ${boundedString(error)}`); }
    }

    React.useEffect(() => {
      notify = () => refresh(value => value + 1);
      void load();
      const settingsTimer = setInterval(() => { void load(); }, 2000);
      const countdownTimer = setInterval(() => setNow(Date.now()), 1000);
      return () => {
        clearInterval(settingsTimer);
        clearInterval(countdownTimer);
        notify = () => {};
      };
    }, []);

    async function run(action) {
      setBusy(true); setMessage("");
      try { await action(); await load(); }
      catch (error) { setMessage(boundedString(error)); }
      finally { setBusy(false); }
    }

    function button(label, action, disabled = false) {
      return React.createElement("button", {onClick: () => void run(action), disabled: busy || disabled, style: {display: "block", marginTop: "6px"}}, label);
    }

    async function copyPairing() {
      if (!pairing?.payload) return;
      try {
        if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(pairing.payload);
          setMessage("Full pairing payload copied");
        } else {
          setMessage("Clipboard is unavailable; select and copy the full payload");
        }
      } catch (_) { setMessage("Clipboard is unavailable; select and copy the full payload"); }
    }

    function pairingRemainingSeconds(expiresAt) {
      const expiry = Number(expiresAt);
      if (!Number.isFinite(expiry)) return null;
      return Math.max(0, Math.ceil(expiry - now / 1000));
    }

    function pairingCountdown(seconds) {
      if (seconds === null) return "unknown";
      if (seconds <= 0) return "expired";
      const minutes = Math.floor(seconds / 60);
      const remainder = String(seconds % 60).padStart(2, "0");
      return `${minutes}:${remainder}`;
    }

    function renderPendingPairing(item) {
      const remaining = pairingRemainingSeconds(item.expires_at);
      const expired = remaining !== null && remaining <= 0;
      const scopes = item.requested_scopes?.join(", ") || "status.read";
      return React.createElement("div", {key: item.pairing_id, style: {marginTop: "8px", padding: "8px", border: "1px solid rgba(255,255,255,0.25)", borderRadius: "4px"}},
        React.createElement("strong", {style: {display: "block"}}, "New pairing request"),
        React.createElement("span", {style: {display: "block"}}, `${item.client_name || "Omarchy client"} — ${scopes}`),
        item.verification_code && React.createElement("small", {style: {display: "block", marginTop: "6px"}}, "Verification code — compare with Omarchy:"),
        item.verification_code && React.createElement("div", {style: {fontSize: "24px", fontFamily: "monospace", fontWeight: "bold", letterSpacing: "3px", margin: "2px 0 4px"}}, item.verification_code),
        React.createElement("small", {style: {display: "block", fontWeight: "bold"}}, `Expires in: ${pairingCountdown(remaining)}`),
        item.verification_code
          ? React.createElement("small", {style: {display: "block", marginTop: "4px"}}, expired ? "This code has expired. Start a new request from Omarchy." : "Approve only when this code matches exactly on both screens.")
          : React.createElement("small", {style: {display: "block", marginTop: "4px"}}, expired ? "This pairing request has expired." : "Review the requested scopes before approving."),
        button("Approve", async () => { await callBackend("approve_pairing", {pairing_id: item.pairing_id, scopes: item.requested_scopes}); }, expired),
        button("Reject", async () => { await callBackend("reject_pairing", {pairing_id: item.pairing_id}); }, expired)
      );
    }

    const provider = settings?.provider;
    const sunshine = settings?.sunshine;
    const pending = settings?.pending_pairings || [];
    const clients = settings?.clients || [];
    const bridge = settings?.bridge || latestSnapshot;
    const wakeTarget = settings?.wake_target;

    function providerSummary() {
      if (provider?.ready) return `${provider.provider} contract ${provider.contract_version}`;
      if (sunshine?.provider) return `${sunshine.provider} status only — owner bridge unavailable`;
      return `Unavailable — ${provider?.reason || "Decky Sunshine owner plugin is not connected"}`;
    }

    function sunshineSummary() {
      if (!sunshine) return "Unavailable — no observation yet";
      if (sunshine.reason && /provider is not connected|owner plugin was not reachable|owner bridge is unavailable/i.test(String(sunshine.reason))) {
        return "Unavailable — Decky Sunshine is not connected through Decky Loader";
      }
      return `${sunshine.state || "unavailable"}${sunshine.reason ? ` — ${sunshine.reason}` : ""}`;
    }

    return React.createElement("div", {style: {padding: "12px", lineHeight: "1.45", maxWidth: "680px"}},
      React.createElement("h2", null, "SteamOS Remote host"),
      React.createElement("p", null, `Host identity: ${settings?.host_id || "Unavailable"}`),
      React.createElement("p", null, `Steam bridge: ${bridge?.ready ? "Ready" : "Unavailable"}${bridge?.reason ? ` — ${bridge.reason}` : ""}`),
      React.createElement("p", null, `TLS pin: ${settings?.tls?.fingerprint || "Unavailable"}${settings?.tls?.reason ? ` — ${settings.tls.reason}` : ""}`),
      React.createElement("h3", null, "Listener"),
      React.createElement("label", null, "Bind address (0.0.0.0 = all interfaces) ", React.createElement("input", {value: address, onChange: event => { setAddress(event.target.value); markDraftDirty(); }})),
      React.createElement("label", {style: {display: "block", marginTop: "4px"}}, "Port ", React.createElement("input", {type: "number", min: 1024, max: 65535, value: port, onChange: event => { setPort(Number(event.target.value)); markDraftDirty(); }})),
      React.createElement("label", {style: {display: "block", marginTop: "4px"}}, "Pairing host/IP override ", React.createElement("input", {value: advertisedHost, placeholder: detectedHost ? `auto-detect (${detectedHost})` : "auto-detect", onChange: event => { setAdvertisedHost(event.target.value); markDraftDirty(); }})),
      React.createElement("small", {style: {display: "block"}}, advertisedHost ? "Using the manually entered pairing host/IP." : `Using the active route address: ${detectedHost || "unavailable"}`),
      React.createElement("small", {style: {display: "block"}}, wakeTarget?.available ? `Wake target advertised after pairing: ${wakeTarget.mac} via ${wakeTarget.interface || "active host interface"}` : `Wake target unavailable: ${wakeTarget?.reason || "no active host interface"}`),
      draftDirty && React.createElement("small", {style: {display: "block", marginTop: "4px"}}, "Unsaved listener changes are preserved while this page refreshes."),
      button("Save listener settings", async () => {
        await callBackend("update_settings", {changes: {listen_address: address, listen_port: port, advertised_host: advertisedHost}});
        clearDraftDirty();
      }),
      draftDirty && React.createElement("button", {onClick: cancelDraft, disabled: busy, style: {display: "block", marginTop: "6px"}}, "Cancel listener edits"),
      React.createElement("h3", null, "Sunshine"),
      React.createElement("label", null, React.createElement("input", {type: "checkbox", checked: settings?.settings?.monitor_sunshine === true, onChange: event => void run(async () => { await callBackend("update_settings", {changes: {monitor_sunshine: event.target.checked}}); })}), " Monitor Sunshine"),
      React.createElement("p", null, "When enabled, paired clients can see Decky Sunshine status and request a restart only after the Decky owner bridge confirms it is stopped. A read-only process check may keep status visible during a short reload gap; this plugin never starts a separate Sunshine process."),
      React.createElement("p", null, `Provider: ${providerSummary()}`),
      React.createElement("p", null, `Observation: ${sunshineSummary()}`),
      React.createElement("h3", null, "Pairing"),
      React.createElement("p", null, "Start on Omarchy: generate a code, send the pairing request, then approve here only when the code matches on both screens."),
      button("Advanced: create full pairing payload", async () => { setPairing(await callBackend("create_pairing", {requested_scopes: ["status.read", "power.control", "display.control"]})); }),
      pairing && React.createElement("div", {style: {marginTop: "8px"}},
        React.createElement("p", null, `Expires: ${new Date(pairing.expires_at * 1000).toLocaleTimeString()}`),
        pairing.qr_svg_base64 && React.createElement("img", {alt: "Pairing QR code", src: `data:image/svg+xml;base64,${pairing.qr_svg_base64}`, style: {display: "block", width: "220px", height: "220px", background: "white", padding: "8px"}}),
        React.createElement("textarea", {readOnly: true, value: pairing.payload, rows: 4, style: {width: "100%"}}),
        React.createElement("button", {onClick: () => void copyPairing(), style: {display: "block", marginTop: "6px"}}, "Copy full pairing payload"),
        React.createElement("p", null, "Paste the full payload into the Omarchy client, then approve the pending request below.")),
      React.createElement("h4", null, "Pending requests"),
      pending.length ? pending.map(renderPendingPairing)
        : React.createElement("p", null, "No pending pairing request."),
      React.createElement("h4", null, "Paired clients"),
      clients.length ? clients.map(item => React.createElement("div", {key: item.client_id, style: {marginTop: "6px"}},
        React.createElement("span", null, `${item.name} — ${item.scopes?.join(", ")}`),
        button("Revoke", async () => { await callBackend("revoke_client", {client_id: item.client_id}); })))
        : React.createElement("p", null, "No paired clients."),
      message && React.createElement("p", null, message),
      React.createElement("small", {style: {display: "block", opacity: 0.75}}, settings?.diagnostics?.log_path ? `Backend log: ${settings.diagnostics.log_path}` : "Backend log is unavailable until the plugin responds."),
      React.createElement("p", null, "The frontend bridge continues while this settings view is closed. Remote operations remain bounded and report Requested/Observed separately.")
    );
  }

  return {
    name: "SteamOS Remote",
    icon: React ? React.createElement("span", null, "R") : null,
    content: React ? React.createElement(Content) : null,
    onDismount() {
      stopped = true;
      if (driverTimer) clearTimeout(driverTimer);
      if (pairingWatchTimer) clearInterval(pairingWatchTimer);
      if (sunshineOwnerTimer) clearInterval(sunshineOwnerTimer);
      if (typeof window.removeEventListener === "function") {
        for (const eventName of wakeEventNames) window.removeEventListener(eventName, wakeBridge);
      }
      notify = () => {};
    },
  };
})()
