# Office Online Local extension

Manifest V3 companion for the local Office Online daemon. It tracks documents opened through `office_online.py open`, keeps one durable tab mapping per session, and forwards manual save / retry / tab-close events over a persistent native port to `org.local.office_online`.

## Install

1. Open `chrome://extensions`, enable **Developer mode**, choose **Load unpacked**, and select this `extension/` directory.
2. Use the stable ID in [`id.txt`](id.txt): `ipbmejlknifhiljakaifiochakimakko`.
3. Set the backend's `extension_id` to that value and install the native messaging host manifest with `allowed_origins` containing `chrome-extension://ipbmejlknifhiljakaifiochakimakko/`. The host must run the project's `office_online.py native-host`.
4. The extension never launches Chrome or signs in to Microsoft for you.

The `key` in `manifest.json` is a public RSA key. It fixes the unpacked extension ID; the corresponding private key is deliberately not stored in this repository. The ID is the first 16 bytes of SHA-256 over the DER-encoded public key, with each hex nibble mapped from `0-f` to `a-p`; `id.txt` contains the result.

## What it does

- Opens `open.html?session=<uuid>` as a bridge, obtains the session from the daemon, binds the current tab, persists the tab mapping in `chrome.storage.local` **before** redirecting to the Microsoft editor URL.
- If another live tab is already mapped to the same session, focuses that tab and closes the duplicate bridge without binding or sending `tab_closed`.
- Shows tracked sessions in a compact popup with original name/path, status/error, **Save to computer** (manual save, `finish:false`), **Retry**, and queued-final-save state.
- Sends `tab_closed` when a mapped tab is removed and persists the request locally first if the native host is offline. Queued closures flush on reconnect.
- Reconnects the persistent native port with backoff, multiplexes request IDs, and refreshes status on connect, popup open, tab close, and a one-minute alarm. A quick reconnect rebinds still-live editing tabs to cancel the daemon's short socket-EOF grace timer; recovery/error sessions are not rebound.
- Uses no remote code, no content scripts, no `web_accessible_resources`, and no host permissions.

## Browser and service limitations

- Chrome may keep an extension's native connection alive after all normal windows are closed when background mode is enabled. Closing a tracked **tab** still emits `onRemoved`, which targets only that tab's session; do not assume window closure ends tracking.
- A browser crash, OS shutdown, extension reload, or forced service-worker termination can prevent JavaScript from running. The daemon's native-socket EOF must queue the final save for the sessions bound to that connection. The extension cannot send `tab_closed` in that case.
- After a daemon restart, interrupted editing and saving sessions are queued for recovery. The extension deliberately does not auto-rebind recovery/error sessions, because `bind` would clear their recovery state; check the result and use Retry if recovery fails.
- Tab IDs are not stable across browser restarts. On `runtime.onStartup` the extension clears old tab mappings (pending offline closures are kept) because the daemon has already seen socket EOF. Do not treat this cleanup as a fresh tab close.
- If the native host is missing, the popup shows "Service offline"; reconnect attempts continue with capped backoff. Queued offline closures survive browser restarts in `chrome.storage.local` until they are acknowledged.
- A bridge tab is closed by Chrome after a duplicate is detected. This must not finalize the session, so mapping always points at the surviving tab and the duplicate tab is never bound.
- Desktop notifications for final saves, conflicts, and save errors are owned by the daemon (`notify-send`). The extension intentionally does not create Chrome notifications too, to avoid duplicate alerts; the popup shows the same states from `status`.
- Microsoft editor pages are ordinary HTTPS pages. The extension does not inspect them and cannot recover if the user navigates that tab away; stale mappings are only focused when the tab still exists.

## Tests

From the project root:

```sh
node --test tests/test_extension*.js
```

Tests use Node's built-in runner with mock Chrome storage, tabs, runtime, timers, and a fake native port. They cover ID/key derivation, request/response multiplexing, timeout/disconnect/reconnect, durable mappings, offline tab closures, duplicate bridge focusing, stale mappings, popup state, and bridge URL safety.
