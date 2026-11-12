#!/usr/bin/env python3
"""Nautilus 4 menu provider for opening local Office files online.

Installed copy lives in ``~/.local/share/nautilus-python/extensions/``.  It only
offers the action when every selected item is a local regular file ending in
.docx, .pptx, or .xlsx.  The CLI is launched with an argv list and ``shell=False``
so filenames are never interpreted by a shell.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import unquote, urlsplit

SUPPORTED_EXTENSIONS = frozenset({".docx", ".pptx", ".xlsx"})
MENU_LABEL = "Open in Office Online"
OFFICE_ONLINE_BIN_ENV = "OFFICE_ONLINE_BIN"

# Keep this exact phrase: install.py/uninstall.py use it as the ownership marker.
MANAGED_MARKER = "Managed by office-online install.py"

try:  # Imported inside Nautilus. Keep the module importable for offline tests.
    import gi

    try:
        gi.require_version("Nautilus", "4.1")
    except ValueError:
        gi.require_version("Nautilus", "4.0")
    from gi.repository import GObject, Nautilus
except (ImportError, ValueError):  # pragma: no cover - exercised by Nautilus
    GObject = None  # type: ignore[assignment]
    Nautilus = None  # type: ignore[assignment]


def _as_path(value: object) -> Path | None:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value:
        return Path(value)
    return None


def local_file_path(file_info: object) -> Path | None:
    """Return a local path for a supported Nautilus FileInfo, else None."""

    try:
        uri = file_info.get_uri()  # type: ignore[attr-defined]
    except Exception:
        return None
    if not isinstance(uri, str) or not uri:
        return None

    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if parsed.scheme != "file":
        return None
    # file://host/path is a remote host and must not be treated as local.
    if parsed.netloc not in ("", "localhost"):
        return None
    if "\x00" in parsed.path:
        return None
    path_text = unquote(parsed.path)
    if not path_text or "\x00" in path_text:
        return None
    path = Path(path_text)
    if not path.is_absolute():
        return None
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None

    try:
        if file_info.is_directory():  # type: ignore[attr-defined]
            return None
    except Exception:
        pass
    try:
        if not path.is_file():
            return None
    except (OSError, ValueError):
        return None
    return path


def selection_paths(files: Iterable[object]) -> list[str]:
    """Return explicit local paths only when the whole selection is supported.

    An empty list means the menu entry must not be shown for this selection.
    """

    paths: list[str] = []
    for file_info in files:
        path = local_file_path(file_info)
        if path is None:
            return []
        paths.append(str(path))
    return paths


def find_office_online() -> Path | None:
    """Find the installed launcher without relying on Nautilus's PATH."""

    candidates: list[Path] = []
    override = os.environ.get(OFFICE_ONLINE_BIN_ENV)
    if override:
        candidate = _as_path(override)
        if candidate is not None:
            if not candidate.is_absolute():
                candidate = (Path.cwd() / candidate).resolve(strict=False)
            candidates.append(candidate)
    candidates.append(Path.home() / ".local" / "bin" / "office-online")
    found = shutil.which("office-online")
    if found:
        candidates.append(Path(found))

    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except (OSError, ValueError):
            continue
    return None


def launch_paths(paths: Sequence[str], binary: Path | str | None = None) -> bool:
    """Launch ``office-online open PATH...`` with an argv list (no shell)."""

    if not paths:
        return False
    executable = _as_path(binary) if binary is not None else find_office_online()
    if executable is None:
        return False
    if not executable.is_absolute():
        executable = (Path.cwd() / executable).resolve(strict=False)
    try:
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return False
    except (OSError, ValueError):
        return False

    argv = [str(executable), "open", "--gui", *[str(path) for path in paths]]
    try:
        subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


if Nautilus is not None and GObject is not None:

    class OfficeOnlineMenuProvider(GObject.GObject, Nautilus.MenuProvider):
        """Add "Open in Office Online" to eligible file selections."""

        def get_file_items(self, *args):
            # Nautilus 4 passes one argument (files). Variadic form also works
            # with the older API that passed (window, files).
            files = args[-1] if args else []
            paths = selection_paths(files)
            if not paths or find_office_online() is None:
                return []

            item = Nautilus.MenuItem(
                name="OfficeOnline::Open",
                label=MENU_LABEL,
            )
            item.connect("activate", lambda _item: launch_paths(paths))
            return [item]
