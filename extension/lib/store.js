import { STORAGE_KEYS } from "./constants.js";

function clone(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function defaultClosureId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `closure-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Durable extension state in chrome.storage.local.
 *
 * Every read-modify-write is serialized through one promise chain so concurrent
 * tab events, bridge claims, and popup requests cannot lose updates. Values are
 * cloned because chrome.storage callbacks and tests may retain references.
 */
export class ExtensionStore {
  constructor({ storageArea, now = () => new Date(), makeClosureId = defaultClosureId }) {
    if (!storageArea) throw new TypeError("ExtensionStore requires storageArea");
    this.storageArea = storageArea;
    this.now = now;
    this.makeClosureId = makeClosureId;
    this._writeChain = Promise.resolve();
  }

  _read(key, fallback) {
    return Promise.resolve(this.storageArea.get(key)).then((result) => {
      if (!result || result[key] === undefined) return clone(fallback);
      return clone(result[key]);
    });
  }

  _write(key, value) {
    return Promise.resolve(this.storageArea.set({ [key]: clone(value) }));
  }

  _serialize(operation) {
    const run = this._writeChain.then(operation, operation);
    this._writeChain = run.catch(() => {});
    return run;
  }

  async getMappings() {
    const mappings = await this._read(STORAGE_KEYS.MAPPINGS, {});
    if (!mappings || typeof mappings !== "object" || Array.isArray(mappings)) return {};
    return mappings;
  }

  async getMapping(sessionId) {
    const mappings = await this.getMappings();
    return mappings[sessionId] || null;
  }

  setMapping(sessionId, mapping) {
    return this._serialize(async () => {
      const mappings = await this._read(STORAGE_KEYS.MAPPINGS, {});
      mappings[sessionId] = clone(mapping);
      await this._write(STORAGE_KEYS.MAPPINGS, mappings);
      return clone(mapping);
    });
  }

  deleteMapping(sessionId) {
    return this._serialize(async () => {
      const mappings = await this._read(STORAGE_KEYS.MAPPINGS, {});
      if (!(sessionId in mappings)) return false;
      delete mappings[sessionId];
      await this._write(STORAGE_KEYS.MAPPINGS, mappings);
      return true;
    });
  }

  /** Atomically delete a mapping only when it still matches the predicate. */
  deleteMappingIf(sessionId, predicate) {
    return this._serialize(async () => {
      const mappings = await this._read(STORAGE_KEYS.MAPPINGS, {});
      const mapping = mappings[sessionId];
      if (!mapping || !predicate(mapping)) return false;
      delete mappings[sessionId];
      await this._write(STORAGE_KEYS.MAPPINGS, mappings);
      return true;
    });
  }

  clearMappings() {
    return this._serialize(() => this._write(STORAGE_KEYS.MAPPINGS, {}));
  }

  async getPendingClosures() {
    const pending = await this._read(STORAGE_KEYS.PENDING_CLOSURES, []);
    if (!Array.isArray(pending)) return [];
    return pending.filter((record) => record && typeof record.session_id === "string");
  }

  /**
   * Persist a tab-close finalization request before attempting to deliver it.
   * A matching unresolved request (same session and tab) is reused so duplicate
   * close events cannot multiply backend work.
   */
  enqueueTabClosure(candidate) {
    return this._serialize(async () => {
      const pending = await this._read(STORAGE_KEYS.PENDING_CLOSURES, []);
      const list = Array.isArray(pending) ? pending : [];
      const existing = list.find(
        (record) =>
          record.session_id === candidate.session_id &&
          record.tab_id === candidate.tab_id,
      );
      if (existing) return clone(existing);

      const record = {
        id: candidate.id || this.makeClosureId(),
        session_id: candidate.session_id,
        tab_id: candidate.tab_id,
        name: candidate.name || "",
        original_path: candidate.original_path || "",
        queued_at: candidate.queued_at || this.now().toISOString(),
        attempts: 0,
        last_error: null,
        last_attempt_at: null,
      };
      list.push(record);
      await this._write(STORAGE_KEYS.PENDING_CLOSURES, list);
      return clone(record);
    });
  }

  removeTabClosure(closureId) {
    return this._serialize(async () => {
      const pending = await this._read(STORAGE_KEYS.PENDING_CLOSURES, []);
      const list = Array.isArray(pending) ? pending : [];
      const next = list.filter((record) => record.id !== closureId);
      if (next.length === list.length) return false;
      await this._write(STORAGE_KEYS.PENDING_CLOSURES, next);
      return true;
    });
  }

  markTabClosureAttempt(closureId, errorMessage) {
    return this._serialize(async () => {
      const pending = await this._read(STORAGE_KEYS.PENDING_CLOSURES, []);
      const list = Array.isArray(pending) ? pending : [];
      const record = list.find((item) => item.id === closureId);
      if (!record) return null;
      record.attempts = Number.isInteger(record.attempts) ? record.attempts + 1 : 1;
      record.last_error = errorMessage || "Unknown error";
      record.last_attempt_at = this.now().toISOString();
      await this._write(STORAGE_KEYS.PENDING_CLOSURES, list);
      return clone(record);
    });
  }

  async getSessionsCache() {
    const cache = await this._read(STORAGE_KEYS.SESSIONS, null);
    if (!cache || typeof cache !== "object" || !Array.isArray(cache.sessions)) return null;
    return cache;
  }

  setSessions(sessions, savedAt = this.now().toISOString()) {
    return this._serialize(() =>
      this._write(STORAGE_KEYS.SESSIONS, {
        saved_at: savedAt,
        sessions: clone(sessions),
      }),
    );
  }
}
