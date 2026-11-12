/**
 * Session normalization, state presentation, and URL safety helpers.
 */

const MICROSOFT_HOST_SUFFIXES = Object.freeze([
  "microsoft.com",
  "microsoftonline.com",
  "microsoft365.com",
  "microsoftpersonalcontent.com",
  "office.com",
  "office.net",
  "officeapps.live.com",
  "onedrive.com",
  "sharepoint.com",
  "cloud.microsoft",
  "live.com",
  "live.net",
]);

const MICROSOFT_EXACT_HOSTS = Object.freeze(["1drv.ms"]);

export const SESSION_STATE_PRESENTATION = Object.freeze({
  opening: { label: "Opening", tone: "pending" },
  editing: { label: "Tracking", tone: "ok" },
  saving: { label: "Saving", tone: "pending" },
  saved: { label: "Saved", tone: "ok" },
  error: { label: "Needs retry", tone: "error" },
  conflict: { label: "Conflict", tone: "warn" },
});

function safeString(value) {
  return typeof value === "string" ? value : "";
}

function nullableString(value) {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function nullableInteger(value) {
  return Number.isInteger(value) ? value : null;
}

/**
 * Convert a backend session object into the compact shape used by the
 * extension. Unknown extra fields are ignored, and malformed fields are made
 * safe for display.
 */
export function normalizeSession(raw) {
  if (!raw || typeof raw !== "object") return null;
  const sessionId = safeString(raw.session_id);
  if (!sessionId) return null;
  const state = safeString(raw.state).toLowerCase();
  return {
    session_id: sessionId,
    name: safeString(raw.name) || "Untitled document",
    original_path: safeString(raw.original_path),
    url: nullableString(raw.url),
    state: state || "unknown",
    tab_id: nullableInteger(raw.tab_id),
    error: nullableString(raw.error),
    last_saved_at: nullableString(raw.last_saved_at),
    result_path: nullableString(raw.result_path),
  };
}

export function normalizeSessions(rawSessions) {
  if (!Array.isArray(rawSessions)) return [];
  const seen = new Set();
  const sessions = [];
  for (const raw of rawSessions) {
    const session = normalizeSession(raw);
    if (!session || seen.has(session.session_id)) continue;
    seen.add(session.session_id);
    sessions.push(session);
  }
  return sessions;
}

export function findSession(sessions, sessionId) {
  if (!Array.isArray(sessions)) return null;
  return sessions.find((session) => session.session_id === sessionId) || null;
}

export function statePresentation(state) {
  return (
    SESSION_STATE_PRESENTATION[state] || {
      label: state ? state.charAt(0).toUpperCase() + state.slice(1) : "Unknown",
      tone: "neutral",
    }
  );
}

/**
 * The bridge only navigates to HTTPS URLs on known Microsoft domains. This is
 * deliberately a host allow-list rather than a fragile URL-path match: Office
 * redirect URLs change frequently.
 */
export function isMicrosoftHttpsUrl(value) {
  if (typeof value !== "string" || value.length === 0) return false;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (parsed.protocol !== "https:") return false;
  if (parsed.username || parsed.password) return false;
  if (parsed.port && parsed.port !== "443") return false;
  const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
  if (!host) return false;
  if (MICROSOFT_EXACT_HOSTS.includes(host)) return true;
  return MICROSOFT_HOST_SUFFIXES.some(
    (suffix) => host === suffix || host.endsWith(`.${suffix}`),
  );
}

export function isSessionBridgeable(session) {
  if (!session || !isMicrosoftHttpsUrl(session.url)) return false;
  // Error/conflict sessions with a cloud URL are intentionally bridgeable:
  // reopening an existing path lets the user continue editing, and the
  // backend bind clears recoverable error state. Only saved is terminal.
  return session.state !== "saved";
}

export function humanError(error, fallback = "Unexpected extension error") {
  if (error && typeof error.message === "string" && error.message.trim()) {
    return error.message.trim();
  }
  if (typeof error === "string" && error.trim()) return error.trim();
  return fallback;
}

/** Short "HH:MM" local time label for a stored ISO8601 timestamp. */
export function formatSavedTime(isoString) {
  if (typeof isoString !== "string" || !isoString) return "";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return "";
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${hours}:${minutes}`;
}
