"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  extensionModule,
  FakeStorageArea,
  FakeScheduler,
  FakeTabs,
  FakeWindows,
  FakeRuntime,
  FakeNativeConnector,
  StubNative,
  findSession,
  flushMicrotasks,
  waitFor,
} = require("./extension-test-helpers.cjs");

const controllerModule = extensionModule("lib", "controller.js");
const storeModule = extensionModule("lib", "store.js");
const constantsModule = extensionModule("lib", "constants.js");
const nativeClientModule = extensionModule("lib", "native-client.js");

async function makeHarness(options = {}) {
  const { BackgroundController } = await controllerModule;
  const { ExtensionStore } = await storeModule;
  const scheduler = options.scheduler || new FakeScheduler();
  const storage = options.storage || new FakeStorageArea(options.initialStorage);
  let closureCounter = 0;
  const store = new ExtensionStore({
    storageArea: storage,
    now: () => new Date(scheduler.nowMs),
    makeClosureId: () => `closure-${++closureCounter}`,
  });
  const tabs = options.tabs || new FakeTabs();
  const windows = options.windows || new FakeWindows();
  const runtime = options.runtime || new FakeRuntime();
  const native =
    options.native ||
    new StubNative(
      options.handler ||
        (async (action) =>
          action === "status" ? { ok: true, sessions: [] } : { ok: true, sessions: [] }),
    );
  const controller = new BackgroundController({
    store,
    native,
    connectNative: options.connectNative,
    scheduler,
    tabs,
    windows,
    runtime,
    now: () => new Date(scheduler.nowMs),
    bridgeTimeoutMs: options.bridgeTimeoutMs ?? 1000,
    bridgePollIntervalMs: options.bridgePollIntervalMs ?? 100,
    requestTimeoutMs: 500,
    postMutationRefreshMs: 100,
    logger: { error() {} },
  });
  return {
    BackgroundController,
    store,
    storage,
    scheduler,
    tabs,
    windows,
    runtime,
    native,
    controller,
  };
}

function session(overrides = {}) {
  return findSession(overrides);
}

test("init refreshes status and popup get-state returns cached worker state", async (t) => {
  const { MessageType } = await constantsModule;
  const harness = await makeHarness({
    handler: async (action) =>
      action === "status"
        ? { ok: true, sessions: [session()] }
        : { ok: true, sessions: [session()] },
  });
  t.after(() => harness.controller.stop());

  await harness.controller.init();
  assert.equal(harness.controller.sessions.length, 1);
  assert.equal(harness.native.requestsFor("status").length >= 2, true);

  const response = await harness.controller.handleMessage({
    type: MessageType.GET_STATE,
  });
  assert.equal(response.ok, true);
  assert.equal(response.state.connected, true);
  assert.equal(response.state.sessions.length, 1);
  assert.equal(response.state.sessions[0].name, "Quarterly report.docx");
  assert.ok(
    harness.runtime.sentMessages.some(
      (message) => message.type === MessageType.STATE_CHANGED,
    ),
  );
});

test("claiming a bridge binds the tab and durably stores the mapping before redirect", async (t) => {
  const tabs = new FakeTabs([{ id: 5, windowId: 3, url: "chrome-extension://bridge" }]);
  const harness = await makeHarness({
    tabs,
    handler: async (action) => {
      if (action === "status") return { ok: true, sessions: [session()] };
      if (action === "bind") {
        return { ok: true, sessions: [session({ tab_id: 5 })] };
      }
      return { ok: true, sessions: [] };
    },
  });
  t.after(() => harness.controller.stop());

  const result = await harness.controller.claimBridge(
    session().session_id,
    5,
  );
  assert.equal(result.action, "redirect");
  assert.equal(result.url, session().url);
  const mapping = await harness.store.getMapping(session().session_id);
  assert.equal(mapping.tab_id, 5);
  assert.equal(mapping.session_id, session().session_id);

  const requests = harness.native.requests.map((request) => request.action);
  assert.deepEqual(requests, ["status", "bind"]);
  assert.equal(harness.native.requests[1].params.session_id, session().session_id);
  assert.equal(harness.native.requests[1].params.tab_id, 5);
});

test("duplicate bridge launch focuses the mapped tab and never binds or finalizes it", async (t) => {
  const tabs = new FakeTabs([
    { id: 1, windowId: 4, url: "https://word.cloud.microsoft/document/abc" },
    { id: 2, windowId: 4, url: "chrome-extension://bridge" },
  ]);
  const windows = new FakeWindows();
  const harness = await makeHarness({ tabs, windows });
  t.after(() => harness.controller.stop());
  await harness.store.setMapping(session().session_id, {
    tab_id: 1,
    session_id: session().session_id,
  });

  const result = await harness.controller.claimBridge(session().session_id, 2);
  assert.equal(result.action, "focus");
  assert.equal(result.existing_tab_id, 1);
  assert.equal(harness.native.requests.length, 0);
  assert.equal(windows.updates.length, 1);
  assert.equal(windows.updates[0].windowId, 4);
  assert.equal((await harness.store.getMapping(session().session_id)).tab_id, 1);

  // The bridge closes itself. Its tab was never mapped, so it must not queue
  // or send tab_closed for the surviving tracked session.
  await harness.controller.handleTabRemoved(2);
  assert.deepEqual(await harness.store.getPendingClosures(), []);
  assert.equal(harness.native.requestsFor("tab_closed").length, 0);
  assert.equal((await harness.store.getMapping(session().session_id)).tab_id, 1);
});

test("reopening a recoverable error session with a cloud URL binds and redirects", async (t) => {
  const tabs = new FakeTabs([{ id: 12, windowId: 1 }]);
  const harness = await makeHarness({
    tabs,
    handler: async (action) => {
      if (action === "status") {
        return {
          ok: true,
          sessions: [
            session({
              state: "error",
              error: "The last save was interrupted",
            }),
          ],
        };
      }
      if (action === "bind") {
        return { ok: true, sessions: [session({ state: "editing", tab_id: 12 })] };
      }
      return { ok: true, sessions: [] };
    },
  });
  t.after(() => harness.controller.stop());

  const result = await harness.controller.claimBridge(session().session_id, 12);
  assert.equal(result.action, "redirect");
  assert.equal(result.session.state, "editing");
  assert.equal((await harness.store.getMapping(session().session_id)).tab_id, 12);
  assert.equal(harness.native.requestsFor("bind").length, 1);
});

test("a stale mapping is discarded and the bridge rebinds the current tab", async (t) => {
  const tabs = new FakeTabs([{ id: 8, windowId: 1 }]);
  const harness = await makeHarness({
    tabs,
    handler: async (action) => {
      if (action === "status") return { ok: true, sessions: [session()] };
      if (action === "bind") {
        return { ok: true, sessions: [session({ tab_id: 8 })] };
      }
      return { ok: true, sessions: [] };
    },
  });
  t.after(() => harness.controller.stop());
  await harness.store.setMapping(session().session_id, {
    tab_id: 99,
    session_id: session().session_id,
  });

  const result = await harness.controller.claimBridge(session().session_id, 8);
  assert.equal(result.action, "redirect");
  assert.equal((await harness.store.getMapping(session().session_id)).tab_id, 8);
  assert.equal(harness.native.requestsFor("tab_closed").length, 0);
});

test("reconnect rebinds live mapped tabs to cancel the daemon's disconnect final-save grace", async (t) => {
  const tabs = new FakeTabs([{ id: 6, windowId: 1 }]);
  const harness = await makeHarness({
    tabs,
    handler: async (action) => {
      if (action === "status") {
        return { ok: true, sessions: [session({ tab_id: 6 })] };
      }
      if (action === "bind") {
        return { ok: true, sessions: [session({ tab_id: 6 })] };
      }
      return { ok: true, sessions: [] };
    },
  });
  t.after(() => harness.controller.stop());
  await harness.store.setMapping(session().session_id, {
    tab_id: 6,
    session_id: session().session_id,
  });
  await harness.store.setSessions([session({ tab_id: 6 })]);

  await harness.controller.handleNativeConnected();
  assert.equal(harness.native.requestsFor("bind").length, 0);

  await harness.controller.handleNativeConnected();
  const binds = harness.native.requestsFor("bind");
  assert.equal(binds.length, 1);
  assert.deepEqual(binds[0].params, {
    session_id: session().session_id,
    tab_id: 6,
  });
  assert.equal((await harness.store.getMapping(session().session_id)).tab_id, 6);
});

test("reconnect does not clear daemon recovery error state by rebinding", async (t) => {
  const tabs = new FakeTabs([{ id: 6, windowId: 1 }]);
  const harness = await makeHarness({
    tabs,
    handler: async (action) => {
      if (action === "status") {
        return {
          ok: true,
          sessions: [
            session({
              tab_id: 6,
              state: "error",
              error: "the daemon restarted while this document was editing",
            }),
          ],
        };
      }
      return { ok: true, sessions: [session({ tab_id: 6 })] };
    },
  });
  t.after(() => harness.controller.stop());
  await harness.store.setMapping(session().session_id, {
    tab_id: 6,
    session_id: session().session_id,
  });
  await harness.store.setSessions([session({ tab_id: 6 })]);

  await harness.controller.handleNativeConnected();
  await harness.controller.handleNativeConnected();
  assert.equal(harness.native.requestsFor("bind").length, 0);
  assert.equal((await harness.store.getMapping(session().session_id)).tab_id, 6);
});

test("closing a tracked tab removes the mapping and sends tab_closed online", async (t) => {
  const tabs = new FakeTabs([{ id: 7, windowId: 1 }]);
  const harness = await makeHarness({
    tabs,
    handler: async (action) => {
      if (action === "tab_closed") {
        return { ok: true, sessions: [] };
      }
      return { ok: true, sessions: [session({ tab_id: 7 })] };
    },
  });
  t.after(() => harness.controller.stop());
  await harness.store.setMapping(session().session_id, {
    tab_id: 7,
    session_id: session().session_id,
  });
  await harness.store.setSessions([session({ tab_id: 7 })]);

  const queued = await harness.controller.handleTabRemoved(7);
  assert.equal(queued.length, 1);
  assert.equal(queued[0].session_id, session().session_id);
  assert.equal(await harness.store.getMapping(session().session_id), null);
  assert.deepEqual(await harness.store.getPendingClosures(), []);

  const requests = harness.native.requestsFor("tab_closed");
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].params, { session_id: session().session_id });
});

test("offline tab closures are persisted and delivered by a restarted worker", async (t) => {
  const nativeClient = await nativeClientModule;
  const storage = new FakeStorageArea();
  const tabs = new FakeTabs([{ id: 7, windowId: 1 }]);
  const first = await makeHarness({
    storage,
    tabs,
    handler: async (action) => {
      if (action === "tab_closed") {
        throw new nativeClient.NativeOfflineError("native host unavailable");
      }
      return { ok: true, sessions: [session({ tab_id: 7 })] };
    },
  });
  t.after(() => first.controller.stop());
  await first.store.setMapping(session().session_id, {
    tab_id: 7,
    session_id: session().session_id,
  });
  await first.store.setSessions([session({ tab_id: 7 })]);

  await first.controller.handleTabRemoved(7);
  const pending = await first.store.getPendingClosures();
  assert.equal(pending.length, 1);
  assert.equal(pending[0].session_id, session().session_id);
  assert.equal(pending[0].attempts, 1);
  assert.match(pending[0].last_error, /native host unavailable/);
  assert.equal(await first.store.getMapping(session().session_id), null);

  // Simulate a browser/service-worker restart with the same chrome.storage.
  const second = await makeHarness({
    storage,
    tabs,
    handler: async (action) => {
      if (action === "tab_closed") {
        return { ok: true, sessions: [] };
      }
      return { ok: true, sessions: [session()] };
    },
  });
  t.after(() => second.controller.stop());
  await second.controller.init();

  assert.deepEqual(await second.store.getPendingClosures(), []);
  assert.equal(second.native.requestsFor("tab_closed").length, 1);
});

test("status removes terminal mappings without sending tab_closed", async (t) => {
  const harness = await makeHarness({
    handler: async (action) =>
      action === "status"
        ? { ok: true, sessions: [session({ state: "saved" })] }
        : { ok: true, sessions: [] },
  });
  t.after(() => harness.controller.stop());
  await harness.store.setMapping(session().session_id, {
    tab_id: 3,
    session_id: session().session_id,
  });
  await harness.store.setSessions([session({ tab_id: 3 })]);

  await harness.controller.init();
  assert.equal(await harness.store.getMapping(session().session_id), null);
  assert.equal(harness.native.requestsFor("tab_closed").length, 0);
});

test("manual Save always sends finish:false and Retry uses the retry action", async (t) => {
  const { MessageType } = await constantsModule;
  const harness = await makeHarness({
    handler: async (action) => ({ ok: true, sessions: [session({ state: "editing" })] }),
  });
  t.after(() => harness.controller.stop());

  const save = await harness.controller.handleMessage({
    type: MessageType.SAVE,
    session_id: session().session_id,
    finish: true,
  });
  assert.equal(save.ok, true);
  const saveRequest = harness.native.requestsFor("save")[0];
  assert.equal(saveRequest.params.finish, false);
  assert.equal(saveRequest.params.session_id, session().session_id);

  const retry = await harness.controller.handleMessage({
    type: MessageType.RETRY,
    session_id: session().session_id,
  });
  assert.equal(retry.ok, true);
  const retryRequest = harness.native.requestsFor("retry")[0];
  assert.equal(retryRequest.params.session_id, session().session_id);
  assert.equal(Object.hasOwn(retryRequest.params, "finish"), false);
});

test("save errors are returned to the popup without claiming success", async (t) => {
  const { MessageType } = await constantsModule;
  const harness = await makeHarness({
    handler: async (action) => {
      if (action === "save") throw new Error("Upload interrupted");
      return { ok: true, sessions: [session()] };
    },
  });
  t.after(() => harness.controller.stop());

  const response = await harness.controller.handleMessage({
    type: MessageType.SAVE,
    session_id: session().session_id,
  });
  assert.equal(response.ok, false);
  assert.equal(response.error, "Upload interrupted");
});

test("bridge polling waits for a session URL before binding", async (t) => {
  let statusCalls = 0;
  const harness = await makeHarness({
    handler: async (action) => {
      if (action === "status") {
        statusCalls += 1;
        if (statusCalls === 1) return { ok: true, sessions: [] };
        return { ok: true, sessions: [session()] };
      }
      if (action === "bind") {
        return { ok: true, sessions: [session({ tab_id: 5 })] };
      }
      return { ok: true, sessions: [] };
    },
  });
  t.after(() => harness.controller.stop());

  const promise = harness.controller.claimBridge(session().session_id, 5);
  await flushMicrotasks();
  assert.equal(statusCalls, 1);
  await harness.scheduler.advance(100);
  const result = await promise;
  assert.equal(result.action, "redirect");
  assert.equal(statusCalls >= 2, true);
  assert.equal((await harness.store.getMapping(session().session_id)).tab_id, 5);
});

test("bridge polling times out with a retryable human error", async (t) => {
  const harness = await makeHarness({
    bridgeTimeoutMs: 200,
    bridgePollIntervalMs: 50,
    handler: async () => ({ ok: true, sessions: [] }),
  });
  t.after(() => harness.controller.stop());

  const outcome = harness.controller
    .claimBridge(session().session_id, 5)
    .then((value) => ({ value }), (error) => ({ error }));
  for (let index = 0; index < 6; index += 1) {
    await flushMicrotasks();
    await harness.scheduler.advance(50);
  }
  const { error } = await outcome;
  assert.equal(error.name, "BridgeError");
  assert.match(error.message, /not found|prepared/i);
  assert.equal(await harness.store.getMapping(session().session_id), null);
});

test("browser startup clears tab ids but still delivers queued offline closures", async (t) => {
  const harness = await makeHarness({
    handler: async (action) => {
      if (action === "tab_closed") return { ok: true, sessions: [] };
      return { ok: true, sessions: [session()] };
    },
  });
  t.after(() => harness.controller.stop());
  await harness.store.setMapping(session().session_id, {
    tab_id: 2,
    session_id: session().session_id,
  });
  await harness.store.enqueueTabClosure({
    session_id: session().session_id,
    tab_id: 2,
    name: session().name,
    original_path: session().original_path,
  });

  await harness.controller.handleBrowserStartup();
  assert.deepEqual(await harness.store.getMappings(), {});
  assert.deepEqual(await harness.store.getPendingClosures(), []);
  assert.equal(harness.native.requestsFor("tab_closed").length, 1);
});

test("real native client reconnects and bootstraps status after a dropped port", async (t) => {
  const connector = new FakeNativeConnector();
  const runtime = new FakeRuntime();
  const harness = await makeHarness({
    handler: async () => {
      throw new Error("stub native must not be used");
    },
    runtime,
    connectNative: (name) => connector.connectNative(name),
  });
  // Replace the injected stub with a real NativeClient controller.
  const { BackgroundController } = await controllerModule;
  harness.controller.stop();
  const real = new BackgroundController({
    store: harness.store,
    connectNative: (name) => connector.connectNative(name),
    scheduler: harness.scheduler,
    tabs: harness.tabs,
    windows: harness.windows,
    runtime,
    now: () => new Date(harness.scheduler.nowMs),
    requestTimeoutMs: 500,
    bridgePollIntervalMs: 10,
    bridgeTimeoutMs: 100,
    logger: { error() {} },
  });
  t.after(() => real.stop());

  const initPromise = real.init();
  await waitFor(() => connector.latest() && connector.latest().sent.length > 0, {
    label: "initial status request",
  });
  const firstPort = connector.latest();
  assert.equal(firstPort.sent[0].action, "status");
  firstPort.respondTo(0, { ok: true, sessions: [session()] });
  // The connect bootstrap performs a second status refresh after closure flush.
  await waitFor(() => firstPort.sent.length >= 2, {
    label: "bootstrap refresh request",
  });
  firstPort.respondTo(1, { ok: true, sessions: [session()] });
  await initPromise;
  assert.equal(real.sessions.length, 1);
  assert.equal(real.native.connected, true);

  firstPort.disconnect();
  await waitFor(() => !real.native.connected, { label: "disconnect" });
  assert.match(real.lastError, /disconnected/i);

  await harness.scheduler.advance(1000);
  await waitFor(
    () =>
      connector.ports.length >= 2 &&
      connector.latest() !== firstPort &&
      connector.latest().sent.length > 0,
    { label: "reconnect status request" },
  );
  const secondPort = connector.latest();
  secondPort.respondTo(0, { ok: true, sessions: [session()] });
  await waitFor(() => secondPort.sent.length >= 2, {
    label: "reconnect bootstrap refresh request",
  });
  secondPort.respondTo(1, { ok: true, sessions: [session()] });
  await flushMicrotasks();
  assert.equal(real.native.connected, true);
  assert.equal(real.sessions.length, 1);
  assert.equal(secondPort.sent[0].action, "status");
});
