import {
  NATIVE_HOST,
  NATIVE_REQUEST_TIMEOUT_MS,
  RECONNECT_DELAYS_MS,
} from "./constants.js";

export class NativeError extends Error {
  constructor(message) {
    super(message);
    this.name = "NativeError";
  }
}

export class NativeOfflineError extends NativeError {
  constructor(message = "Office Online native host is not available.") {
    super(message);
    this.name = "NativeOfflineError";
  }
}

export class NativeTimeoutError extends NativeError {
  constructor(action, timeoutMs) {
    super(`Office Online service did not answer the "${action}" request within ${Math.round(timeoutMs / 1000)}s.`);
    this.name = "NativeTimeoutError";
    this.action = action;
  }
}

export class NativeResponseError extends NativeError {
  constructor(message, { action, response } = {}) {
    super(message || "Office Online service rejected the request.");
    this.name = "NativeResponseError";
    this.action = action;
    this.response = response;
  }
}

export function isOfflineError(error) {
  return error instanceof NativeOfflineError || error instanceof NativeTimeoutError;
}

function defaultMakeRequestId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return `req-${globalThis.crypto.randomUUID()}`;
  }
  return `req-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function defaultGetLastError() {
  try {
    return globalThis.chrome?.runtime?.lastError?.message || null;
  } catch {
    return null;
  }
}

function listen(event, listener) {
  if (event && typeof event.addListener === "function") event.addListener(listener);
}

function unlisten(event, listener) {
  if (event && typeof event.removeListener === "function") event.removeListener(listener);
}

/**
 * One persistent chrome.runtime.connectNative port with request/response
 * multiplexing, timeouts, durable reconnection, and no unsolicited messages.
 */
export class NativeClient {
  constructor({
    connectNative,
    scheduler = globalThis,
    requestTimeoutMs = NATIVE_REQUEST_TIMEOUT_MS,
    reconnectDelaysMs = RECONNECT_DELAYS_MS,
    onConnected = () => {},
    onDisconnected = () => {},
    onStateChange = () => {},
    getLastError = defaultGetLastError,
    logger = console,
    makeRequestId = defaultMakeRequestId,
  } = {}) {
    if (typeof connectNative !== "function") {
      throw new TypeError("NativeClient requires connectNative");
    }
    this.connectNative = connectNative;
    this.scheduler = scheduler;
    this.requestTimeoutMs = requestTimeoutMs;
    this.reconnectDelaysMs =
      Array.isArray(reconnectDelaysMs) && reconnectDelaysMs.length > 0
        ? reconnectDelaysMs
        : [...RECONNECT_DELAYS_MS];
    this.onConnected = onConnected;
    this.onDisconnected = onDisconnected;
    this.onStateChange = onStateChange;
    this.getLastError = getLastError;
    this.logger = logger;
    this.makeRequestId = makeRequestId;

    this.port = null;
    this.pending = new Map();
    this.reconnectTimer = null;
    this.reconnectAttempt = 0;
    this.lastError = null;
    this.stopped = false;
  }

  get connected() {
    return Boolean(this.port);
  }

  get pendingCount() {
    return this.pending.size;
  }

  /**
   * Open the persistent native port if it is not already open.
   * Returns true only when a new port was created.
   */
  ensureConnected() {
    if (this.stopped || this.port) return false;
    this._clearReconnectTimer();

    let port;
    try {
      port = this.connectNative(NATIVE_HOST);
    } catch (error) {
      this.lastError =
        error && typeof error.message === "string"
          ? `Cannot start the Office Online native host: ${error.message}`
          : "Cannot start the Office Online native host.";
      this._emitState();
      this._scheduleReconnect();
      return false;
    }

    if (!port || typeof port.postMessage !== "function") {
      this.lastError = "The Office Online native host returned an invalid connection.";
      this._emitState();
      this._scheduleReconnect();
      return false;
    }

    this.port = port;
    this.lastError = null;
    this._portMessageListener = (message) => this._handleMessage(message);
    this._portDisconnectListener = () => this._handlePortDisconnected(port);
    listen(port.onMessage, this._portMessageListener);
    listen(port.onDisconnect, this._portDisconnectListener);
    this._emitState();

    // Give the controller a chance to run its status/closure bootstrap. The
    // native host provides no explicit "connected" event, so failures arrive
    // through request rejection or onDisconnect shortly afterwards.
    Promise.resolve()
      .then(() => this.onConnected())
      .catch((error) => this._log("onConnected handler failed", error));

    return true;
  }

  /**
   * Send one request. The returned promise rejects for transport failures,
   * timeouts, and `{ok:false}` replies.
   */
  request(action, params = {}, { timeoutMs = this.requestTimeoutMs } = {}) {
    if (typeof action !== "string" || !action) {
      return Promise.reject(new NativeError("Invalid native action."));
    }
    if (this.stopped) {
      return Promise.reject(new NativeOfflineError("Office Online extension is shutting down."));
    }
    if (!this.port) {
      this.ensureConnected();
      if (!this.port) {
        return Promise.reject(
          new NativeOfflineError(this.lastError || "Office Online native host is not available."),
        );
      }
    }

    const port = this.port;
    const id = this.makeRequestId();
    return new Promise((resolve, reject) => {
      const pending = {
        id,
        action,
        resolve,
        reject,
        timer: null,
      };

      const effectiveTimeout = Number.isFinite(timeoutMs) && timeoutMs > 0
        ? timeoutMs
        : this.requestTimeoutMs;
      if (Number.isFinite(effectiveTimeout) && effectiveTimeout > 0) {
        pending.timer = this.scheduler.setTimeout(() => {
          if (!this.pending.has(id)) return;
          this.pending.delete(id);
          const error = new NativeTimeoutError(action, effectiveTimeout);
          this.lastError = error.message;
          this._emitState();
          reject(error);
        }, effectiveTimeout);
      }
      this.pending.set(id, pending);

      try {
        port.postMessage({ ...params, id, action });
      } catch (error) {
        // A disconnected port throws synchronously. Tear it down and reject
        // every in-flight request; onDisconnect may still fire afterwards.
        this._failPort(
          port,
          new NativeOfflineError(
            `Lost the Office Online native connection while sending "${action}".`,
          ),
        );
      }
    });
  }

  stop() {
    if (this.stopped) return;
    this.stopped = true;
    this._clearReconnectTimer();
    const port = this.port;
    this.port = null;
    if (port) {
      unlisten(port.onMessage, this._portMessageListener);
      unlisten(port.onDisconnect, this._portDisconnectListener);
      try {
        port.disconnect();
      } catch {
        // Already disconnected.
      }
    }
    this._rejectAllPending(
      new NativeOfflineError("Office Online native connection closed."),
    );
  }

  _handleMessage(message) {
    if (!message || typeof message !== "object") {
      this._log("Ignoring malformed native message");
      return;
    }
    const pending = this.pending.get(message.id);
    if (!pending) {
      // The contract says the host never sends unsolicited responses.
      this._log("Ignoring native response with unknown request id");
      return;
    }
    this.pending.delete(message.id);
    if (pending.timer !== null) this.scheduler.clearTimeout(pending.timer);
    this.reconnectAttempt = 0;
    this.lastError = null;

    if (message.ok === true) {
      pending.resolve(message);
      return;
    }
    const errorText =
      typeof message.error === "string" && message.error.trim()
        ? message.error.trim()
        : "Office Online service rejected the request.";
    pending.reject(
      new NativeResponseError(errorText, { action: pending.action, response: message }),
    );
  }

  _handlePortDisconnected(port) {
    if (this.port !== port) return;
    const lastError = this.getLastError();
    const message = lastError
      ? `Office Online native host disconnected: ${lastError}`
      : "Office Online native host disconnected.";
    this._failPort(port, new NativeOfflineError(message), { disconnectPort: false });
  }

  _failPort(port, error, { disconnectPort = true } = {}) {
    if (this.port !== port) return;
    this.port = null;
    unlisten(port.onMessage, this._portMessageListener);
    unlisten(port.onDisconnect, this._portDisconnectListener);
    if (disconnectPort) {
      try {
        port.disconnect();
      } catch {
        // Already disconnected.
      }
    }
    this.lastError = error.message;
    this._rejectAllPending(error);
    this._emitState();
    try {
      this.onDisconnected(error);
    } catch (callbackError) {
      this._log("onDisconnected handler failed", callbackError);
    }
    this._scheduleReconnect();
  }

  _rejectAllPending(error) {
    const pending = [...this.pending.values()];
    this.pending.clear();
    for (const item of pending) {
      if (item.timer !== null) this.scheduler.clearTimeout(item.timer);
      item.reject(error);
    }
  }

  _scheduleReconnect() {
    if (this.stopped || this.port || this.reconnectTimer !== null) return;
    const index = Math.min(
      this.reconnectAttempt,
      this.reconnectDelaysMs.length - 1,
    );
    const delay = this.reconnectDelaysMs[index];
    this.reconnectAttempt += 1;
    this.reconnectTimer = this.scheduler.setTimeout(() => {
      this.reconnectTimer = null;
      if (this.stopped || this.port) return;
      this.ensureConnected();
    }, delay);
  }

  _clearReconnectTimer() {
    if (this.reconnectTimer === null) return;
    this.scheduler.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  _emitState() {
    try {
      this.onStateChange({
        connected: this.connected,
        lastError: this.lastError,
      });
    } catch (error) {
      this._log("onStateChange handler failed", error);
    }
  }

  _log(...args) {
    try {
      if (this.logger && typeof this.logger.debug === "function") {
        this.logger.debug("[office-online]", ...args);
      }
    } catch {
      // Logging must never break the extension.
    }
  }
}
