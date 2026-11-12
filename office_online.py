#!/usr/bin/env python3
"""Office Online local integration backend.

Usage:
    python3 office_online.py daemon      # long-lived user systemd service
    python3 office_online.py native-host # Chrome native-messaging proxy (internal)
    python3 office_online.py open FILE...
    python3 office_online.py status
    python3 office_online.py setup       # OneDrive auth for the isolated profile
    python3 office_online.py doctor

The backend keeps its OneDrive authentication and sync root completely
separate from a normal OneDrive setup:

    config:  $XDG_CONFIG_HOME/office-online/onedrive
    drive:   $XDG_DATA_HOME/office-online/drive
    socket:  $XDG_RUNTIME_DIR/office-online/control.sock

``setup`` runs the abraunegg/onedrive client with no ``--sync``/``--monitor``
argument, so it authenticates only and never synchronises the whole drive.
Uploads use ``--upload-only --no-remote-delete`` and downloads use
``--download-only --single-directory`` scoped to one session directory.

OneDrive link access
--------------------
The preferred ``Session.url`` is the authenticated Microsoft ``webUrl``
returned by ``onedrive --get-file-link``; it does not create a sharing link and
the user authenticates normally in the browser. If that command cannot return
a web URL, opening fails without changing sharing permissions. Anonymous links
are never created.

The Chrome extension must be installed manually in developer mode.  This
backend never enables the extension, signs in for the user, or auto-loads
extension code.
"""

import sys

from office_online_backend.cli import main

if __name__ == "__main__":
    sys.exit(main())
