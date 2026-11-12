#!/bin/sh
# Install Office Online's per-user integration. No sudo or package changes.
set -eu

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3 is required. Install it using your distribution package manager.' >&2
    exit 1
fi

OFFICE_ONLINE_SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -f "$OFFICE_ONLINE_SOURCE_DIR/install.py" ]; then
    printf '%s\n' 'install.py is missing. Run install.sh from a complete project checkout.' >&2
    exit 1
fi

exec python3 "$OFFICE_ONLINE_SOURCE_DIR/install.py" "$@"
