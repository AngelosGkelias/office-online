"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const {
  EXTENSION_DIR,
  extensionModule,
  flushMicrotasks,
  waitFor,
} = require("./extension-test-helpers.cjs");

class FakeClassList {
  constructor(element) {
    this.element = element;
  }

  add(...tokens) {
    for (const token of tokens) this.element._classes.add(token);
  }

  remove(...tokens) {
    for (const token of tokens) this.element._classes.delete(token);
  }

  contains(token) {
    return this.element._classes.has(token);
  }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.listeners = new Map();
    this._classes = new Set();
    this.classList = new FakeClassList(this);
    this.textContent = "";
    this.className = "";
    this.title = "";
    this.type = "";
    this.disabled = false;
    this.hidden = false;
  }

  set className(value) {
    this._classes = new Set(
      String(value || "")
        .split(/\s+/)
        .filter(Boolean),
    );
  }

  get className() {
    return [...this._classes].join(" ");
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    this.children = [...nodes];
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  dispatch(type) {
    for (const listener of this.listeners.get(type) || []) listener({ type });
  }

  get childElementCount() {
    return this.children.length;
  }
}

function makeDocument() {
  const byId = new Map();
  for (const id of [
    "refresh",
    "connection",
    "connection-label",
    "connection-detail",
    "banner",
    "session-list",
    "empty",
    "version",
  ]) {
    byId.set(id, new FakeElement(id === "session-list" ? "ul" : "div"));
  }
  return {
    getElementById: (id) => byId.get(id) || null,
    createElement: (tagName) => new FakeElement(tagName),
    byId,
  };
}

function collectElements(root, predicate, result = []) {
  if (predicate(root)) result.push(root);
  for (const child of root.children) {
    if (child instanceof FakeElement) collectElements(child, predicate, result);
  }
  return result;
}

function buttonLabels(root) {
  return collectElements(root, (element) => element.tagName === "BUTTON").map(
    (element) => element.textContent,
  );
}

test("popup renders worker state through safe DOM nodes and honors revision ordering", async () => {
  const { MessageType } = await extensionModule("lib", "constants.js");
  const documentMock = makeDocument();
  const sentMessages = [];
  const messageListeners = [];
  const initialState = {
    revision: 1,
    connected: true,
    lastError: null,
    sessions: [
      {
        session_id: "s-edit",
        name: "<Report & Deck>.docx",
        original_path: "/home/user/<Report & Deck>.docx",
        state: "editing",
        error: null,
        last_saved_at: null,
        result_path: null,
      },
      {
        session_id: "s-error",
        name: "Broken.xlsx",
        original_path: "/home/user/Broken.xlsx",
        state: "error",
        error: "Upload interrupted",
      },
    ],
    pendingClosures: [],
    mappings: {},
    sessionsSavedAt: null,
  };
  const chromeMock = {
    runtime: {
      id: "test-extension-id",
      getManifest: () => ({ version: "9.9.9" }),
      async sendMessage(message) {
        sentMessages.push(message);
        return { ok: true, state: initialState };
      },
      onMessage: {
        addListener(listener) {
          messageListeners.push(listener);
        },
      },
    },
  };

  const previousDocument = global.document;
  const previousChrome = global.chrome;
  const previousSetInterval = global.setInterval;
  global.document = documentMock;
  global.chrome = chromeMock;
  global.setInterval = () => 1;

  try {
    const popupUrl = pathToFileURL(
      path.join(EXTENSION_DIR, "popup.js"),
    ).href;
    await import(`${popupUrl}?test=${Date.now()}`);
    await waitFor(
      () => documentMock.byId.get("session-list").children.length === 2,
      { label: "popup initial render" },
    );

    assert.equal(sentMessages[0].type, MessageType.GET_STATE);
    assert.equal(
      documentMock.byId.get("connection-label").textContent,
      "Connected",
    );
    assert.equal(documentMock.byId.get("empty").hidden, true);
    assert.equal(documentMock.byId.get("version").textContent, "v9.9.9");

    const list = documentMock.byId.get("session-list");
    const labels = buttonLabels(list);
    assert.deepEqual(labels.sort(), ["Retry", "Save to computer", "Save to computer"]);
    const allText = collectElements(list, () => true)
      .map((element) => element.textContent)
      .join(" ");
    assert.match(allText, /<Report & Deck>\.docx/);
    assert.match(allText, /Upload interrupted/);
    assert.match(allText, /Needs retry/);

    assert.equal(messageListeners.length, 1);
    messageListeners[0]({
      type: MessageType.STATE_CHANGED,
      state: {
        revision: 2,
        connected: false,
        lastError: "Native host has exited",
        sessions: initialState.sessions,
        pendingClosures: [],
        mappings: {},
      },
    });
    await flushMicrotasks();
    assert.equal(
      documentMock.byId.get("connection-label").textContent,
      "Service offline",
    );
    const offlineButtons = collectElements(
      list,
      (element) => element.tagName === "BUTTON",
    );
    assert.equal(offlineButtons.length, 3);
    assert.equal(offlineButtons.every((button) => button.disabled), true);

    // A late response from the earlier revision must not replace newer state.
    messageListeners[0]({
      type: MessageType.STATE_CHANGED,
      state: initialState,
    });
    await flushMicrotasks();
    assert.equal(
      documentMock.byId.get("connection-label").textContent,
      "Service offline",
    );
  } finally {
    global.document = previousDocument;
    global.chrome = previousChrome;
    global.setInterval = previousSetInterval;
  }
});
