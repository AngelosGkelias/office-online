import { ALARM_NAME, MESSAGE_PREFIX } from "./lib/constants.js";
import { BackgroundController } from "./lib/controller.js";
import { humanError } from "./lib/session.js";
import { ExtensionStore } from "./lib/store.js";

const logger = {
  debug: (...args) => console.debug(...args),
  error: (...args) => console.error(...args),
};

const controller = new BackgroundController({
  store: new ExtensionStore({ storageArea: chrome.storage.local }),
  connectNative: (hostName) => chrome.runtime.connectNative(hostName),
  tabs: chrome.tabs,
  windows: chrome.windows,
  runtime: chrome.runtime,
  logger,
});

function ensureAlarm() {
  try {
    chrome.alarms.create(ALARM_NAME, { periodInMinutes: 1 });
  } catch (error) {
    logger.debug("[office-online] cannot create heartbeat alarm", error);
  }
}

function isTrustedSender(sender) {
  if (!sender) return false;
  if (sender.id && sender.id !== chrome.runtime.id) return false;
  const baseUrl = chrome.runtime.getURL("");
  return !sender.url || sender.url.startsWith(baseUrl);
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (
    !message ||
    typeof message.type !== "string" ||
    !message.type.startsWith(MESSAGE_PREFIX)
  ) {
    return false;
  }
  if (!isTrustedSender(sender)) {
    sendResponse({ ok: false, error: "Untrusted extension message sender." });
    return false;
  }
  controller.handleMessage(message).then(
    (response) => sendResponse(response),
    (error) => sendResponse({ ok: false, error: humanError(error) }),
  );
  return true;
});

chrome.tabs.onRemoved.addListener((tabId, removeInfo) => {
  controller.handleTabRemoved(tabId, removeInfo).catch((error) => {
    logger.error("[office-online] tab close handling failed", error);
  });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (!alarm || alarm.name !== ALARM_NAME) return;
  controller.tick().catch((error) => {
    logger.error("[office-online] heartbeat failed", error);
  });
});

chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm();
  controller.init().catch((error) => {
    logger.error("[office-online] initialization failed", error);
  });
});

chrome.runtime.onStartup.addListener(() => {
  ensureAlarm();
  controller.handleBrowserStartup().catch((error) => {
    logger.error("[office-online] browser startup handling failed", error);
  });
});

ensureAlarm();
controller.init().catch((error) => {
  logger.error("[office-online] initialization failed", error);
});
