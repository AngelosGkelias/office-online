import {
  BRIDGE_POLL_INTERVAL_MS,
  BRIDGE_TIMEOUT_MS,
  MessageType,
  NATIVE_HOST,
  NATIVE_REQUEST_TIMEOUT_MS,
  POST_MUTATION_REFRESH_MS,
  TERMINAL_SESSION_STATES,
} from "./constants.js";
import {
  humanError,
  isMicrosoftHttpsUrl,
  isSessionBridgeable,
  findSession,
  normalizeSessions,
} from "./session.js";
import {
  NativeClient,
  NativeResponseError,
  isOfflineError,
} from "./native-client.js";

export class BridgeError extends Error {
  constructor(message) {
    super(message);
    this.name = "BridgeError";
  }
}

function defaultSleep(scheduler, ms) {
  return new Promise((resolve) => scheduler.setTimeout(resolve, ms));
}

function validSessionId(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 200;
}

function validTabId(value) {
  return Number.isInteger(value) && value >= 0;
}

/**
 * Background service-worker controller. All Chrome APIs are injected so the
 * tracking, reconnect, bridge, and popup behavior can be tested with mocks.
 */
export class BackgroundController {
  constructor({
    store,
    native,
    connectNative,
    scheduler = globalThis,
    tabs,
    windows = null,
    runtime = null,
    logger = console,
    now = () => new Date(),
    bridgeTimeoutMs = BRIDGE_TIMEOUT_MS,
    bridgePollIntervalMs = BRIDGE_POLL_INTERVAL_MS,
    requestTimeoutMs = NATIVE_REQUEST_TIMEOUT_MS,
    postMutationRefreshMs = POST_MUTATION_REFRESH_MS,
    makeRequestId,
  }) {
    if (!store) throw new TypeError("BackgroundController requires store");
    this.store = store;
    this.scheduler = scheduler;
    this.tabs = tabs;
    this.windows = windows;
    this.runtime = runtime;
    this.logger = logger;
    this.now = now;
    this.bridgeTimeoutMs = bridgeTimeoutMs;
    this.bridgePollIntervalMs = bridgePollIntervalMs;
    this.requestTimeoutMs = requestTimeoutMs;
    this.postMutationRefreshMs = postMutationRefreshMs;

    this.native =
      native ||
      new NativeClient({
        connectNative,
        scheduler,
        requestTimeoutMs,
        logger,
        makeRequestId,
        onConnected: () => {
          this.handleNativeConnected().catch((error) =>
            this._log("native connect bootstrap failed", error),
          );
        },
        onDisconnected: (error) => this.handleNativeDisconnected(error),
        onStateChange: () => {
          this.broadcastState().catch((error) =>
            this._log("state broadcast failed", error),
          );
        },
      });

    this.lastError = null;
    this._lastSessions = null;
    this._lastSessionsAt = null;
    this._statusPromise = null;
    this._bootstrapPromise = null;
    this._flushPromise = null;
    this._refreshTimer = null;
    this._broadcastPromise = Promise.resolve();
    this._revision = 0;
    this._sessionLocks = new Map();
    this._connectedOnce = false;
    this._connectionEpoch = 0;
    this._initialized = false;
    this._stopped = false;
  }

  get sessions() {
    return this._lastSessions || [];
  }

  async init() {
    if (this._stopped) return null;
    this._initialized = true;
    return this.ensureReady();
  }

  async ensureReady() {
    if (this._stopped) return null;
    try {
      this.native.ensureConnected();
    } catch (error) {
      this.lastError = humanError(error);
      await this.broadcastState();
      return null;
    }
    if (!this.native.connected) {
      await this.broadcastState();
      return null;
    }
    if (!this._connectedOnce) {
      this._connectedOnce = true;
      return this.handleNativeConnected();
    }
    // The popup calls this on open and while visible. Coalesced status
    // requests keep the cached state fresh without rerunning the full
    // reconnect bootstrap.
    this.refreshStatus().catch((error) =>
      this._log("opportunistic status refresh failed", error),
    );
    return null;
  }

  /**
   * On every new native connection: fetch status, discard mappings whose tabs
   * no longer exist, deliver persisted offline tab closures, then refresh.
   */
  async handleNativeConnected() {
    if (this._stopped) return null;
    this._connectedOnce = true;
    if (this._bootstrapPromise) return this._bootstrapPromise;
    this._connectionEpoch += 1;
    const isReconnect = this._connectionEpoch > 1;
    this._bootstrapPromise = (async () => {
      this.lastError = null;
      await this.refreshStatus();
      await this.reconcileMappings();
      if (isReconnect) {
        // The daemon has a short disconnect grace period. Rebinding live
        // mappings to the new native connection cancels its EOF final-save
        // timer without treating a transient port drop as a tab close.
        await this.rebindMappedSessions();
      }
      await this.flushPendingClosures();
      await this.refreshStatus();
      await this.broadcastState();
    })()
      .catch((error) => {
        this.lastError = humanError(error);
        this._log("native bootstrap failed", error);
      })
      .finally(() => {
        this._bootstrapPromise = null;
      });
    return this._bootstrapPromise;
  }

  handleNativeDisconnected(error) {
    this._connectedOnce = false;
    this.lastError = humanError(error, "Office Online native host disconnected.");
    this.broadcastState().catch((broadcastError) =>
      this._log("state broadcast failed", broadcastError),
    );
  }

  /**
   * Chrome tab IDs are not stable across browser restarts. The backend queues
   * final saves on native socket EOF, so old mappings are simply discarded.
   * Offline tab closures remain durable and are delivered after reconnect.
   */
  async handleBrowserStartup() {
    this._connectedOnce = false;
    this._connectionEpoch = 0;
    this._lastSessions = null;
    this._lastSessionsAt = null;
    await this.store.clearMappings();
    await this.broadcastState();
    return this.ensureReady();
  }

  async tick() {
    if (this._stopped) return null;
    if (!this.native.connected) return this.ensureReady();
    await this.refreshStatus();
    await this.flushPendingClosures();
    return null;
  }

  stop() {
    this._stopped = true;
    if (this._refreshTimer !== null) {
      this.scheduler.clearTimeout(this._refreshTimer);
      this._refreshTimer = null;
    }
    if (typeof this.native.stop === "function") this.native.stop();
  }

  async refreshStatus() {
    if (this._stopped) return null;
    if (this._statusPromise) return this._statusPromise;
    const promise = (async () => {
      if (!this.native.connected) {
        await this.broadcastState();
        return null;
      }
      try {
        const response = await this.native.request(
          "status",
          {},
          { timeoutMs: this.requestTimeoutMs },
        );
        this.lastError = null;
        return await this.acceptSessions(response.sessions);
      } catch (error) {
        this.lastError = humanError(error);
        await this.broadcastState();
        return null;
      }
    })().finally(() => {
      this._statusPromise = null;
    });
    this._statusPromise = promise;
    return promise;
  }

  async acceptSessions(rawSessions) {
    if (!Array.isArray(rawSessions)) return this._lastSessions || [];
    const sessions = normalizeSessions(rawSessions);
    this.lastError = null;
    const savedAt = this.now().toISOString();
    this._lastSessions = sessions;
    this._lastSessionsAt = savedAt;
    await this.store.setSessions(sessions, savedAt);
    await this._discardObsoleteMappings(sessions);
    await this.broadcastState();
    return sessions;
  }

  scheduleRefresh(delay = this.postMutationRefreshMs) {
    if (this._stopped || this._refreshTimer !== null) return;
    this._refreshTimer = this.scheduler.setTimeout(() => {
      this._refreshTimer = null;
      this.refreshStatus().catch((error) =>
        this._log("scheduled status refresh failed", error),
      );
    }, delay);
  }

  /**
   * Durable mappings are also checked against live Chrome tabs. A stale entry
   * is removed silently; it is not treated as a tab close because the backend
   * socket EOF may already have queued the final save (e.g. browser restart).
   */
  async reconcileMappings() {
    let mappings;
    try {
      mappings = await this.store.getMappings();
    } catch (error) {
      this._log("cannot read mappings", error);
      return [];
    }
    const removed = [];
    for (const [sessionId, mapping] of Object.entries(mappings)) {
      if (!mapping || !validTabId(mapping.tab_id)) {
        await this.store.deleteMapping(sessionId);
        removed.push(sessionId);
        continue;
      }
      const tab = await this._getTab(mapping.tab_id);
      if (!tab) {
        await this.store.deleteMappingIf(
          sessionId,
          (current) => current && current.tab_id === mapping.tab_id,
        );
        removed.push(sessionId);
        continue;
      }
      const known = this._lastSessions
        ? findSession(this._lastSessions, sessionId)
        : null;
      if (
        this._lastSessions &&
        (!known || TERMINAL_SESSION_STATES.includes(known.state))
      ) {
        await this.store.deleteMappingIf(
          sessionId,
          (current) => current && current.tab_id === mapping.tab_id,
        );
        removed.push(sessionId);
      }
    }
    return removed;
  }

  /**
   * Re-associate existing mapped tabs with a newly reconnected native socket.
   * `bind` cancels the daemon's disconnect-finalize timer, so a brief port drop
   * does not become a final save while the tab is still being edited.
   */
  async rebindMappedSessions() {
    const mappings = await this.store.getMappings();
    for (const [sessionId, mapping] of Object.entries(mappings)) {
      if (!mapping || !validTabId(mapping.tab_id)) continue;
      const tab = await this._getTab(mapping.tab_id);
      if (!tab) continue;
      const known = findSession(this._lastSessions || [], sessionId);
      // A daemon restart marks previously editing sessions as error/recovery.
      // Rebinding those would clear the recovery state, so only reattach
      // sessions that the current status still considers live editing work.
      if (
        !known ||
        !["editing", "saving", "conflict"].includes(known.state)
      ) {
        continue;
      }
      try {
        const response = await this.native.request(
          "bind",
          { session_id: sessionId, tab_id: mapping.tab_id },
          { timeoutMs: this.requestTimeoutMs },
        );
        if (Array.isArray(response.sessions)) {
          await this.acceptSessions(response.sessions);
        }
      } catch (error) {
        if (isOfflineError(error)) break;
        if (error instanceof NativeResponseError) {
          // The session finished or disappeared while the socket was down.
          await this.store.deleteMappingIf(
            sessionId,
            (current) => current && current.tab_id === mapping.tab_id,
          );
        }
      }
    }
  }

  async _discardObsoleteMappings(sessions) {
    const mappings = await this.store.getMappings();
    for (const sessionId of Object.keys(mappings)) {
      const session = findSession(sessions, sessionId);
      if (!session || TERMINAL_SESSION_STATES.includes(session.state)) {
        await this.store.deleteMapping(sessionId);
      }
    }
  }

  /**
   * Atomically choose between focusing an existing mapped tab and binding this
   * bridge tab. The mapping is written before the caller is told to redirect.
   */
  async claimBridge(sessionId, tabId) {
    if (!validSessionId(sessionId)) {
      throw new BridgeError("This opening link has an invalid session identifier.");
    }
    if (!validTabId(tabId)) {
      throw new BridgeError("The browser tab could not be identified.");
    }

    return this._withSessionLock(sessionId, async () => {
      // Duplicate launches should focus the existing tab even if the native
      // status request is slow or temporarily offline. No bind is performed.
      const earlyMapping = await this._usableMapping(sessionId, tabId);
      if (earlyMapping && earlyMapping !== "current") {
        if (await this._focusExistingTab(earlyMapping.tab_id)) {
          return {
            action: "focus",
            existing_tab_id: earlyMapping.tab_id,
            session: findSession(this._lastSessions || [], sessionId),
          };
        }
        await this.store.deleteMappingIf(
          sessionId,
          (mapping) => mapping && mapping.tab_id === earlyMapping.tab_id,
        );
      }

      const deadline = this.now().getTime() + this.bridgeTimeoutMs;
      let session = null;
      let lastStatusError = null;

      while (true) {
        try {
          const remainingForRequest = Math.max(
            1000,
            deadline - this.now().getTime(),
          );
          const response = await this.native.request(
            "status",
            {},
            {
              timeoutMs: Math.min(this.requestTimeoutMs, remainingForRequest),
            },
          );
          this.lastError = null;
          session = findSession(
            await this.acceptSessions(response.sessions),
            sessionId,
          );
        } catch (error) {
          lastStatusError = humanError(error);
          this.lastError = lastStatusError;
          await this.broadcastState();
        }

        if (session) {
          const existing = await this._usableMapping(sessionId, tabId);
          if (existing === "current") {
            if (isSessionBridgeable(session)) {
              return { action: "redirect", url: session.url, session };
            }
          } else if (existing) {
            const focused = await this._focusExistingTab(existing.tab_id);
            if (focused) {
              return {
                action: "focus",
                existing_tab_id: existing.tab_id,
                session,
              };
            }
            // The tab vanished or cannot be focused; treat the mapping as stale.
            await this.store.deleteMappingIf(
              sessionId,
              (mapping) => mapping && mapping.tab_id === existing.tab_id,
            );
          }

          if (session.state === "saved") {
            throw new BridgeError(
              "This document session has already finished.",
            );
          }
          if (isSessionBridgeable(session)) break;
          if (
            (session.state === "error" || session.state === "conflict") &&
            !isMicrosoftHttpsUrl(session.url)
          ) {
            throw new BridgeError(
              session.error ||
                `This document session is in the "${session.state}" state without an editable URL. Use the extension popup to retry.`,
            );
          }
        }

        const nowMs = this.now().getTime();
        if (nowMs >= deadline) {
          if (session) {
            throw new BridgeError(
              "The document is still being prepared by Office Online. Use Retry in a moment.",
            );
          }
          throw new BridgeError(
            lastStatusError
              ? `Cannot reach the Office Online service: ${lastStatusError}`
              : "The Office Online session was not found. It may have expired.",
          );
        }
        const remaining = deadline - nowMs;
        const waitMs = Math.max(0, Math.min(this.bridgePollIntervalMs, remaining));
        if (lastStatusError && !this.native.connected) {
          this.native.ensureConnected();
        }
        await defaultSleep(this.scheduler, waitMs);
      }

      let bindResponse;
      try {
        bindResponse = await this.native.request(
          "bind",
          { session_id: sessionId, tab_id: tabId },
          { timeoutMs: this.requestTimeoutMs },
        );
      } catch (error) {
        throw new BridgeError(
          `Could not bind this tab to the document session: ${humanError(error)}`,
        );
      }

      let boundSession = session;
      if (Array.isArray(bindResponse.sessions)) {
        const sessions = await this.acceptSessions(bindResponse.sessions);
        boundSession = findSession(sessions, sessionId) || session;
      }
      const url =
        boundSession && isMicrosoftHttpsUrl(boundSession.url)
          ? boundSession.url
          : session.url;
      if (!isMicrosoftHttpsUrl(url)) {
        throw new BridgeError(
          "The document URL from the Office Online service is not a safe Microsoft HTTPS URL.",
        );
      }

      // Must be durable before the bridge redirects to Microsoft.
      await this.store.setMapping(sessionId, {
        tab_id: tabId,
        session_id: sessionId,
        name: boundSession.name,
        original_path: boundSession.original_path,
        bound_at: this.now().toISOString(),
      });
      await this.broadcastState();
      return { action: "redirect", url, session: boundSession || session };
    });
  }

  /**
   * Returns "current" if this bridge tab is already mapped, a mapping object if
   * another live tab owns the session, or null when no usable mapping exists.
   */
  async _usableMapping(sessionId, currentTabId) {
    const mapping = await this.store.getMapping(sessionId);
    if (!mapping || !validTabId(mapping.tab_id)) return null;
    if (mapping.tab_id === currentTabId) return "current";
    const tab = await this._getTab(mapping.tab_id);
    if (!tab) {
      await this.store.deleteMappingIf(
        sessionId,
        (current) => current && current.tab_id === mapping.tab_id,
      );
      return null;
    }
    return mapping;
  }

  async _focusExistingTab(tabId) {
    try {
      const tab = await this.tabs.get(tabId);
      if (!tab) return false;
      await this.tabs.update(tabId, { active: true });
      if (
        this.windows &&
        typeof this.windows.update === "function" &&
        Number.isInteger(tab.windowId)
      ) {
        try {
          await this.windows.update(tab.windowId, { focused: true });
        } catch {
          // The tab is already active; a missing window is not fatal.
        }
      }
      return true;
    } catch {
      return false;
    }
  }

  async _getTab(tabId) {
    if (!this.tabs || typeof this.tabs.get !== "function") return null;
    try {
      return await this.tabs.get(tabId);
    } catch {
      return null;
    }
  }

  /**
   * A tab is the single tracked surface for its session. Persist the closure
   * before delivery so an offline/browser-suspend gap cannot lose the final save.
   */
  async handleTabRemoved(tabId) {
    if (!validTabId(tabId)) return [];
    let mappings;
    try {
      mappings = await this.store.getMappings();
    } catch (error) {
      this._log("cannot read mappings for tab close", error);
      return [];
    }
    const affected = Object.entries(mappings)
      .filter(([, mapping]) => mapping && mapping.tab_id === tabId)
      .map(([sessionId]) => sessionId);

    if (affected.length === 0) return [];

    let sessions = this._lastSessions;
    if (!sessions) {
      const cache = await this.store.getSessionsCache();
      sessions = cache ? cache.sessions : [];
    }
    const queued = [];
    for (const sessionId of affected) {
      const record = await this._withSessionLock(sessionId, async () => {
        const mapping = await this.store.getMapping(sessionId);
        if (!mapping || mapping.tab_id !== tabId) return null;
        const removed = await this.store.deleteMappingIf(
          sessionId,
          (current) => current && current.tab_id === tabId,
        );
        if (!removed) return null;
        const session = findSession(sessions, sessionId);
        return this.store.enqueueTabClosure({
          session_id: sessionId,
          tab_id: tabId,
          name: session ? session.name : mapping.name || "",
          original_path: session ? session.original_path : mapping.original_path || "",
          queued_at: this.now().toISOString(),
        });
      });
      if (record) queued.push(record);
    }

    await this.broadcastState();
    if (queued.length > 0) {
      await this.flushPendingClosures();
      // A flush that was already running may have snapshotted the queue before
      // this close was persisted. Give those records one more pass if needed.
      const remaining = await this.store.getPendingClosures();
      const queuedIds = new Set(queued.map((record) => record.id));
      if (
        this.native.connected &&
        remaining.some(
          (record) =>
            queuedIds.has(record.id) && (!record.attempts || record.attempts === 0),
        )
      ) {
        await this.flushPendingClosures();
      }
      // The final save is asynchronous in the daemon; ask for status again so
      // the popup can show the eventual saved/error result.
      this.scheduleRefresh(500);
    }
    return queued;
  }

  async flushPendingClosures() {
    if (this._stopped) return { sent: 0, failed: 0, remaining: 0 };
    if (this._flushPromise) return this._flushPromise;
    this._flushPromise = (async () => {
      let pending;
      try {
        pending = await this.store.getPendingClosures();
      } catch (error) {
        this._log("cannot read pending tab closures", error);
        return { sent: 0, failed: 0, remaining: 0 };
      }
      let sent = 0;
      let failed = 0;
      for (const record of pending) {
        if (!this.native.connected) break;
        try {
          const response = await this.native.request(
            "tab_closed",
            { session_id: record.session_id },
            { timeoutMs: this.requestTimeoutMs },
          );
          await this.store.removeTabClosure(record.id);
          if (Array.isArray(response.sessions)) {
            await this.acceptSessions(response.sessions);
          }
          sent += 1;
        } catch (error) {
          failed += 1;
          await this.store.markTabClosureAttempt(record.id, humanError(error));
          if (isOfflineError(error)) break;
        }
      }
      if (failed > 0) await this.broadcastState();
      const remaining = (await this.store.getPendingClosures()).length;
      return { sent, failed, remaining };
    })().finally(() => {
      this._flushPromise = null;
    });
    return this._flushPromise;
  }

  async handleMessage(message) {
    if (!message || typeof message !== "object") {
      return { ok: false, error: "Invalid extension message." };
    }
    try {
      switch (message.type) {
        case MessageType.GET_STATE: {
          // Return the cached state immediately, but trigger a connection and
          // status bootstrap so the popup updates via broadcast.
          this.ensureReady().catch((error) =>
            this._log("ensureReady from popup failed", error),
          );
          return { ok: true, state: await this.buildState() };
        }
        case MessageType.REFRESH: {
          await this.ensureReady();
          await this.refreshStatus();
          return {
            ok: true,
            state: await this.buildState(),
            sessions: this.sessions,
          };
        }
        case MessageType.SAVE: {
          return await this._mutateSession("save", message.session_id);
        }
        case MessageType.RETRY: {
          return await this._mutateSession("retry", message.session_id);
        }
        case MessageType.CLAIM_BRIDGE: {
          const result = await this.claimBridge(
            message.session_id,
            message.tab_id,
          );
          return {
            ok: true,
            result,
            state: await this.buildState(),
          };
        }
        default:
          return { ok: false, error: "Unknown extension message." };
      }
    } catch (error) {
      return { ok: false, error: humanError(error) };
    }
  }

  async _mutateSession(action, sessionId) {
    if (!validSessionId(sessionId)) {
      return { ok: false, error: "Invalid session identifier." };
    }
    const params = { session_id: sessionId };
    // Manual Save never ends tracking; finalization happens on tab close.
    if (action === "save") params.finish = false;
    let response;
    try {
      response = await this.native.request(action, params, {
        timeoutMs: this.requestTimeoutMs,
      });
    } catch (error) {
      const message = humanError(error);
      if (isOfflineError(error)) this.lastError = message;
      await this.broadcastState();
      return { ok: false, error: message, state: await this.buildState() };
    }

    if (Array.isArray(response.sessions)) {
      await this.acceptSessions(response.sessions);
    } else {
      await this.refreshStatus();
    }
    this.scheduleRefresh();
    return {
      ok: true,
      state: await this.buildState(),
      sessions: this.sessions,
    };
  }

  async buildState() {
    const [mappings, pending, cache] = await Promise.all([
      this.store.getMappings().catch(() => ({})),
      this.store.getPendingClosures().catch(() => []),
      this.store.getSessionsCache().catch(() => null),
    ]);
    const sessions =
      this._lastSessions || (cache && Array.isArray(cache.sessions) ? cache.sessions : []);
    return {
      revision: ++this._revision,
      connected: Boolean(this.native.connected),
      lastError: this.lastError,
      sessions,
      sessionsSavedAt: this._lastSessionsAt || (cache ? cache.saved_at : null),
      mappings,
      pendingClosures: pending,
      nativeHost: NATIVE_HOST,
      updatedAt: this.now().toISOString(),
    };
  }

  broadcastState() {
    if (!this.runtime || typeof this.runtime.sendMessage !== "function") {
      return Promise.resolve();
    }
    this._broadcastPromise = this._broadcastPromise.then(async () => {
      const state = await this.buildState();
      try {
        await this.runtime.sendMessage({
          type: MessageType.STATE_CHANGED,
          state,
        });
      } catch {
        // No popup or bridge page is listening; this is normal.
      }
    });
    return this._broadcastPromise;
  }

  _withSessionLock(sessionId, operation) {
    const prior = this._sessionLocks.get(sessionId) || Promise.resolve();
    const run = prior.catch(() => {}).then(operation);
    const tail = run.catch(() => {});
    this._sessionLocks.set(sessionId, tail);
    tail.finally(() => {
      if (this._sessionLocks.get(sessionId) === tail) {
        this._sessionLocks.delete(sessionId);
      }
    });
    return run;
  }

  _log(...args) {
    try {
      if (this.logger && typeof this.logger.error === "function") {
        this.logger.error("[office-online]", ...args);
      }
    } catch {
      // Never let logging break message handling.
    }
  }
}
