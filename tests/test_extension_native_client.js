"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  extensionModule,
  FakeScheduler,
  FakeNativeConnector,
  flushMicrotasks,
} = require("./extension-test-helpers.cjs");

const nativeModule = extensionModule("lib", "native-client.js");

function makeClient(overrides = {}) {
  const scheduler = overrides.scheduler || new FakeScheduler();
  const connector = new FakeNativeConnector();
  const events = {
    connected: 0,
    disconnected: [],
    states: [],
  };
  return nativeModule.then(({ NativeClient }) => {
    const client = new NativeClient({
      connectNative: (host) => connector.connectNative(host),
      scheduler,
      reconnectDelaysMs: [100, 200, 400],
      requestTimeoutMs: 1000,
      onConnected: () => {
        events.connected += 1;
      },
      onDisconnected: (error) => {
        events.disconnected.push(error);
      },
      onStateChange: (state) => {
        events.states.push(state);
      },
      logger: { debug() {} },
      ...overrides.options,
    });
    return { client, connector, scheduler, events };
  });
}

test("native client multiplexes out-of-order responses by request id", async () => {
  const { client, connector, scheduler, events } = await makeClient();
  try {
    const first = client.request("status", {}, { timeoutMs: 5000 });
    const second = client.request("save", {
      session_id: "session-a",
      finish: false,
    });
    const port = connector.latest();
    assert.equal(connector.connectCalls.length, 1);
    assert.equal(connector.connectCalls[0], "org.local.office_online");
    assert.equal(port.sent.length, 2);
    assert.equal(port.sent[0].action, "status");
    assert.equal(port.sent[1].action, "save");
    assert.equal(port.sent[1].finish, false);
    assert.notEqual(port.sent[0].id, port.sent[1].id);

    port.respond({
      id: port.sent[1].id,
      ok: true,
      sessions: [{ session_id: "session-a" }],
    });
    port.respond({
      id: port.sent[0].id,
      ok: true,
      sessions: [],
      extra: "backend-field",
    });

    const [status, save] = await Promise.all([first, second]);
    assert.deepEqual(status.sessions, []);
    assert.equal(status.extra, "backend-field");
    assert.equal(save.sessions[0].session_id, "session-a");
    assert.equal(client.pendingCount, 0);

    await flushMicrotasks();
    assert.equal(events.connected, 1);
  } finally {
    client.stop();
    scheduler.timers.clear();
  }
});

test("native client rejects {ok:false} with the backend human error", async () => {
  const { client, connector, scheduler } = await makeClient();
  try {
    const pending = client.request("save", { session_id: "missing" });
    const port = connector.latest();
    port.respond({
      id: port.sent[0].id,
      ok: false,
      error: "Session is no longer tracked.",
    });
    await assert.rejects(pending, (error) => {
      assert.equal(error.name, "NativeResponseError");
      assert.equal(error.message, "Session is no longer tracked.");
      assert.equal(error.action, "save");
      return true;
    });
  } finally {
    client.stop();
    scheduler.timers.clear();
  }
});

test("native client times out one request without disturbing a later request", async () => {
  const { client, connector, scheduler } = await makeClient();
  try {
    const outcome = client
      .request("status", {}, { timeoutMs: 500 })
      .then((value) => ({ value }), (error) => ({ error }));
    const port = connector.latest();
    await scheduler.advance(500);
    const { error } = await outcome;
    assert.equal(error.name, "NativeTimeoutError");
    assert.match(error.message, /status/);
    assert.equal(client.pendingCount, 0);
    assert.equal(client.connected, true);

    const next = client.request("retry", { session_id: "s1" });
    port.respond({
      id: port.sent[port.sent.length - 1].id,
      ok: true,
      sessions: [],
    });
    const response = await next;
    assert.equal(response.ok, true);
  } finally {
    client.stop();
    scheduler.timers.clear();
  }
});

test("native client ignores unsolicited messages and malformed responses", async () => {
  const { client, connector, scheduler } = await makeClient();
  try {
    const pending = client.request("status");
    const port = connector.latest();
    port.respond({ id: "not-a-pending-request", ok: true });
    port.respond({ ok: true });
    port.respond(null);
    assert.equal(client.pendingCount, 1);

    port.respond({ id: port.sent[0].id, ok: true, sessions: [] });
    const response = await pending;
    assert.equal(response.ok, true);
  } finally {
    client.stop();
    scheduler.timers.clear();
  }
});

test("native client rejects pending work on disconnect and reconnects with backoff", async () => {
  const { client, connector, scheduler, events } = await makeClient();
  try {
    const outcome = client
      .request("save", { session_id: "s1", finish: false })
      .then((value) => ({ value }), (error) => ({ error }));
    const firstPort = connector.latest();
    assert.equal(client.connected, true);

    firstPort.disconnect();
    const { error } = await outcome;
    assert.equal(error.name, "NativeOfflineError");
    assert.match(error.message, /disconnected/i);
    assert.equal(client.connected, false);
    assert.equal(events.disconnected.length, 1);
    assert.equal(client.pendingCount, 0);

    await scheduler.advance(100);
    assert.equal(connector.ports.length, 2);
    assert.equal(client.connected, true);
    const secondPort = connector.latest();
    assert.equal(secondPort.sent.length, 0);

    const next = client.request("status");
    secondPort.respond({ id: secondPort.sent[0].id, ok: true, sessions: [] });
    await next;

    await flushMicrotasks();
    assert.equal(events.connected, 2);
  } finally {
    client.stop();
    scheduler.timers.clear();
  }
});

test("native client recovers when postMessage throws on a dead port", async () => {
  const { client, connector, scheduler, events } = await makeClient();
  try {
    client.ensureConnected();
    const deadPort = connector.latest();
    deadPort.failNextPost = true;
    const outcome = client
      .request("status")
      .then((value) => ({ value }), (error) => ({ error }));
    const { error } = await outcome;
    assert.equal(error.name, "NativeOfflineError");
    assert.equal(client.connected, false);
    assert.equal(events.disconnected.length, 1);

    await scheduler.advance(100);
    assert.equal(connector.ports.length, 2);
    assert.equal(client.connected, true);
  } finally {
    client.stop();
    scheduler.timers.clear();
  }
});

test("native client retries connectNative failures with capped backoff", async () => {
  const { client, connector, scheduler, events } = await makeClient();
  try {
    connector.failNextConnect = true;
    assert.equal(client.ensureConnected(), false);
    assert.match(client.lastError, /native host/i);
    assert.equal(client.connected, false);
    assert.equal(events.states.at(-1).connected, false);

    await scheduler.advance(100);
    assert.equal(connector.ports.length, 1);
    assert.equal(client.connected, true);
    assert.equal(events.connected, 1);
  } finally {
    client.stop();
    scheduler.timers.clear();
  }
});

test("native client stop rejects in-flight work and does not reconnect", async () => {
  const { client, connector, scheduler } = await makeClient();
  const outcome = client
    .request("status")
    .then((value) => ({ value }), (error) => ({ error }));
  const port = connector.latest();
  client.stop();
  const { error } = await outcome;
  assert.equal(error.name, "NativeOfflineError");
  assert.equal(client.connected, false);
  await scheduler.advance(1000);
  assert.equal(connector.ports.length, 1);
  assert.equal(port.disconnected, true);
  scheduler.timers.clear();
});
