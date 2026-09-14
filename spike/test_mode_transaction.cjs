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

function field(number, value) {
  return concat(varint(number * 8), varint(value));
}

function bytesField(number, value) {
  const bytes = Buffer.from(value);
  return concat(varint(number * 8 + 2), varint(bytes.length), bytes);
}

function mode(id, width, height, refresh) {
  return concat(field(1, id), field(2, width), field(3, height), field(4, refresh));
}

function state(currentModeId) {
  const modes = currentModeId === 2
    ? [mode(2, 3840, 2160, 60), mode(4, 3440, 1440, 60)]
    : currentModeId === 6001
      ? [mode(6001, 3440, 1440, 59), mode(2, 3840, 2160, 60)]
      : [mode(3352, 3440, 1440, 59), mode(2, 3840, 2160, 60)];
  const display = concat(
    field(1, 1),
    bytesField(2, Buffer.from("gamescope")),
    bytesField(3, Buffer.from("test display")),
    field(4, 1),
    field(5, 1),
    field(10, currentModeId),
    ...modes.map(candidate => bytesField(11, candidate)),
  );
  return concat(bytesField(1, display), field(2, 1), field(3, 1));
}

function decodeSetMode(value) {
  const bytes = Buffer.from(value, "base64");
  const result = {};
  let offset = 0;
  while (offset < bytes.length) {
    const key = readVarint(bytes, offset);
    offset = key.offset;
    assert.equal(key.value % 8, 0, "test payload uses only varint fields");
    const fieldNumber = key.value / 8;
    const decoded = readVarint(bytes, offset);
    offset = decoded.offset;
    result[fieldNumber] = decoded.value;
  }
  return result;
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
  const source = fs.readFileSync(path.join(__dirname, "dist/index.js"), "utf8");
  const events = [];
  const modeWrites = [];
  let currentModeId = 3352;
  let stateReads = 0;
  let powerCalls = 0;
  let unsubscribed = 0;

  const dm = {
    GetState() {
      stateReads++;
      return Promise.resolve({
        result: 1,
        message: "",
        reply: new Uint8Array(state(currentModeId)),
      });
    },
    SetMode(payload) {
      const decoded = decodeSetMode(payload);
      modeWrites.push(decoded);
      currentModeId = decoded[2] === 4 ? 6001 : decoded[2];
      return Promise.resolve("ok");
    },
    ClearModeOverride() {},
    SetCompatibilityMode() {},
    RegisterForStateChanges() {
      return {unregister() { unsubscribed++; }};
    },
  };
  const system = {
    DisplayManager: dm,
    SuspendPC() { powerCalls++; },
    RestartPC() {},
    ShutdownPC() {},
  };

  const React = {
    useState(initial) { return [initial, () => {}]; },
    useEffect(effect) { effect(); },
    createElement(type, props, ...children) {
      if (typeof type === "function") return type(props);
      return {type, props: props || {}, children};
    },
  };
  const realSetTimeout = setTimeout;
  const fastSetTimeout = (callback, milliseconds, ...args) => realSetTimeout(callback, Math.min(milliseconds, 10), ...args);
  const pluginFactory = vm.runInNewContext(source, {
    window: {SteamClient: {System: system}, SP_REACT: React},
    console: {info() {}, warn() {}},
    setTimeout: fastSetTimeout,
    clearTimeout,
    ArrayBuffer,
    Uint8Array,
    WeakSet,
    TextDecoder,
    atob: value => Buffer.from(value, "base64").toString("binary"),
    btoa: value => Buffer.from(value, "binary").toString("base64"),
  });
  const plugin = pluginFactory({callPluginMethod: async (method, {event}) => {
    assert.equal(method, "record_frontend");
    events.push(event);
    return {success: true};
  }});

  await new Promise(resolve => realSetTimeout(resolve, 30));
  const labels = plugin.content.children.filter(child => child?.type === "label");
  const selects = labels.map(label => label.children[1]);
  assert.equal(selects.length, 2);
  selects[1].props.onChange({target: {value: "2"}});

  const buttons = plugin.content.children.filter(child => child?.type === "button");
  buttons[1].props.onClick();
  await new Promise(resolve => realSetTimeout(resolve, 200));

  assert.deepEqual(modeWrites, [{1: 1, 2: 2, 3: 0}, {1: 1, 2: 4, 3: 0}]);
  assert.ok(stateReads >= 5, "preflight, readback, restore preflight, and restore readback are captured");
  assert.ok(events.some(event => event.kind === "frontend.mode_test_started"));
  assert.ok(events.some(event => event.kind === "frontend.mode_readback" && event.data.phase === "apply" && event.data.matches));
  assert.ok(events.some(event => event.kind === "frontend.mode_readback" && event.data.phase === "restore" && event.data.matches));
  assert.ok(events.some(event => event.kind === "frontend.mode_restore_resolved" && event.data.matched_by === "resolution_fallback"));
  assert.ok(events.some(event => event.kind === "frontend.mode_readback" && event.data.phase === "restore" && event.data.matched_by === "mode_properties"));
  assert.equal(powerCalls, 0, "mode test must never invoke a power method");

  plugin.onDismount();
  assert.equal(unsubscribed, 1);
  console.log("PASS: explicit mode test applies an advertised mode, reads it back, and auto-restores the baseline");
})().catch(error => { console.error(error); process.exitCode = 1; });
