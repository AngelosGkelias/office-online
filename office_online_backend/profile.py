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


def ensure_quiet_profile(directory: Path, *, sync_dir: Path | None = None) -> None:
    """Set app options, including a sync root matching the CLI override.

    OneDrive compares ``--syncdir`` against the config file's ``sync_dir``
    (or its default ~/OneDrive), even on a fresh profile. Persist the app's
    root so its normal scoped commands do not demand a resync on every run.
    """
    settings = dict(QUIET_SETTINGS)
    if sync_dir is not None:
        settings["sync_dir"] = str(Path(sync_dir).absolute())
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
        if match and match.group(1) in settings:
            continue
        lines.append(line)
    content = "".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    content += "".join(f'{key} = "{value}"\n' for key, value in settings.items())
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
