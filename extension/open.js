import { parseSessionId, startBridge } from "./lib/bridge-page.js";

const elements = {
  card: document.querySelector(".bridge-card"),
  spinner: document.getElementById("spinner"),
  title: document.getElementById("bridge-title"),
  message: document.getElementById("bridge-message"),
  session: document.getElementById("bridge-session"),
  retry: document.getElementById("retry"),
};

const sessionId = parseSessionId(window.location.search);
if (elements.session) {
  elements.session.textContent = sessionId
    ? `Session ${sessionId.slice(0, 8)}`
    : "Session identifier missing or invalid";
}

async function getCurrentTab() {
  const current = await chrome.tabs.getCurrent();
  if (current) return current;
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs && tabs[0] ? tabs[0] : null;
}

async function focusTab(tabId) {
  const tab = await chrome.tabs.get(tabId);
  await chrome.tabs.update(tabId, { active: true });
  if (tab && Number.isInteger(tab.windowId)) {
    try {
      await chrome.windows.update(tab.windowId, { focused: true });
    } catch {
      // The tab is active even if the window API is unavailable.
    }
  }
}

function setBusy() {
  elements.card.classList.remove("is-error");
  elements.spinner.hidden = false;
  elements.title.textContent = "Opening document";
  elements.message.textContent = "Waiting for the local Office Online service…";
  elements.retry.hidden = true;
  elements.retry.disabled = true;
}

function setError(error) {
  elements.card.classList.add("is-error");
  elements.spinner.hidden = true;
  elements.title.textContent = "Could not open document";
  elements.message.textContent = error.message || String(error);
  const retryable = error.retryable !== false;
  elements.retry.hidden = !retryable;
  elements.retry.disabled = false;
}

async function run() {
  setBusy();
  try {
    const result = await startBridge({
      search: window.location.search,
      sendMessage: (message) => chrome.runtime.sendMessage(message),
      getCurrentTab,
      focusTab,
      closeTab: (tabId) => chrome.tabs.remove(tabId),
      navigate: (url) => window.location.replace(url),
    });
    if (result.action === "focus") {
      elements.spinner.hidden = true;
      elements.title.textContent = "Document already open";
      elements.message.textContent =
        "This document is already open in another tab. That tab has been focused.";
      elements.retry.hidden = true;
    }
  } catch (error) {
    setError(error);
  }
}

elements.retry.addEventListener("click", () => {
  run().catch((error) => setError(error));
});

run().catch((error) => setError(error));
