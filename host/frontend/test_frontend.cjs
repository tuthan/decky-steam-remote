const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function concat(...parts) {
  return Buffer.concat(parts.map(part => Buffer.from(part)));
}

function varint(value) {
  const bytes = [];
  do {
    let byte = value % 128;
    value = Math.floor(value / 128);
    if (value) byte |= 0x80;
    bytes.push(byte);
  } while (value);
  return Buffer.from(bytes);
}

function field(number, value) { return concat(varint(number * 8), varint(value)); }

function bytesField(number, value) {
  const bytes = Buffer.from(value);
  return concat(varint(number * 8 + 2), varint(bytes.length), bytes);
}

function mode(id, width, height, refresh) {
  return concat(field(1, id), field(2, width), field(3, height), field(4, refresh));
}

function displayState(currentModeId) {
  const modes = [mode(3352, 3440, 1440, 59), mode(2, 3840, 2160, 60)];
  const display = concat(
    field(1, 1),
    bytesField(2, "HDMI-A-1"),
    bytesField(3, "test display"),
    field(6, 0),
    field(10, currentModeId),
    ...modes.map(candidate => bytesField(11, candidate)),
  );
  return concat(bytesField(1, display), field(2, 1), field(3, 1));
}

function decodeVarints(value) {
  const bytes = Buffer.from(value, "base64");
  const fields = {};
  let offset = 0;
  while (offset < bytes.length) {
    const key = readVarint(bytes, offset);
    offset = key.offset;
    const decoded = readVarint(bytes, offset);
    offset = decoded.offset;
    fields[key.value / 8] = decoded.value;
  }
  return fields;
}

function readVarint(bytes, start) {
  let value = 0;
  let multiplier = 1;
  let offset = start;
  for (;;) {
    const byte = bytes[offset++];
    value += (byte & 0x7f) * multiplier;
    if ((byte & 0x80) === 0) return {value, offset};
    multiplier *= 128;
  }
}

(async () => {
  let currentModeId = 3352;
  let snapshotReported = false;
  let modeWrites = 0;
  let suspendCalls = 0;
  let restartCalls = 0;
  let shutdownCalls = 0;
  let sunshineStatusCalls = 0;
  let sunshineRestartCalls = 0;
  let loaderConnectCalls = 0;
  const results = [];

  const dm = {
    GetState() { return Promise.resolve(new Uint8Array(displayState(currentModeId))); },
    SetMode(payload) {
      const fields = decodeVarints(payload);
      assert.equal(fields[1], 1);
      currentModeId = fields[2];
      modeWrites++;
      return Promise.resolve("ok");
    },
  };
  const system = {
    DisplayManager: dm,
    SuspendPC() { suspendCalls++; return Promise.resolve("suspend-requested"); },
    RestartPC() { restartCalls++; return Promise.resolve("restart-requested"); },
    ShutdownPC() { shutdownCalls++; return Promise.resolve("shutdown-requested"); },
  };
  const React = {
    useState(initial) { return [initial, () => {}]; },
    useEffect(effect) { effect(); },
    createElement(type, props, ...children) { return {type, props: props || {}, children}; },
  };
  const source = fs.readFileSync(path.join(__dirname, "index.js"), "utf8");
  const realSetTimeout = setTimeout;
  const fastSetTimeout = (callback, milliseconds, ...args) => realSetTimeout(callback, Math.min(milliseconds, 8), ...args);
  const factory = vm.runInNewContext(source, {
    window: {
      SteamClient: {System: system},
      DeckyBackend: {
        call(route, pluginName, methodName) {
          assert.equal(route, "loader/call_legacy_plugin_method");
          assert.equal(pluginName, "Decky Sunshine");
          if (methodName === "isSunshineRunning") { sunshineStatusCalls++; return Promise.resolve({success: true, result: false}); }
          if (methodName === "startSunshine") { sunshineRestartCalls++; return Promise.resolve({success: true, result: true}); }
          throw new Error(`unexpected Sunshine method ${methodName}`);
        },
      },
      __DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit: {
        connect(version, pluginName) {
          loaderConnectCalls++;
          assert.equal(version, 1);
          assert.equal(pluginName, "Decky Sunshine");
          return {
            call(methodName) {
              if (methodName === "isSunshineRunning") { sunshineStatusCalls++; return Promise.resolve(false); }
              if (methodName === "startSunshine") { sunshineRestartCalls++; return Promise.resolve(true); }
              throw new Error(`unexpected Sunshine method ${methodName}`);
            },
          };
        },
      },
      SP_REACT: React,
    },
    console: {info() {}, warn() {}},
    setTimeout: fastSetTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    ArrayBuffer,
    Uint8Array,
    WeakSet,
    TextDecoder,
    atob: value => Buffer.from(value, "base64").toString("binary"),
    btoa: value => Buffer.from(value, "binary").toString("base64"),
  });
  const commands = [
    {command_id: "bridge-mode", operation_id: "op-mode", kind: "set_mode", payload: {output_id: "1", mode_id: "2", generation: 1}},
    {command_id: "bridge-power", operation_id: "op-power", kind: "power", payload: {action: "suspend"}},
    {command_id: "bridge-restart", operation_id: "op-restart", kind: "power", payload: {action: "restart"}},
    {command_id: "bridge-shutdown", operation_id: "op-shutdown", kind: "power", payload: {action: "shutdown"}},
    {command_id: "bridge-sunshine-status", operation_id: "op-sunshine-status", kind: "sunshine_status", payload: {}},
    {command_id: "bridge-sunshine-restart", operation_id: "op-sunshine-restart", kind: "sunshine_restart", payload: {}},
  ];
  const plugin = factory({callPluginMethod: async (method, args = {}) => {
    if (method === "report_bridge_snapshot") {
      snapshotReported = true;
      return {success: true, result: {accepted: true}};
    }
    if (method === "next_bridge_command") {
      return {success: true, result: snapshotReported ? commands.shift() || null : null};
    }
    if (method === "report_bridge_result") {
      results.push({command_id: args.command_id, result: args.result});
      return {success: true, result: {accepted: true}};
    }
    if (method === "report_sunshine_owner") {
      assert.ok(args.report && typeof args.report.available === "boolean");
      return {success: true, result: {ready: true}};
    }
    throw new Error(`unexpected backend method ${method}`);
  }});

  await new Promise(resolve => realSetTimeout(resolve, 250));
  plugin.onDismount();
  assert.equal(modeWrites, 1, "the bridge must apply the advertised mode once");
  assert.equal(suspendCalls, 1, "the bridge must invoke suspend only for a fixed power command");
  assert.equal(restartCalls, 1, "the bridge must invoke restart only for a fixed power command");
  assert.equal(shutdownCalls, 1, "the bridge must invoke shutdown only for a fixed power command");
  assert.deepEqual(results.map(item => item.command_id), ["bridge-mode", "bridge-power", "bridge-restart", "bridge-shutdown", "bridge-sunshine-status", "bridge-sunshine-restart"]);
  assert.equal(results[0].result.ok, true);
  assert.equal(results[0].result.snapshot.outputs[0].current_mode_id, "2");
  assert.equal(results[0].result.snapshot.outputs[0].generation, 1, "a mode switch must not invalidate the output generation");
  assert.equal(results[1].result.ok, true);
  assert.equal(results[2].result.action, "restart");
  assert.equal(results[3].result.action, "shutdown");
  assert.equal(results[4].result.running, false);
  assert.equal(results[5].result.ok, true);
  assert.equal(loaderConnectCalls, 0, "the legacy owner route must be preferred for Decky Sunshine");
  assert.ok(sunshineStatusCalls >= 2, "the bridge must probe and monitor through Decky Sunshine");
  assert.equal(sunshineRestartCalls, 1, "the bridge must invoke the owner restart method only for a fixed command");
  console.log("PASS: production Decky bridge decodes state, preserves display generations, invokes power actions, and delegates Sunshine to its owner");
})().catch(error => { console.error(error); process.exitCode = 1; });
