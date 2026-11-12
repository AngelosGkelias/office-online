/**
 * Shared constants for the Office Online Local extension.
 *
 * Keep this module dependency-free: it is imported by the service worker,
 * popup, bridge page, and Node tests.
 */

export const NATIVE_HOST = "org.local.office_online";

export const MESSAGE_PREFIX = "office-online:";

export const MessageType = Object.freeze({
  GET_STATE: `${MESSAGE_PREFIX}get-state`,
  REFRESH: `${MESSAGE_PREFIX}refresh`,
  SAVE: `${MESSAGE_PREFIX}save`,
  RETRY: `${MESSAGE_PREFIX}retry`,
  CLAIM_BRIDGE: `${MESSAGE_PREFIX}claim-bridge`,
  STATE_CHANGED: `${MESSAGE_PREFIX}state-changed`,
});

export const STORAGE_KEYS = Object.freeze({
  MAPPINGS: "officeOnline.sessionTabs.v1",
  PENDING_CLOSURES: "officeOnline.pendingTabClosures.v1",
  SESSIONS: "officeOnline.sessionsCache.v1",
  META: "officeOnline.meta.v1",
});

export const ALARM_NAME = "office-online:heartbeat";

/**
 * Delay between native connection attempts (milliseconds). The first retry is
 * deliberately shorter than the daemon's disconnect grace period so a
 * transient native-host restart can rebind before a final save is queued.
 */
export const RECONNECT_DELAYS_MS = Object.freeze([
  500,
  1000,
  2500,
  5000,
  15000,
  30000,
]);

/** How long a single native request may take before it is treated as failed. */
export const NATIVE_REQUEST_TIMEOUT_MS = 20000;

/** How long the bridge page waits for the backend to publish a session URL. */
export const BRIDGE_TIMEOUT_MS = 25000;

/** Poll interval while the bridge waits for a session to become ready. */
export const BRIDGE_POLL_INTERVAL_MS = 500;

/**
 * Requesting status shortly after a mutation lets the worker observe
 * asynchronous backend work (saves are queued rather than synchronous).
 */
export const POST_MUTATION_REFRESH_MS = 900;

export const SESSION_STATES = Object.freeze([
  "opening",
  "editing",
  "saving",
  "saved",
  "error",
  "conflict",
]);

export const TERMINAL_SESSION_STATES = Object.freeze(["saved"]);
