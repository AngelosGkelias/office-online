import { MessageType } from "./lib/constants.js";
import { buildPopupViewModel } from "./lib/popup-view.js";

const elements = {
  refresh: document.getElementById("refresh"),
  connection: document.getElementById("connection"),
  connectionLabel: document.getElementById("connection-label"),
  connectionDetail: document.getElementById("connection-detail"),
  banner: document.getElementById("banner"),
  list: document.getElementById("session-list"),
  empty: document.getElementById("empty"),
  version: document.getElementById("version"),
};

let lastRevision = -1;
let hasAppliedState = false;
let isLoading = false;
let bannerTimer = null;

function createElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setBanner(message, tone = "info") {
  if (bannerTimer !== null) {
    clearTimeout(bannerTimer);
    bannerTimer = null;
  }
  elements.banner.textContent = message;
  elements.banner.className = `banner banner--${tone}`;
  elements.banner.hidden = false;
  bannerTimer = setTimeout(() => {
    bannerTimer = null;
    elements.banner.hidden = true;
  }, 5000);
}

function clearBanner() {
  if (bannerTimer !== null) {
    clearTimeout(bannerTimer);
    bannerTimer = null;
  }
  elements.banner.hidden = true;
}

function renderConnection(connection) {
  elements.connection.className = `connection connection--${connection.tone}`;
  elements.connectionLabel.textContent = connection.label;
  elements.connectionDetail.textContent = connection.detail;
  elements.connectionDetail.title = connection.detail;
}

function renderSession(item) {
  const card = createElement(
    "li",
    `session-card${item.queuedOnly ? " session-card--queued" : ""}`,
  );
  card.dataset.sessionId = item.sessionId;

  const top = createElement("div", "session-top");
  const name = createElement("span", "session-name", item.name);
  name.title = item.name;
  const pill = createElement(
    "span",
    `status-pill status-pill--${item.tone}`,
    item.stateLabel,
  );
  top.append(name, pill);
  card.append(top);

  if (item.path) {
    const path = createElement("div", "session-path", item.path);
    path.title = item.path;
    card.append(path);
  }

  if (item.error) {
    const error = createElement("p", "session-error", item.error);
    card.append(error);
  }

  const metaBits = [];
  if (item.lastSavedLabel) {
    metaBits.push(createElement("span", "meta-item", `Saved ${item.lastSavedLabel}`));
  }
  if (item.resultPath) {
    const result = createElement("span", "meta-item", `Copy: ${item.resultPath}`);
    result.title = item.resultPath;
    metaBits.push(result);
  }
  if (item.pendingClosure) {
    const badge = createElement(
      "span",
      "meta-badge",
      `Final save queued${item.pendingClosure.attempts ? ` (${item.pendingClosure.attempts} attempt${item.pendingClosure.attempts === 1 ? "" : "s"})` : ""}`,
    );
    if (item.pendingClosure.last_error) {
      badge.title = item.pendingClosure.last_error;
    } else if (item.pendingClosure.queued_at) {
      badge.title = `Queued ${item.pendingClosure.queued_at}`;
    }
    metaBits.push(badge);
  }
  if (metaBits.length > 0) {
    const meta = createElement("div", "session-meta");
    meta.append(...metaBits);
    card.append(meta);
  }

  const actions = createElement("div", "session-actions");
  if (
    !item.queuedOnly &&
    (item.state === "editing" ||
      item.state === "error" ||
      item.state === "conflict")
  ) {
    const save = createElement("button", "button button--primary", "Save to computer");
    save.type = "button";
    save.disabled = !item.canSave;
    if (item.saveHint) save.title = item.saveHint;
    save.addEventListener("click", () => {
      runAction(MessageType.SAVE, item.sessionId, save, "Saving…");
    });
    actions.append(save);
  }
  if (!item.queuedOnly && item.state === "error") {
    const retry = createElement("button", "button", "Retry");
    retry.type = "button";
    retry.disabled = !item.canRetry;
    if (item.retryHint) retry.title = item.retryHint;
    retry.addEventListener("click", () => {
      runAction(MessageType.RETRY, item.sessionId, retry, "Retrying…");
    });
    actions.append(retry);
  }
  if (actions.childElementCount > 0) card.append(actions);
  return card;
}

function render(view) {
  renderConnection(view.connection);
  const items = [...view.sessions, ...view.pendingOnly].map(renderSession);
  elements.list.replaceChildren(...items);
  elements.empty.hidden = !view.isEmpty;
}

function applyState(state) {
  if (!state || typeof state !== "object") return;
  const revision = Number.isInteger(state.revision) ? state.revision : 0;
  if (hasAppliedState && revision < lastRevision) return;
  lastRevision = revision;
  hasAppliedState = true;
  render(buildPopupViewModel(state));
}

async function sendMessage(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response || typeof response !== "object") {
    throw new Error("The extension worker returned no response.");
  }
  return response;
}

async function load({ quiet = false, refresh = false } = {}) {
  if (isLoading) return;
  isLoading = true;
  elements.refresh.classList.add("is-loading");
  try {
    const response = await sendMessage({
      type: refresh ? MessageType.REFRESH : MessageType.GET_STATE,
    });
    if (!response.ok) {
      throw new Error(response.error || "The extension worker could not load state.");
    }
    applyState(response.state);
  } catch (error) {
    if (!quiet) {
      setBanner(
        `Cannot reach the Office Online extension worker: ${error.message || error}`,
        "error",
      );
    }
  } finally {
    isLoading = false;
    elements.refresh.classList.remove("is-loading");
  }
}

async function runAction(type, sessionId, button, busyLabel) {
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = busyLabel;
  try {
    const response = await sendMessage({ type, session_id: sessionId });
    if (!response.ok) {
      throw new Error(response.error || "The Office Online service rejected the request.");
    }
    if (response.state) applyState(response.state);
    setBanner(
      type === MessageType.SAVE
        ? "Save queued. This session stays tracked."
        : "Retry queued.",
      "success",
    );
    setTimeout(() => {
      load({ quiet: true }).catch(() => {});
    }, 800);
  } catch (error) {
    setBanner(error.message || String(error), "error");
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

elements.refresh.addEventListener("click", () => {
  clearBanner();
  load({ refresh: true }).catch(() => {});
});

chrome.runtime.onMessage.addListener((message) => {
  if (
    message &&
    message.type === MessageType.STATE_CHANGED &&
    message.state
  ) {
    applyState(message.state);
  }
});

try {
  elements.version.textContent = `v${chrome.runtime.getManifest().version}`;
} catch {
  elements.version.textContent = "";
}

load().catch(() => {});
setInterval(() => {
  load({ quiet: true }).catch(() => {});
}, 4000);
