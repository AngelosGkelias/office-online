"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { extensionModule } = require("./extension-test-helpers.cjs");

const bridgeModule = extensionModule("lib", "bridge-page.js");
const constantsModule = extensionModule("lib", "constants.js");

const SESSION_ID = "11111111-1111-4111-8111-111111111111";

test("bridge parses only a well-formed session query", async () => {
  const { parseSessionId } = await bridgeModule;
  assert.equal(parseSessionId(`?session=${SESSION_ID}`), SESSION_ID);
  assert.equal(parseSessionId(`?foo=1&session=${SESSION_ID.toUpperCase()}`), SESSION_ID.toUpperCase());
  assert.equal(parseSessionId("?session=not-a-uuid"), null);
  assert.equal(parseSessionId("?session=../../etc/passwd"), null);
  assert.equal(parseSessionId(`?session=${SESSION_ID}&session=other`), SESSION_ID);
  assert.equal(parseSessionId(""), null);
  assert.equal(parseSessionId(null), null);
});

test("bridge obtains the current tab, claims both ids via the worker, then redirects", async () => {
  const { startBridge } = await bridgeModule;
  const { MessageType } = await constantsModule;
  const messages = [];
  const navigations = [];
  const result = await startBridge({
    search: `?session=${SESSION_ID}`,
    sendMessage: async (message) => {
      messages.push(message);
      return {
        ok: true,
        result: {
          action: "redirect",
          url: "https://word.cloud.microsoft/document/abc",
          session: { session_id: SESSION_ID, name: "Report.docx" },
        },
      };
    },
    getCurrentTab: async () => ({ id: 17, windowId: 2 }),
    focusTab: async () => {
      throw new Error("should not focus");
    },
    closeTab: async () => {
      throw new Error("should not close");
    },
    navigate: (url) => navigations.push(url),
  });

  assert.equal(messages.length, 1);
  assert.deepEqual(messages[0], {
    type: MessageType.CLAIM_BRIDGE,
    session_id: SESSION_ID,
    tab_id: 17,
  });
  assert.deepEqual(navigations, ["https://word.cloud.microsoft/document/abc"]);
  assert.equal(result.action, "redirect");
  assert.equal(result.session.name, "Report.docx");
});

test("bridge focuses an existing mapped tab and closes the duplicate after a focus result", async () => {
  const { startBridge } = await bridgeModule;
  const focused = [];
  const closed = [];
  const navigations = [];
  const result = await startBridge({
    search: `?session=${SESSION_ID}`,
    sendMessage: async () => ({
      ok: true,
      result: {
        action: "focus",
        existing_tab_id: 4,
        session: { session_id: SESSION_ID },
      },
    }),
    getCurrentTab: async () => ({ id: 9, windowId: 2 }),
    focusTab: async (tabId) => focused.push(tabId),
    closeTab: async (tabId) => closed.push(tabId),
    navigate: (url) => navigations.push(url),
  });

  assert.deepEqual(focused, [4]);
  assert.deepEqual(closed, [9]);
  assert.deepEqual(navigations, []);
  assert.equal(result.action, "focus");
  assert.equal(result.existingTabId, 4);
});

test("bridge tolerates a duplicate tab already being closed after focus", async () => {
  const { startBridge } = await bridgeModule;
  const result = await startBridge({
    search: `?session=${SESSION_ID}`,
    sendMessage: async () => ({
      ok: true,
      result: { action: "focus", existing_tab_id: 4, session: null },
    }),
    getCurrentTab: async () => ({ id: 9, windowId: 2 }),
    focusTab: async () => {},
    closeTab: async () => {
      throw new Error("No tab with id 9");
    },
    navigate: () => {
      throw new Error("should not navigate");
    },
  });
  assert.equal(result.action, "focus");
});

test("bridge rejects unsafe redirect targets returned by a faulty worker", async () => {
  const { startBridge, BridgePageError } = await bridgeModule;
  const navigations = [];
  await assert.rejects(
    startBridge({
      search: `?session=${SESSION_ID}`,
      sendMessage: async () => ({
        ok: true,
        result: { action: "redirect", url: "javascript:alert(1)" },
      }),
      getCurrentTab: async () => ({ id: 9, windowId: 2 }),
      focusTab: async () => {},
      closeTab: async () => {},
      navigate: (url) => navigations.push(url),
    }),
    (error) => {
      assert.ok(error instanceof BridgePageError);
      assert.match(error.message, /unsafe/i);
      return true;
    },
  );
  assert.deepEqual(navigations, []);
});

test("bridge reports worker errors and marks only invalid links as non-retryable", async () => {
  const { startBridge, BridgePageError } = await bridgeModule;

  const invalid = await startBridge({
    search: "?session=bad",
    sendMessage: async () => {
      throw new Error("must not be called");
    },
    getCurrentTab: async () => ({ id: 1 }),
    focusTab: async () => {},
    closeTab: async () => {},
    navigate: () => {},
  }).then(
    () => null,
    (error) => error,
  );
  assert.ok(invalid instanceof BridgePageError);
  assert.equal(invalid.retryable, false);

  const backendError = await startBridge({
    search: `?session=${SESSION_ID}`,
    sendMessage: async () => ({ ok: false, error: "Session expired." }),
    getCurrentTab: async () => ({ id: 1 }),
    focusTab: async () => {},
    closeTab: async () => {},
    navigate: () => {},
  }).then(
    () => null,
    (error) => error,
  );
  assert.equal(backendError.message, "Session expired.");
  assert.equal(backendError.retryable, true);

  const noTab = await startBridge({
    search: `?session=${SESSION_ID}`,
    sendMessage: async () => ({ ok: true, result: {} }),
    getCurrentTab: async () => null,
    focusTab: async () => {},
    closeTab: async () => {},
    navigate: () => {},
  }).then(
    () => null,
    (error) => error,
  );
  assert.match(noTab.message, /tab could not be identified/i);
});

test("bridge converts a worker messaging failure into a retryable human error", async () => {
  const { startBridge } = await bridgeModule;
  const error = await startBridge({
    search: `?session=${SESSION_ID}`,
    sendMessage: async () => {
      throw new Error("Could not establish connection. Receiving end does not exist.");
    },
    getCurrentTab: async () => ({ id: 1 }),
    focusTab: async () => {},
    closeTab: async () => {},
    navigate: () => {},
  }).then(
    () => null,
    (caught) => caught,
  );
  assert.match(error.message, /Could not contact the Office Online extension/);
  assert.equal(error.retryable, true);
});
