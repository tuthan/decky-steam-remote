const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

(async () => {
  const events = [];
  const state = Promise.resolve(new Uint8Array([8, 1]));
  let stateReads = 0, modeWrites = 0, powerCalls = 0, unsubscribed = 0;
  const failure = new Error("Steam failure");
  const dm = {
    GetState() { assert.equal(this, dm); stateReads++; return state; },
    SetMode(payload) { assert.equal(this, dm); modeWrites++; return payload; },
    ClearModeOverride() { throw failure; },
    SetCompatibilityMode() { return 17; },
    RegisterForStateChanges(callback) { return {unregister() { unsubscribed++; }}; },
  };
  const system = {DisplayManager: dm, SuspendPC() { powerCalls++; }, RestartPC() {}, ShutdownPC() {}};
  const original = {...dm};
  const React = {
    useState(initial) { return [initial, () => {}]; },
    useEffect(effect) { effect(); },
    createElement(type, props, ...children) {
      if (typeof type === "function") return type(props);
      return {type, props: props || {}, children};
    },
  };
  const factory = vm.runInNewContext(fs.readFileSync(path.join(__dirname, "dist/index.js"), "utf8"), {
    window: {SteamClient: {System: system}, SP_REACT: React}, console: {info() {}, warn() {}},
    setTimeout, clearTimeout, ArrayBuffer, Uint8Array, WeakSet,
    btoa: s => Buffer.from(s, "binary").toString("base64"),
  });
  const plugin = factory({callPluginMethod: async (method, {event}) => {
    assert.equal(method, "record_frontend"); events.push(event); return {success: true};
  }});
  assert.equal(stateReads, 1);
  assert.equal(modeWrites, 0, "loading the probe must never change a mode");
  assert.equal(powerCalls, 0, "loading the probe must never request power");
  assert.equal(dm.GetState(), state, "return the same promise");
  assert.equal(dm.SetMode("CAE="), "CAE=");
  assert.throws(() => dm.ClearModeOverride(), error => error === failure);
  for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve));
  assert(events.some(e => e.kind === "frontend.call" && e.data.method === "SetMode" && e.data.args[0] === "CAE="));
  assert(events.some(e => e.kind === "frontend.result" && e.data.value?.base64 === "CAE="));
  const suspendButton = plugin.content.children.find(child =>
    child?.type === "button" && child.children?.includes("Suspend PC (test)"));
  assert(suspendButton, "the explicit suspend test button is exposed");
  suspendButton.props.onClick();
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(powerCalls, 1, "the suspend test button invokes SuspendPC only after the click");
  assert(events.some(e => e.kind === "frontend.power_test_started" && e.data.method === "SuspendPC"));
  assert(events.some(e => e.kind === "frontend.call" && e.data.method === "SuspendPC"));
  const laterPatch = () => "later";
  dm.SetCompatibilityMode = laterPatch;
  plugin.onDismount();
  assert.equal(dm.SetMode, original.SetMode);
  assert.equal(dm.GetState, original.GetState);
  assert.equal(dm.ClearModeOverride, original.ClearModeOverride);
  assert.equal(dm.SetCompatibilityMode, laterPatch, "do not remove another plugin's later patch");
  assert.equal(unsubscribed, 1);
  console.log("PASS: observer preserves calls, results, throws, promise identity, and patch ownership; no startup mutation");
})().catch(error => { console.error(error); process.exitCode = 1; });
