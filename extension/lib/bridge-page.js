import { MessageType } from "./constants.js";
import { isMicrosoftHttpsUrl } from "./session.js";

const SESSION_ID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export class BridgePageError extends Error {
  constructor(message, { retryable = true } = {}) {
    super(message);
    this.name = "BridgePageError";
    this.retryable = retryable;
  }
}

export function parseSessionId(search) {
  let params;
  try {
    params = new URLSearchParams(search || "");
  } catch {
    return null;
  }
  const value = params.get("session");
  if (!value) return null;
  const trimmed = value.trim();
  if (!SESSION_ID_PATTERN.test(trimmed)) return null;
  return trimmed;
}

/**
 * Runs the open.html bridge. The worker owns status polling, duplicate-tab
 * detection, binding, and durable mapping; this page only calls it and performs
 * the resulting navigation/focus/close using injected Chrome operations.
 */
export async function startBridge({
  search,
  sendMessage,
  getCurrentTab,
  focusTab,
  closeTab,
  navigate,
}) {
  const sessionId = parseSessionId(search);
  if (!sessionId) {
    throw new BridgePageError(
      "This link is missing a valid Office Online session identifier.",
      { retryable: false },
    );
  }

  let tab;
  try {
    tab = await getCurrentTab();
  } catch (error) {
    throw new BridgePageError(
      `The current browser tab could not be read: ${error.message || error}`,
    );
  }
  if (!tab || !Number.isInteger(tab.id)) {
    throw new BridgePageError("The current browser tab could not be identified.");
  }

  let response;
  try {
    response = await sendMessage({
      type: MessageType.CLAIM_BRIDGE,
      session_id: sessionId,
      tab_id: tab.id,
    });
  } catch (error) {
    throw new BridgePageError(
      `Could not contact the Office Online extension: ${error.message || error}`,
    );
  }

  if (!response || response.ok !== true) {
    throw new BridgePageError(
      (response && response.error) ||
        "The Office Online service could not prepare this document.",
    );
  }

  const result = response.result;
  if (!result || typeof result !== "object") {
    throw new BridgePageError("The extension returned an invalid bridge response.");
  }

  if (result.action === "redirect") {
    if (!isMicrosoftHttpsUrl(result.url)) {
      throw new BridgePageError(
        "The Office Online service returned an unsafe document URL.",
      );
    }
    navigate(result.url);
    return {
      action: "redirect",
      session: result.session || null,
      sessionId,
    };
  }

  if (result.action === "focus") {
    if (!Number.isInteger(result.existing_tab_id)) {
      throw new BridgePageError(
        "The extension could not identify the tab that already has this document.",
      );
    }
    if (result.existing_tab_id === tab.id) {
      throw new BridgePageError("This document is already open in this tab.");
    }
    try {
      await focusTab(result.existing_tab_id);
    } catch (error) {
      throw new BridgePageError(
        `The tab that already has this document could not be focused: ${error.message || error}`,
      );
    }
    try {
      await closeTab(tab.id);
    } catch {
      // The browser may have removed the duplicate already.
    }
    return {
      action: "focus",
      existingTabId: result.existing_tab_id,
      session: result.session || null,
      sessionId,
    };
  }

  throw new BridgePageError("The extension returned an unknown bridge action.");
}
