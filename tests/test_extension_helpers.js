"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const {
  EXTENSION_DIR,
  extensionModule,
} = require("./extension-test-helpers.cjs");

const sessionModule = extensionModule("lib", "session.js");
const popupViewModelModule = extensionModule("lib", "popup-view.js");
const constantsModule = extensionModule("lib", "constants.js");

function walkFiles(dirPath, result = []) {
  for (const entry of fs.readdirSync(dirPath, { withFileTypes: true })) {
    const fullPath = path.join(dirPath, entry.name);
    if (entry.isDirectory()) walkFiles(fullPath, result);
    else if (entry.isFile()) result.push(fullPath);
  }
  return result;
}

function computeExtensionId(publicKeyDer) {
  const digest = crypto.createHash("sha256").update(publicKeyDer).digest("hex");
  return digest
    .slice(0, 32)
    .split("")
    .map((character) => String.fromCharCode(97 + Number.parseInt(character, 16)))
    .join("");
}

test("manifest key is a valid public RSA key with a matching stable extension id", () => {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(EXTENSION_DIR, "manifest.json"), "utf8"),
  );
  assert.equal(typeof manifest.key, "string");
  const der = Buffer.from(manifest.key, "base64");
  const publicKey = crypto.createPublicKey({
    key: der,
    format: "der",
    type: "spki",
  });
  assert.equal(publicKey.asymmetricKeyType, "rsa");
  assert.equal(publicKey.asymmetricKeyDetails.modulusLength, 2048);

  const extensionId = computeExtensionId(der);
  const storedId = fs.readFileSync(path.join(EXTENSION_DIR, "id.txt"), "utf8").trim();
  assert.match(extensionId, /^[a-p]{32}$/);
  assert.equal(extensionId, storedId);
});

test("extension sources do not contain a private key", () => {
  const files = walkFiles(EXTENSION_DIR);
  for (const file of files) {
    const contents = fs.readFileSync(file, "utf8");
    assert.doesNotMatch(
      contents,
      /-----BEGIN (?:RSA )?PRIVATE KEY-----/,
      `private key material found in ${file}`,
    );
  }
});

test("manifest is MV3, uses the required native host permissions, and has no web injection surface", () => {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(EXTENSION_DIR, "manifest.json"), "utf8"),
  );
  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.background.type, "module");
  assert.equal(manifest.background.service_worker, "background.js");
  assert.equal(manifest.action.default_popup, "popup.html");
  assert.equal(manifest.content_scripts, undefined);
  assert.equal(manifest.host_permissions, undefined);
  assert.equal(manifest.web_accessible_resources, undefined);
  assert.deepEqual(
    [...manifest.permissions].sort(),
    ["alarms", "nativeMessaging", "storage"],
  );
  assert.ok(!manifest.permissions.some((permission) => permission.includes("<all_urls>")));
});

test("extension JavaScript avoids unsafe markup APIs and HTML pages have no inline handlers", () => {
  const jsFiles = walkFiles(EXTENSION_DIR).filter((file) => file.endsWith(".js"));
  for (const file of jsFiles) {
    const source = fs.readFileSync(file, "utf8");
    assert.doesNotMatch(
      source,
      /\binnerHTML\b|\bouterHTML\b|insertAdjacentHTML|document\.write\s*\(/,
      `unsafe markup API in ${file}`,
    );
  }
  const htmlFiles = walkFiles(EXTENSION_DIR).filter((file) => file.endsWith(".html"));
  for (const file of htmlFiles) {
    const html = fs.readFileSync(file, "utf8");
    assert.doesNotMatch(html, /\son[a-z]+\s*=/i, `inline event handler in ${file}`);
    assert.doesNotMatch(html, /<script[^>]+src=["']https?:/i);
    assert.doesNotMatch(html, /<link[^>]+href=["']https?:/i);
  }
});

test("extension README documents the browser-shutdown and background limitations", () => {
  const readme = fs.readFileSync(path.join(EXTENSION_DIR, "README.md"), "utf8");
  assert.match(readme, /crash|shutdown/i);
  assert.match(readme, /background mode/i);
  assert.match(readme, /native-socket EOF|socket EOF/i);
  assert.match(readme, /tab mappings/i);
});

test("session helpers normalize backend data and enforce Microsoft HTTPS redirects", async () => {
  const {
    normalizeSessions,
    normalizeSession,
    isMicrosoftHttpsUrl,
    isSessionBridgeable,
    statePresentation,
  } = await sessionModule;

  const normalized = normalizeSessions([
    {
      session_id: "s1",
      name: "",
      state: "EDITING",
      tab_id: "not-an-int",
      error: 5,
      extra_internal: "ignored",
    },
    { session_id: "s1", name: "duplicate" },
    { session_id: "" },
    null,
  ]);
  assert.equal(normalized.length, 1);
  assert.equal(normalized[0].name, "Untitled document");
  assert.equal(normalized[0].state, "editing");
  assert.equal(normalized[0].tab_id, null);
  assert.equal(normalized[0].error, null);
  assert.equal(normalizeSession(null), null);
  assert.equal(statePresentation("editing").label, "Tracking");
  assert.equal(statePresentation("made-up").label, "Made-up");

  for (const url of [
    "https://word.cloud.microsoft/document/abc",
    "https://www.office.com/launch/word",
    "https://contoso.sharepoint.com/:w:/r/doc.docx",
    "https://view.officeapps.live.com/op/view.aspx?src=x",
    "https://login.microsoftonline.com/common/oauth2/authorize",
    "https://1drv.ms/w/s!abc",
    "https://onedrive.live.com/:w:/r/doc.docx",
    "https://contoso.onedrive.com/:x:/r/doc.xlsx",
  ]) {
    assert.equal(isMicrosoftHttpsUrl(url), true, url);
    assert.equal(isSessionBridgeable({ url, state: "editing" }), true, url);
  }
  for (const url of [
    "http://word.cloud.microsoft/document/abc",
    "javascript:alert(1)",
    "https://microsoft.com.evil.test/document",
    "https://evil.test/?next=https://office.com",
    "https://user:pass@office.com/doc",
    "https://office.com:8443/doc",
    "data:text/html,hi",
    "",
    null,
  ]) {
    assert.equal(isMicrosoftHttpsUrl(url), false, String(url));
  }
  assert.equal(isSessionBridgeable({ url: "https://office.com/x", state: "saved" }), false);
  assert.equal(isSessionBridgeable({ url: "https://office.com/x", state: "error" }), true);
  assert.equal(isSessionBridgeable({ url: "https://office.com/x", state: "conflict" }), true);
  assert.equal(isSessionBridgeable({ url: null, state: "error" }), false);
});

test("popup view model renders status, errors, actions, and offline pending closures safely", async () => {
  const { buildPopupViewModel } = await popupViewModelModule;
  const localSaved = new Date(2026, 0, 2, 13, 5, 0).toISOString();
  const state = {
    connected: true,
    lastError: null,
    sessions: [
      {
        session_id: "s-editing",
        name: "Deck.pptx",
        original_path: "/home/user/Deck.pptx",
        state: "editing",
        error: null,
        last_saved_at: localSaved,
        result_path: "/home/user/Deck.pptx",
      },
      {
        session_id: "s-error",
        name: "Broken.xlsx",
        original_path: "/home/user/Broken.xlsx",
        state: "error",
        error: "Upload interrupted",
      },
      {
        session_id: "s-saving",
        name: "Saving.docx",
        original_path: "/home/user/Saving.docx",
        state: "saving",
      },
      {
        session_id: "s-saved",
        name: "Finished.docx",
        original_path: "/home/user/Finished.docx",
        state: "saved",
        result_path: "/home/user/Finished.docx",
      },
    ],
    pendingClosures: [
      {
        id: "closure-1",
        session_id: "s-offline",
        tab_id: 9,
        name: "Offline.docx",
        original_path: "/home/user/Offline.docx",
        attempts: 2,
        last_error: "native host unavailable",
      },
    ],
  };

  const view = buildPopupViewModel(state);
  assert.equal(view.connection.label, "Connected");
  assert.equal(view.connection.tone, "ok");
  assert.equal(view.sessions.length, 3);
  assert.equal(
    view.sessions.some((item) => item.sessionId === "s-saved"),
    false,
  );
  assert.equal(view.sessions[0].sessionId, "s-error");
  assert.equal(view.sessions[0].canSave, true);
  assert.equal(view.sessions[0].canRetry, true);
  assert.equal(view.sessions[0].stateLabel, "Needs retry");
  assert.equal(view.sessions[0].error, "Upload interrupted");
  const editing = view.sessions.find((item) => item.sessionId === "s-editing");
  assert.equal(editing.canSave, true);
  assert.equal(editing.canRetry, false);
  assert.equal(editing.lastSavedLabel, "13:05");
  assert.equal(editing.resultPath, "/home/user/Deck.pptx");
  const saving = view.sessions.find((item) => item.sessionId === "s-saving");
  assert.equal(saving.canSave, false);
  assert.match(saving.saveHint, /already in progress/i);
  assert.equal(view.pendingOnly.length, 1);
  assert.equal(view.pendingOnly[0].name, "Offline.docx");
  assert.equal(view.pendingOnly[0].canSave, false);
  assert.match(view.pendingOnly[0].stateLabel, /Final save queued/);
  assert.equal(view.isEmpty, false);
});

test("popup view model shows offline and empty states without exposing markup", async () => {
  const { buildPopupViewModel } = await popupViewModelModule;
  const offline = buildPopupViewModel({
    connected: false,
    lastError: "Native host has exited",
    sessions: [
      {
        session_id: "s1",
        name: "<img src=x onerror=alert(1)>",
        original_path: "/tmp/x.docx",
        state: "editing",
      },
    ],
    pendingClosures: [],
  });
  assert.equal(offline.connection.label, "Service offline");
  assert.equal(offline.connection.tone, "error");
  assert.equal(offline.sessions[0].name, "<img src=x onerror=alert(1)>");
  assert.equal(offline.sessions[0].canSave, false);
  assert.match(offline.sessions[0].saveHint, /offline/i);

  const empty = buildPopupViewModel({ connected: true, sessions: [] });
  assert.equal(empty.isEmpty, true);
  const disconnected = buildPopupViewModel(null);
  assert.equal(disconnected.connection.label, "Connecting");
  assert.equal(disconnected.isEmpty, true);
});

test("constants match the contract's native host and message storage keys", async () => {
  const { NATIVE_HOST, STORAGE_KEYS, MessageType } = await constantsModule;
  assert.equal(NATIVE_HOST, "org.local.office_online");
  assert.match(STORAGE_KEYS.MAPPINGS, /sessionTabs/);
  assert.match(MessageType.CLAIM_BRIDGE, /^office-online:/);
});
