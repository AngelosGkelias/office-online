"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  extensionModule,
  FakeStorageArea,
  FakeScheduler,
} = require("./extension-test-helpers.cjs");

const storeModule = extensionModule("lib", "store.js");
const constantsModule = extensionModule("lib", "constants.js");

async function makeStore({ initial, scheduler, makeClosureId } = {}) {
  const { ExtensionStore } = await storeModule;
  const { STORAGE_KEYS } = await constantsModule;
  const storage = new FakeStorageArea(initial);
  const clock = scheduler || new FakeScheduler();
  let nextClosureId = 1;
  const store = new ExtensionStore({
    storageArea: storage,
    now: () => new Date(clock.nowMs),
    makeClosureId:
      makeClosureId || (() => `closure-test-${nextClosureId++}`),
  });
  return { store, storage, clock, STORAGE_KEYS };
}

test("store persists tab mappings across store instances", async () => {
  const { store, storage, STORAGE_KEYS } = await makeStore();
  await store.setMapping("session-a", {
    tab_id: 42,
    session_id: "session-a",
    bound_at: "2026-01-01T00:00:00.000Z",
  });
  const { ExtensionStore } = await storeModule;
  const second = new ExtensionStore({ storageArea: storage });
  const mapping = await second.getMapping("session-a");
  assert.equal(mapping.tab_id, 42);
  assert.equal(mapping.session_id, "session-a");
  assert.ok(storage.data[STORAGE_KEYS.MAPPINGS]);
});

test("store serializes concurrent read-modify-write updates without losing mappings", async () => {
  const { store } = await makeStore();
  await Promise.all(
    Array.from({ length: 20 }, (_, index) =>
      store.setMapping(`session-${index}`, {
        tab_id: index + 1,
        session_id: `session-${index}`,
      }),
    ),
  );
  const mappings = await store.getMappings();
  assert.equal(Object.keys(mappings).length, 20);
  assert.equal(mappings["session-19"].tab_id, 20);
});

test("store deletes a mapping only when the tab still matches", async () => {
  const { store } = await makeStore();
  await store.setMapping("session-a", { tab_id: 7 });
  assert.equal(
    await store.deleteMappingIf("session-a", (mapping) => mapping.tab_id === 8),
    false,
  );
  assert.equal((await store.getMapping("session-a")).tab_id, 7);
  assert.equal(
    await store.deleteMappingIf("session-a", (mapping) => mapping.tab_id === 7),
    true,
  );
  assert.equal(await store.getMapping("session-a"), null);
});

test("store durably queues deduplicated offline tab closures", async () => {
  const { store } = await makeStore();
  const candidate = {
    session_id: "session-a",
    tab_id: 3,
    name: "Report.docx",
    original_path: "/tmp/Report.docx",
    queued_at: "2026-01-01T00:00:00.000Z",
  };
  const first = await store.enqueueTabClosure(candidate);
  const second = await store.enqueueTabClosure(candidate);
  assert.equal(first.id, second.id);
  assert.equal((await store.getPendingClosures()).length, 1);

  const updated = await store.markTabClosureAttempt(first.id, "native offline");
  assert.equal(updated.attempts, 1);
  assert.equal(updated.last_error, "native offline");
  const pending = await store.getPendingClosures();
  assert.equal(pending[0].attempts, 1);

  assert.equal(await store.removeTabClosure(first.id), true);
  assert.equal((await store.getPendingClosures()).length, 0);
});

test("store keeps distinct offline closures for distinct tabs and sessions", async () => {
  const { store, storage } = await makeStore();
  await Promise.all([
    store.enqueueTabClosure({ session_id: "a", tab_id: 1, name: "A" }),
    store.enqueueTabClosure({ session_id: "b", tab_id: 2, name: "B" }),
  ]);
  assert.equal((await store.getPendingClosures()).length, 2);

  const { ExtensionStore } = await storeModule;
  const second = new ExtensionStore({ storageArea: storage });
  const pending = await second.getPendingClosures();
  assert.deepEqual(
    pending.map((item) => item.session_id).sort(),
    ["a", "b"],
  );
});

test("store caches normalized sessions for popup startup", async () => {
  const { store } = await makeStore();
  const sessions = [
    {
      session_id: "session-a",
      name: "Report.docx",
      state: "editing",
      original_path: "/tmp/Report.docx",
    },
  ];
  await store.setSessions(sessions, "2026-01-01T12:00:00.000Z");
  const cache = await store.getSessionsCache();
  assert.equal(cache.saved_at, "2026-01-01T12:00:00.000Z");
  assert.deepEqual(cache.sessions, sessions);
});

test("store ignores malformed persisted values instead of crashing", async () => {
  const { STORAGE_KEYS } = await constantsModule;
  const { store } = await makeStore({
    initial: {
      [STORAGE_KEYS.MAPPINGS]: [],
      [STORAGE_KEYS.PENDING_CLOSURES]: "bad",
      [STORAGE_KEYS.SESSIONS]: { sessions: "bad" },
    },
  });
  assert.deepEqual(await store.getMappings(), {});
  assert.deepEqual(await store.getPendingClosures(), []);
  assert.equal(await store.getSessionsCache(), null);
});
