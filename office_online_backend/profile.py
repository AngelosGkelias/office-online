"""Quiet settings for this app's dedicated OneDrive profile only."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .util import ensure_private_dir, read_file_no_follow

QUIET_SETTINGS = {
    "disable_notifications": "true",
    "notify_file_actions": "false",
    "disable_version_check": "true",
}


def ensure_quiet_profile(directory: Path) -> None:
    """Preserve other options; suppress OneDrive UI and update reminders."""
    directory = Path(directory)
    ensure_private_dir(directory)
    path = directory / "config"
    if path.exists() or path.is_symlink():
        original = read_file_no_follow(path, max_bytes=1024 * 1024).decode("utf-8")
    else:
        original = ""
    lines = []
    for line in original.splitlines(keepends=True):
        match = re.match(r"^\s*([a-z_]+)\s*=", line)
        if match and match.group(1) in QUIET_SETTINGS:
            continue
        lines.append(line)
    content = "".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    content += "".join(f'{key} = "{value}"\n' for key, value in QUIET_SETTINGS.items())
    if content == original:
        return
    fd, temporary = tempfile.mkstemp(prefix=".quiet-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
