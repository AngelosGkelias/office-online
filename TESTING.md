# Verification

Validation target: Fedora Workstation, GNOME Shell, and Nautilus introspection API 4.1.

- Python: full suite passes (`python3 -m unittest discover -s tests`; 129 tests at last run).
- Chrome extension: 48 tests pass (`node --test tests/test_extension*.js`).
- Installed Nautilus provider imports successfully against the real Nautilus 4.1 API.
- Installed desktop entries pass `desktop-file-validate`.
- Installed native messaging wrapper exchanges a framed status request with the real user-service daemon.
- User systemd service enabled and running; CLI status responds.
- GTK4 uploader protocol, error reporting and dismissed-window behavior covered by 24 mocked tests.
- Bounded Wayland smoke test confirmed the uploader app ID; its window closes on completion and normal stdin EOF.

Tests cover session copies, manual/repeated/final saves, connection-specific browser disconnect saves, reconnect grace, local-edit conflicts, backup creation, network retries, restart recovery, native frame IO, installer isolation, extension tab tracking, seven-day completed-session cleanup selection/dry-run/retry/idempotency, local working/backup/history removal with sanitized `sessions.json.bak`, legacy `cloud_deleted_at` local-only cleanup, cloud-failure preservation, local partial-failure retry without a second cloud deletion, symlink/path-escape/protected-original refusals, the locked `SessionStore.prune` durability rules, the exact acknowledgement-checked OneDrive `--remove-directory` command, and the dedicated OneDrive quiet profile. Save-back is download-only, anonymous sharing links are never created; cleanup removes completed app cloud folders plus app-owned working/backup directories and history records, while user originals, results, and conflict siblings are retained. Cleanup tests use temporary directories and mocked runners, so they authenticate and delete nothing real.

Local smoke test (2026-11-12): reproduced an installed-profile upload failure
with OneDrive v2.5.11, which rejected the CLI sync root because the profile did
not set a matching `sync_dir`. After persisting the app's working folder in the
profile, a real document upload succeeded, an authenticated Microsoft URL was
returned, and the installed Chrome extension bound the editing tab. No resync
was performed. Regression checks cover profile idempotence, installer paths
(including XDG overrides), and authentication setup writing the matching root.

Not live-verified: actual Office editor interaction or real download/save-back.
Automated checks do not authenticate or edit user documents. Chrome must load
the unpacked extension manually; Chrome headless rejected the load-extension
flag during earlier agent validation. See README.md quick start for setup and
a real disposable-file edit/save trial before relying on it for important documents.
