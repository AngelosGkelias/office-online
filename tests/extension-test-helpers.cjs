"use strict";

const path = require("node:path");
const { pathToFileURL } = require("node:url");

const EXTENSION_DIR = path.join(__dirname, "..", "extension");

function extensionModule(...parts) {
  return import(
    pathToFileURL(path.join(EXTENSION_DIR, ...parts)).href
  );
}

function clone(value) {
  return value === undefined ? undefined : structuredClone(value);
}

async function flushMicrotasks() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

async function waitFor(predicate, { timeoutMs = 2000, label = "condition" } = {}) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  throw new Error(`Timed out waiting for ${label}`);
}

class FakeEvent {
  constructor() {
    this.listeners = [];
  }

  addListener(listener) {
    if (!this.listeners.includes(listener)) this.listeners.push(listener);
  }

  removeListener(listener) {
    this.listeners = this.listeners.filter((item) => item !== listener);
  }

  hasListener(listener) {
    return this.listeners.includes(listener);
  }

  emit(...args) {
    for (const listener of [...this.listeners]) listener(...args);
  }
}

class FakeStorageArea {
  constructor(initial = {}) {
    this.data = clone(initial) || {};
    this.setCalls = [];
  }

  async get(key) {
    if (key === null || key === undefined) return clone(this.data);
    if (typeof key === "string") {
      return key in this.data ? { [key]: clone(this.data[key]) } : {};
    }
    if (Array.isArray(key)) {
      const result = {};
      for (const item of key) {
        if (item in this.data) result[item] = clone(this.data[item]);
      }
      return result;
    }
    const result = {};
    if (typeof key === "object") {
      for (const item of Object.keys(key)) {
        result[item] = item in this.data ? clone(this.data[item]) : clone(key[item]);
      }
    }
    return result;
  }

  async set(values) {
    this.setCalls.push(clone(values));
    Object.assign(this.data, clone(values));
  }
}

class FakeScheduler {
  constructor() {
    this.nowMs = 0;
    this.nextId = 1;
    this.timers = new Map();
  }

  setTimeout(callback, delay = 0) {
    const id = this.nextId++;
    this.timers.set(id, {
      id,
      callback,
      due: this.nowMs + Math.max(0, delay),
      interval: null,
    });
    return id;
  }

  clearTimeout(id) {
    if (id !== null && id !== undefined) this.timers.delete(id);
  }

  setInterval(callback, delay = 0) {
    const id = this.nextId++;
    this.timers.set(id, {
      id,
      callback,
      due: this.nowMs + Math.max(1, delay),
      interval: Math.max(1, delay),
    });
    return id;
  }

  clearInterval(id) {
    if (id !== null && id !== undefined) this.timers.delete(id);
  }

  async advance(ms) {
    const target = this.nowMs + ms;
    let guard = 0;
    while (true) {
      if (++guard > 10000) throw new Error("FakeScheduler runaway timer loop");
      let next = null;
      for (const timer of this.timers.values()) {
        if (timer.due <= target && (!next || timer.due < next.due)) next = timer;
      }
      if (!next) break;
      this.nowMs = next.due;
      if (next.interval !== null) {
        next.due += next.interval;
      } else {
        this.timers.delete(next.id);
      }
      next.callback();
      await flushMicrotasks();
    }
    this.nowMs = target;
  }
}

class FakePort {
  constructor(name = "native") {
    this.name = name;
    this.onMessage = new FakeEvent();
    this.onDisconnect = new FakeEvent();
    this.sent = [];
    this.disconnected = false;
    this.failNextPost = false;
  }

  postMessage(message) {
    if (this.failNextPost) {
      this.failNextPost = false;
      throw new Error("Attempting to use a disconnected port object");
    }
    if (this.disconnected) {
      throw new Error("Attempting to use a disconnected port object");
    }
    this.sent.push(clone(message));
  }

  respond(response) {
    this.onMessage.emit(response);
  }

  respondTo(index, response) {
    const request = this.sent[index];
    if (!request) throw new Error(`No sent native request at index ${index}`);
    this.respond({ id: request.id, ...response });
  }

  disconnect() {
    if (this.disconnected) return;
    this.disconnected = true;
    this.onDisconnect.emit();
  }
}

class FakeNativeConnector {
  constructor() {
    this.ports = [];
    this.failNextConnect = false;
    this.connectCalls = [];
  }

  connectNative(name) {
    this.connectCalls.push(name);
    if (this.failNextConnect) {
      this.failNextConnect = false;
      throw new Error("Specified native messaging host not found.");
    }
    const port = new FakePort(name);
    this.ports.push(port);
    return port;
  }

  latest() {
    return this.ports[this.ports.length - 1] || null;
  }
}

class FakeTabs {
  constructor(initialTabs = []) {
    this.tabs = new Map();
    this.onRemoved = new FakeEvent();
    this.onUpdated = new FakeEvent();
    this.nextId = 1;
    this.removals = [];
    for (const tab of initialTabs) this.add(tab);
  }

  add({ id, windowId = 1, url = "about:blank", active = false, title = "" } = {}) {
    const tabId = Number.isInteger(id) ? id : this.nextId++;
    if (tabId >= this.nextId) this.nextId = tabId + 1;
    const tab = { id: tabId, windowId, url, active, title };
    this.tabs.set(tabId, tab);
    return { ...tab };
  }

  async get(tabId) {
    const tab = this.tabs.get(tabId);
    if (!tab) throw new Error(`No tab with id: ${tabId}`);
    return { ...tab };
  }

  async update(tabId, props) {
    const tab = this.tabs.get(tabId);
    if (!tab) throw new Error(`No tab with id: ${tabId}`);
    Object.assign(tab, props);
    if (props.active) {
      for (const other of this.tabs.values()) {
        if (other.windowId === tab.windowId && other.id !== tab.id) {
          other.active = false;
        }
      }
    }
    this.onUpdated.emit(tabId, { ...tab }, props);
    return { ...tab };
  }

  async remove(tabId) {
    const tab = this.tabs.get(tabId);
    if (!tab) throw new Error(`No tab with id: ${tabId}`);
    this.tabs.delete(tabId);
    this.removals.push(tabId);
    this.onRemoved.emit(tabId, {
      windowId: tab.windowId,
      isWindowClosing: false,
    });
  }

  async query(queryInfo) {
    return [...this.tabs.values()].filter((tab) => {
      if (queryInfo.active !== undefined && tab.active !== queryInfo.active) return false;
      return true;
    });
  }

  has(tabId) {
    return this.tabs.has(tabId);
  }
}

class FakeWindows {
  constructor() {
    this.updates = [];
  }

  async update(windowId, props) {
    this.updates.push({ windowId, props: { ...props } });
    return { id: windowId, ...props };
  }
}

class FakeRuntime {
  constructor({ id = "test-extension-id" } = {}) {
    this.id = id;
    this.message = new FakeEvent();
    this.sentMessages = [];
  }

  getURL(relativePath = "") {
    return `chrome-extension://${this.id}/${relativePath}`;
  }

  async sendMessage(message) {
    this.sentMessages.push(clone(message));
    return { ok: true };
  }
}

class StubNative {
  constructor(handler = async () => ({ ok: true, sessions: [] })) {
    this.connected = true;
    this.ensureCalls = 0;
    this.requests = [];
    this.handler = handler;
    this.stopped = false;
  }

  ensureConnected() {
    this.ensureCalls += 1;
    return false;
  }

  async request(action, params = {}, options = {}) {
    this.requests.push({
      action,
      params: clone(params),
      timeoutMs: options.timeoutMs,
    });
    return this.handler(action, params, options, this);
  }

  stop() {
    this.stopped = true;
    this.connected = false;
  }

  requestsFor(action) {
    return this.requests.filter((request) => request.action === action);
  }
}

function findSession(overrides = {}) {
  return {
    session_id: "11111111-1111-4111-8111-111111111111",
    name: "Quarterly report.docx",
    original_path: "/home/user/Documents/Quarterly report.docx",
    url: "https://word.cloud.microsoft/document/abc",
    state: "editing",
    tab_id: null,
    error: null,
    last_saved_at: null,
    result_path: null,
    ...overrides,
  };
}

module.exports = {
  EXTENSION_DIR,
  extensionModule,
  FakeEvent,
  FakeStorageArea,
  FakeScheduler,
  FakePort,
  FakeNativeConnector,
  FakeTabs,
  FakeWindows,
  FakeRuntime,
  StubNative,
  findSession,
  clone,
  flushMicrotasks,
  waitFor,
};
