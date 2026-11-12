import {
  formatSavedTime,
  statePresentation,
} from "./session.js";

const STATE_ORDER = Object.freeze({
  error: 0,
  conflict: 1,
  saving: 2,
  editing: 3,
  opening: 4,
  saved: 5,
});

function connectionView(state, pendingCount) {
  const connected = Boolean(state && state.connected);
  const lastError =
    state && typeof state.lastError === "string" ? state.lastError : "";
  if (connected && lastError) {
    return {
      label: "Connected",
      detail: lastError,
      tone: "warn",
      canAct: true,
    };
  }
  if (connected) {
    return {
      label: "Connected",
      detail:
        pendingCount > 0
          ? `${pendingCount} final save${pendingCount === 1 ? "" : "s"} queued`
          : "Local Office Online service",
      tone: "ok",
      canAct: true,
    };
  }
  if (lastError) {
    return {
      label: "Service offline",
      detail: lastError,
      tone: "error",
      canAct: false,
    };
  }
  return {
    label: "Connecting",
    detail: "Waiting for the local Office Online service",
    tone: "pending",
    canAct: false,
  };
}

function queuedItem(closure) {
  return {
    id: `pending:${closure.id}`,
    sessionId: closure.session_id,
    name: closure.name || "Document session",
    path: closure.original_path || "",
    stateLabel: "Final save queued",
    tone: "pending",
    error: closure.last_error || "",
    lastSavedLabel: "",
    resultPath: "",
    canSave: false,
    canRetry: false,
    saveHint: "",
    retryHint: "",
    pendingClosure: closure,
    queuedOnly: true,
  };
}

/**
 * Pure view model used by popup.js and unit tests. All values are plain strings
 * or booleans so rendering never needs to interpret backend data as markup.
 */
export function buildPopupViewModel(state) {
  const safeState = state && typeof state === "object" ? state : {};
  const allSessions = Array.isArray(safeState.sessions)
    ? safeState.sessions
    : [];
  // "saved" is terminal: the daemon retains records, but the compact popup
  // should not accumulate a growing history of finished sessions.
  const sessions = allSessions.filter((session) => session.state !== "saved");
  const pendingClosures = Array.isArray(safeState.pendingClosures)
    ? safeState.pendingClosures
    : [];
  const connected = Boolean(safeState.connected);

  const pendingBySession = new Map();
  for (const closure of pendingClosures) {
    if (!pendingBySession.has(closure.session_id)) {
      pendingBySession.set(closure.session_id, closure);
    }
  }

  const items = sessions.map((session) => {
    const presentation = statePresentation(session.state);
    const pending = pendingBySession.get(session.session_id) || null;
    const canSave =
      connected &&
      (session.state === "editing" ||
        session.state === "error" ||
        session.state === "conflict");
    const canRetry = connected && session.state === "error";
    let saveHint = "";
    if (!connected) saveHint = "The local Office Online service is offline.";
    else if (session.state === "saving") saveHint = "Saving is already in progress.";
    else if (session.state === "opening") saveHint = "The document is still opening.";
    else if (session.state === "saved") saveHint = "This session has finished.";
    else if (!canSave) saveHint = "Saving is not available for this session.";

    return {
      id: `session:${session.session_id}`,
      sessionId: session.session_id,
      name: session.name || "Untitled document",
      path: session.original_path || "",
      state: session.state,
      stateLabel: presentation.label,
      tone: presentation.tone,
      error: session.error || "",
      lastSavedLabel: formatSavedTime(session.last_saved_at),
      resultPath: session.result_path || "",
      canSave,
      canRetry,
      saveHint,
      retryHint: connected
        ? ""
        : "The local Office Online service is offline.",
      pendingClosure: pending,
      queuedOnly: false,
      sortRank:
        session.state in STATE_ORDER ? STATE_ORDER[session.state] : 6,
    };
  });

  items.sort((a, b) => {
    if (a.sortRank !== b.sortRank) return a.sortRank - b.sortRank;
    return a.name.localeCompare(b.name);
  });

  const sessionIds = new Set(sessions.map((session) => session.session_id));
  const pendingOnly = pendingClosures
    .filter((closure) => !sessionIds.has(closure.session_id))
    .map(queuedItem)
    .sort((a, b) => a.name.localeCompare(b.name));

  return {
    connection: connectionView(safeState, pendingClosures.length),
    sessions: items,
    pendingOnly,
    pendingCount: pendingClosures.length,
    isEmpty: items.length === 0 && pendingOnly.length === 0,
    sessionsSavedAt: safeState.sessionsSavedAt || null,
  };
}
