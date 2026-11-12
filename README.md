# Office Online for GNOME

Open local Word, Excel, and PowerPoint documents in Microsoft Office Online from
GNOME Files, then save your browser edits back to the original file.

**This is a vibe-coded project:** it was developed with AI coding agents and
human direction. It is experimental software, independently developed and not
affiliated with Microsoft. Automated tests cover local behavior, but they do
not establish production reliability or verify Microsoft's live web editor.
Try disposable copies before using important documents.

## Features

- **Open in Office Online** in the GNOME Files right-click menu for `.docx`,
  `.xlsx`, and `.pptx` files, plus a standard **Open With** entry.
- A GTK4 upload window with its own application icon.
- A Chrome/Chromium extension with **Save to computer**, status, and retry controls.
- Automatic final save when a tracked tab closes, with a background helper that
  can also save after the browser exits.
- Backups before replacement and separate conflict copies when a local file changed.
- Persistent session recovery and retry after connection failures.
- Seven-day retention for successfully completed editing sessions, covering
  their OneDrive working folder, local working copies, backups, and session history.
- A dedicated OneDrive profile, without using an existing personal sync profile.

## How it works

1. Select a supported local file and choose **Open in Office Online**.
2. The helper copies it into a unique `OfficeOnline/<session UUID>/` working
   folder and uploads that folder to your OneDrive.
3. Chrome opens an extension bridge page, which associates the editing tab with
   the local file and then navigates to Microsoft's authenticated document URL.
4. Edit in Word, Excel, or PowerPoint for the web. The browser edits the cloud copy.
5. Click **Save to computer**, or close the tracked tab. The helper downloads
   the cloud copy and saves it locally. Saving only downloads; it does not
   upload an older snapshot over browser edits.

**Cloud-save timing matters.** The helper assumes Office has finished saving
when you request a download or close the tab. It cannot guarantee that the last
keystrokes reached Microsoft. Wait for Office's saved indicator before closing,
and check the app's save result afterward.

## Requirements

The development target is **Fedora Workstation with GNOME Files/Nautilus 4**.
Other distributions have not been validated.

- Python 3.9 or newer for the backend; GTK4 and PyGObject for the uploader.
- Nautilus Python extension support, using API 4.1 or 4.0.
- Distro-installed Google Chrome or Chromium 116 or newer.
- The [abraunegg OneDrive Client for Linux](https://github.com/abraunegg/onedrive).
- A Microsoft account with usable OneDrive storage and Office web access.
- A working user systemd session; `notify-send` for save/error notifications.

Flatpak/Snap browser native messaging and enterprise-managed extension policies
may need additional configuration and are not covered by this installer.
Work/school account policies may restrict OneDrive client authorization.

## Install

### 1. Install prerequisites

On Fedora, install the dependencies explicitly:

```sh
sudo dnf install onedrive nautilus-python python3-gobject gtk4 libnotify
```

This is a package-manager step you run yourself. The project installer never
runs `sudo`, installs packages, upgrades OneDrive, or changes global settings.

### 2. Clone and install the per-user integration

```sh
git clone https://github.com/AngelosGkelias/office-online.git
cd office-online

./install.sh --dry-run
./install.sh
```

`install.sh` is the shell entry point to `python3 install.py`; both accept the
same options. The installer creates the user launchers, Nautilus provider,
desktop entries, native-messaging manifests, uploader icon, and user service.
It enables and starts **only** `office-online.service`.

Keep the checkout at this location: installed launchers reference its absolute
path. If you move it, rerun the installer from the new location.

### 3. Load the browser extension

1. Open `chrome://extensions`.
2. Enable **Developer mode** and select **Load unpacked**.
3. Select this checkout's `extension/` directory.
4. Optionally pin **Office Online Local** to the toolbar.

The fixed extension ID is `ipbmejlknifhiljakaifiochakimakko`. The manifest contains
an intentional **public** key to keep that ID stable; no private key is included.
The installer registers a native host that permits only this extension origin.

### 4. Sign into the dedicated OneDrive profile

```sh
~/.local/bin/office-online setup
```

Follow the OneDrive client's authorization instructions. Setup authenticates
only the app's profile and does not synchronize your entire drive. Sign into
the same Microsoft account in Chrome when opening documents.

The app's profile disables the OneDrive client's desktop notifications and
version-check reminders. Office Online's own save/error notifications remain on.
This does not alter another OneDrive profile or GNOME notification settings.

### 5. Reload Files and check the setup

Close and reopen Files. If the new menu does not appear, log out and back in,
or run `nautilus -q` when you are ready to close your existing Files windows.
The installer does not restart Files, Chrome, or GNOME Shell.

```sh
~/.local/bin/office-online doctor
```

Start with a disposable document and verify both manual save and tab-close save.

## Use and saving behavior

Right-click one or more local `.docx`, `.pptx`, or `.xlsx` files and select
**Open in Office Online**. Unsupported or remote/GVFS selections are excluded.
Reopening a tracked file focuses its existing editing tab where possible.

- **Save to computer** downloads the latest cloud version and keeps the session open.
- **Closing a tracked tab** requests a final save.
- **Fully exiting Chrome** closes its native connection. The independent helper
  treats that disconnect as a final-save trigger for its bound sessions.
- Chrome background mode can keep the process alive after windows close;
  tracked tab closure is still handled separately.
- Browser crashes, logout, and power loss can interrupt saving. Persistent
  session records allow recovery when the helper runs again; no saving can run
  while the computer is off.

Before replacing a local original, the app creates a backup. If the original
changed externally, the app preserves it and writes the downloaded document
beside it as a uniquely named conflict copy. Review that copy before choosing
which version to keep. Failed downloads do not replace the original.

## Retention and cleanup

Cleanup checks about 30 seconds after helper startup and every six hours afterward.
A session qualifies only after **seven days from its final successful app save**.
This is not a scan of your entire OneDrive or a rule based on arbitrary file age.

For an eligible, completed session, cleanup:

1. Removes its exact app-created `OfficeOnline/<session UUID>/` cloud folder.
2. Removes that session's local working folder and save-back backups.
3. Removes its history from the app's session store and fallback copy.

Cloud removal must succeed before local recovery data is removed. Interrupted
local cleanup retains enough session state for a later retry. Active, failed,
unsaved, or conflicted sessions are preserved. Original documents and conflict
results are protected; cleanup is limited to validated app session directories.

**Later edits made directly in OneDrive do not reset the app's retention date.**
Use the original local document for a new editing session if you want a new cycle.

Preview or run the same cleanup manually:

```sh
~/.local/bin/office-online cleanup --dry-run
~/.local/bin/office-online cleanup
```

## Files and privacy

All integration files are installed for the current user:

| Location | Purpose |
| --- | --- |
| `~/.local/bin/office-online` | Command-line launcher |
| `~/.local/bin/office-online-native` | Browser native-messaging host |
| `~/.config/office-online/` | App configuration and dedicated OneDrive authentication |
| `~/.local/share/office-online/` | Session state, working copies, and backups |
| `~/.config/systemd/user/office-online.service` | Background user service |
| `~/.local/share/nautilus-python/extensions/nautilus_office_online.py` | Files context-menu integration |
| `~/.config/{google-chrome,chromium}/NativeMessagingHosts/` | Restricted native-host manifests |
| `~/.local/share/applications/office-online*.desktop` | App and setup entries |
| `~/.local/share/applications/org.local.OfficeOnline.Uploader.desktop` | Uploader identity |
| `~/.local/share/icons/hicolor/scalable/apps/org.local.OfficeOnline.Uploader.svg` | Uploader icon |

XDG configuration/data/runtime overrides are supported. Authentication files
and the app's control socket are private to your user account. OneDrive stores
its authentication on disk; this app does not add a credential vault.

Documents you open are uploaded to **your Microsoft OneDrive**. Microsoft
account, storage, and retention policies apply. No anonymous sharing links are
created and no telemetry is added by this project. The extension has no content
scripts or broad website permissions and does not inspect document contents.

Credentials, document files, session databases, backups, local configuration,
and logs are excluded from the repository. Keep runtime data outside the checkout
and do not publish the contents of your app profile or full session output.

## Troubleshooting

```sh
~/.local/bin/office-online doctor
~/.local/bin/office-online status
systemctl --user status office-online.service
journalctl --user -u office-online.service -n 50 --no-pager
```

- **No right-click entry:** check `nautilus-python`, select only supported local
  files, and reload Files.
- **Service offline in the extension:** check the user service and native-host
  manifests, then reload the extension. Its ID must match the app configuration.
- **Sign-in or upload fails:** rerun `office-online setup` and check account policy,
  network access, and OneDrive storage availability.
- **Conflict:** compare the original with the separately saved cloud result.
  Cleanup deliberately leaves unresolved conflicts alone.
- **No uploader window:** check GTK4/PyGObject availability. Uploading can continue
  without the optional progress window; check CLI output and extension status.

Session output contains document names, local paths, and cloud URLs. Redact it
before opening a public issue. Never include OneDrive tokens or authentication files.

## Updating and uninstalling

Before updating or removing the app, save and close tracked editing tabs.

```sh
git pull --ff-only
./install.sh
systemctl --user restart office-online.service
```

The installer updates owned files and preserves app configuration. It does not
forcibly restart an already running helper, so restart it after updating code.

To remove the per-user integration:

```sh
python3 uninstall.py --dry-run
python3 uninstall.py
```

Uninstall retains app data, authentication, backups, and the checkout by default.
Remove the extension manually in `chrome://extensions`. The explicit
`python3 uninstall.py --purge` option also deletes the app's local configuration
and data, including credentials and backups; it does not delete cloud documents.

## Development and validation

The backend uses Python's standard library; the GTK UI and Nautilus provider use
system PyGObject. Extension tests use Node's built-in test runner.

```sh
python3 -m unittest discover -s tests -q
node --test tests/test_extension*.js
sh -n install.sh
```

Tests use temporary directories and simulated OneDrive/Chrome operations. They
cover saving, conflicts, recovery, native messaging, retention, installer
ownership, and extension tab tracking. See [TESTING.md](TESTING.md) for verification
scope, [CONTRACT.md](CONTRACT.md) for the integration protocol, and
[extension/README.md](extension/README.md) for browser details.

## Credits and license

Inspired by [Fmstrat/loffice-365](https://github.com/Fmstrat/loffice-365)'s
file-manager-to-OneDrive workflow. Uses the independently maintained
[OneDrive Client for Linux](https://github.com/abraunegg/onedrive).
Microsoft Office, OneDrive, and related names belong to their respective owners.

Released under the [MIT License](LICENSE).
