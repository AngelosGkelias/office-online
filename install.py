#!/usr/bin/env python3
"""Install the per-user Office Online integration.

The installer is deliberately unprivileged.  It only writes the files needed to
run this project as the current user: launchers in ``~/.local/bin``, browser
native-messaging manifests, a Nautilus Python extension, a desktop entry, and an
optional systemd user unit.  It never invokes sudo, installs Chrome or
OneDrive, authenticates an account, or restarts Nautilus/Chrome.

Run ``python3 install.py --help`` for the test-friendly options.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from office_online_backend import upload_ui
from office_online_backend.profile import ensure_quiet_profile

APP_NAME = "office-online"
NATIVE_HOST_NAME = "org.local.office_online"
SERVICE_NAME = "office-online.service"
NATIVE_WRAPPER_NAME = "office-online-native"
NAUTILUS_FILENAME = "nautilus_office_online.py"
DESKTOP_FILENAME = "office-online.desktop"
SETUP_DESKTOP_FILENAME = "office-online-setup.desktop"
SETUP_APP_ID = "office-online-setup"
MANAGED_MARKER = "Managed by office-online install.py"
NATIVE_DESCRIPTION = (
    "Office Online local integration native messaging proxy. "
    + MANAGED_MARKER
)
INSTALL_MANIFEST_NAME = "install-manifest.json"
VERSION = 1

EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")
SUPPORTED_MIME_TYPES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
EXTENSION_DIR_CANDIDATES = (
    "extension",
    "chrome-extension",
    "browser-extension",
    "webextension",
    "chrome",
    "src/extension",
    "src/chrome-extension",
)


class InstallError(Exception):
    """A user-facing installation or uninstallation error."""


@dataclass(frozen=True)
class InstallPaths:
    """All per-user destinations used by the integration."""

    home: Path
    config_home: Path
    data_home: Path
    bin_dir: Path

    @property
    def config_dir(self) -> Path:
        return self.config_home / APP_NAME

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def data_dir(self) -> Path:
        return self.data_home / APP_NAME

    @property
    def install_manifest(self) -> Path:
        return self.data_dir / INSTALL_MANIFEST_NAME

    @property
    def launcher(self) -> Path:
        return self.bin_dir / APP_NAME

    @property
    def native_launcher(self) -> Path:
        return self.bin_dir / NATIVE_WRAPPER_NAME

    @property
    def chrome_native_dir(self) -> Path:
        return self.config_home / "google-chrome" / "NativeMessagingHosts"

    @property
    def chromium_native_dir(self) -> Path:
        return self.config_home / "chromium" / "NativeMessagingHosts"

    @property
    def chrome_native_manifest(self) -> Path:
        return self.chrome_native_dir / f"{NATIVE_HOST_NAME}.json"

    @property
    def chromium_native_manifest(self) -> Path:
        return self.chromium_native_dir / f"{NATIVE_HOST_NAME}.json"

    @property
    def native_manifests(self) -> tuple[Path, Path]:
        return (self.chrome_native_manifest, self.chromium_native_manifest)

    @property
    def nautilus_dir(self) -> Path:
        return self.data_home / "nautilus-python" / "extensions"

    @property
    def nautilus_extension(self) -> Path:
        return self.nautilus_dir / NAUTILUS_FILENAME

    @property
    def applications_dir(self) -> Path:
        return self.data_home / "applications"

    @property
    def desktop_file(self) -> Path:
        return self.applications_dir / DESKTOP_FILENAME

    @property
    def setup_desktop_file(self) -> Path:
        return self.applications_dir / SETUP_DESKTOP_FILENAME

    @property
    def uploader_desktop_file(self) -> Path:
        return self.applications_dir / upload_ui.DESKTOP_FILENAME

    @property
    def hicolor_icon_dir(self) -> Path:
        return self.data_home / "icons" / "hicolor"

    @property
    def uploader_icon_file(self) -> Path:
        return (
            self.hicolor_icon_dir
            / "scalable"
            / "apps"
            / f"{upload_ui.ICON_NAME}.svg"
        )

    @property
    def systemd_user_dir(self) -> Path:
        return self.config_home / "systemd" / "user"

    @property
    def service_file(self) -> Path:
        return self.systemd_user_dir / SERVICE_NAME


@dataclass(frozen=True)
class FileSpec:
    """A file this installer owns and may replace or remove."""

    path: Path
    content: str
    mode: int = 0o644


def _xdg_path(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    if not value:
        return fallback
    candidate = Path(value).expanduser()
    # The XDG spec says relative values are invalid and should be ignored.
    if not candidate.is_absolute():
        return fallback
    return candidate


def resolve_paths(prefix: str | None = None) -> InstallPaths:
    """Resolve XDG/user paths.

    ``--prefix`` (also accepted as ``--test-home``) means "treat this directory
    as a synthetic HOME" and intentionally ignores XDG_* overrides so a test can
    never escape the prefix.  Without it, XDG_CONFIG_HOME / XDG_DATA_HOME are
    honored and the launchers go under the real HOME's ``.local/bin``.
    """

    if prefix is not None:
        if not str(prefix).strip():
            raise InstallError("--prefix/--test-home must not be empty")
        raw_home = Path(prefix).expanduser()
        if not raw_home.is_absolute():
            raw_home = Path.cwd() / raw_home
        home = raw_home.resolve(strict=False)
        if home == Path("/"):
            raise InstallError("refusing to use / as an install prefix")
        config_home = home / ".config"
        data_home = home / ".local" / "share"
        bin_dir = home / ".local" / "bin"
    else:
        home = Path.home()
        config_home = _xdg_path("XDG_CONFIG_HOME", home / ".config")
        data_home = _xdg_path("XDG_DATA_HOME", home / ".local" / "share")
        bin_dir = home / ".local" / "bin"

    return InstallPaths(
        home=home,
        config_home=config_home,
        data_home=data_home,
        bin_dir=bin_dir,
    )


def compute_extension_id(public_key_b64: str) -> str:
    """Compute Chrome's stable extension ID from a manifest ``key`` value."""

    compact = "".join(public_key_b64.split())
    if not compact:
        raise InstallError("extension manifest key is empty")
    padding = "=" * (-len(compact) % 4)
    try:
        der = base64.b64decode(compact + padding, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InstallError(f"extension manifest key is not valid base64: {exc}") from exc
    if not der:
        raise InstallError("extension manifest key decodes to zero bytes")

    digest = hashlib.sha256(der).digest()[:16]
    extension_id = "".join(
        chr(ord("a") + (byte >> 4)) + chr(ord("a") + (byte & 0x0F))
        for byte in digest
    )
    if not EXTENSION_ID_RE.fullmatch(extension_id):
        raise InstallError("computed extension ID is invalid")
    return extension_id


def load_extension_manifest(extension_dir: Path) -> dict[str, Any]:
    manifest_path = extension_dir / "manifest.json"
    if not manifest_path.is_file():
        raise InstallError(
            f"extension manifest not found: {manifest_path}\n"
            "Pass --extension-dir with the unpacked Chrome extension directory."
        )
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InstallError(f"cannot read {manifest_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InstallError(f"invalid JSON in {manifest_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{manifest_path} must contain a JSON object")
    return data


def extension_id_from_manifest(manifest: dict[str, Any], manifest_path: Path) -> str:
    key = manifest.get("key")
    if not isinstance(key, str) or not key.strip():
        raise InstallError(
            f"{manifest_path} has no stable \"key\".\n"
            "Add the extension's public key under the top-level \"key\" field so "
            "its ID (and the native-messaging allow-list) stays stable."
        )
    return compute_extension_id(key)


def find_extension_dir(source_root: Path, explicit: str | None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve(strict=False)
        else:
            candidate = candidate.resolve(strict=False)
        if not (candidate / "manifest.json").is_file():
            raise InstallError(
                f"no manifest.json in extension directory: {candidate}"
            )
        return candidate

    for relative in EXTENSION_DIR_CANDIDATES:
        candidate = source_root / relative
        if (candidate / "manifest.json").is_file():
            return candidate
    raise InstallError(
        "could not find the unpacked Chrome extension (manifest.json).\n"
        "Pass --extension-dir DIR explicitly."
    )


def resolve_python_executable() -> Path:
    """Find the user's real Python 3, not a launcher around this process.

    Some packaged environments expose a non-python ``sys.executable`` (for
    example an AppImage wrapper).  Prefer the python3 found on PATH, which is
    also the venv interpreter when the installer is run inside one.
    """

    candidates: list[Path] = []
    override = os.environ.get("OFFICE_ONLINE_PYTHON")
    if override:
        candidates.append(Path(override).expanduser())
    found = shutil.which("python3")
    if found:
        candidates.append(Path(found))
    candidates.append(Path("/usr/bin/python3"))
    candidates.append(Path(sys.executable).expanduser())

    for candidate in candidates:
        if not candidate.is_absolute():
            resolved = shutil.which(str(candidate))
            if resolved:
                candidate = Path(resolved)
        candidate = candidate.resolve(strict=False)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise InstallError("could not find an executable Python 3 interpreter")


def is_managed_file(path: Path) -> bool:
    """Return True only for a regular file carrying this project's marker."""

    try:
        if not path.is_file():
            return False
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return MANAGED_MARKER in handle.read(8192)
    except OSError:
        return False


def _ensure_dir(path: Path, mode: int = 0o755) -> None:
    if path.exists():
        if not path.is_dir():
            raise InstallError(f"cannot use {path}: not a directory")
        return
    try:
        path.mkdir(parents=True, mode=mode, exist_ok=True)
        os.chmod(path, mode)
    except OSError as exc:
        raise InstallError(f"cannot create directory {path}: {exc}") from exc


def _write_atomic(path: Path, content: str, mode: int) -> None:
    _ensure_dir(path.parent)
    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent)
        )
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, mode)
    except OSError as exc:
        raise InstallError(f"cannot write {path}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def read_app_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise InstallError(f"app config is not a regular file: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"cannot read app config {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InstallError(
            f"refusing to overwrite invalid JSON config {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise InstallError(f"app config {path} must contain a JSON object")
    return data


def _config_mode(path: Path) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode) & 0o777
    except OSError:
        return 0o600


def render_launcher(python: Path, backend: Path) -> str:
    return (
        "#!/bin/sh\n"
        f"# {MANAGED_MARKER}. Re-run install.py to update this file.\n"
        f"exec {shlex.quote(str(python))} {shlex.quote(str(backend))} \"$@\"\n"
    )


def render_native_launcher(python: Path, backend: Path) -> str:
    return (
        "#!/bin/sh\n"
        f"# {MANAGED_MARKER}. Re-run install.py to update this file.\n"
        f"exec {shlex.quote(str(python))} {shlex.quote(str(backend))} native-host\n"
    )


def render_native_manifest(extension_id: str, native_launcher: Path) -> str:
    if not EXTENSION_ID_RE.fullmatch(extension_id):
        raise InstallError(f"invalid extension ID: {extension_id!r}")
    payload = {
        "name": NATIVE_HOST_NAME,
        "description": NATIVE_DESCRIPTION,
        "path": str(native_launcher),
        "type": "stdio",
        # Exactly one origin: the stable unpacked-extension ID. No wildcard.
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }
    return json.dumps(payload, indent=2) + "\n"


def _desktop_exec_arg(value: str) -> str:
    """Quote one literal Exec argument according to the Desktop Entry spec."""

    # A literal percent sign must be doubled because % introduces field codes.
    value = value.replace("%", "%%")
    if value and re.fullmatch(r"[A-Za-z0-9_./:@+=-]+", value):
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
    )
    return f'"{escaped}"'


def _desktop_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def _known_executable(name: str, absolute_fallback: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    fallback = Path(absolute_fallback)
    try:
        if fallback.is_file() and os.access(fallback, os.X_OK):
            return str(fallback)
    except OSError:
        pass
    return None


def setup_action_argv(paths: InstallPaths) -> list[str] | None:
    """Return an argv that opens the hidden setup entry in a terminal.

    ``Terminal=true`` is only valid in a [Desktop Entry] group, not in a
    [Desktop Action] group.  Launch the hidden setup desktop entry through GIO
    (which honors Terminal=true) instead of using an invalid action key.
    """

    gio = _known_executable("gio", "/usr/bin/gio")
    if gio:
        return [gio, "launch", str(paths.setup_desktop_file)]
    gtk_launch = _known_executable("gtk-launch", "/usr/bin/gtk-launch")
    if gtk_launch:
        return [gtk_launch, SETUP_APP_ID]
    return None


def render_desktop_entry(
    launcher: Path, setup_action: Sequence[str] | None = None
) -> str:
    exec_launcher = _desktop_exec_arg(str(launcher))
    mime = ";".join(SUPPORTED_MIME_TYPES) + ";"
    setup_lines = ""
    if setup_action:
        setup_exec = " ".join(_desktop_exec_arg(part) for part in setup_action)
        setup_lines = (
            "Actions=Setup;\n"
            "\n"
            "[Desktop Action Setup]\n"
            "Name=Set up OneDrive sign-in\n"
            "Exec=" + setup_exec + "\n"
        )
    return (
        "# Managed by office-online install.py\n"
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=Office Online\n"
        "GenericName=Microsoft Office Online editor\n"
        "Comment=Open local DOCX, PPTX, and XLSX files in Microsoft Office Online\n"
        "Exec=" + exec_launcher + " open --gui %F\n"
        "TryExec=" + _desktop_value(str(launcher)) + "\n"
        "Icon=x-office-document\n"
        "Terminal=false\n"
        "Categories=Office;\n"
        "MimeType=" + mime + "\n"
        "NoDisplay=false\n"
        "StartupNotify=true\n"
        "X-OfficeOnline-Managed=true\n"
        + setup_lines
    )


def render_setup_desktop_entry(launcher: Path) -> str:
    return (
        "# Managed by office-online install.py\n"
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=Set up OneDrive sign-in\n"
        "Comment=Authenticate the dedicated Office Online OneDrive profile\n"
        "Exec=" + _desktop_exec_arg(str(launcher)) + " setup\n"
        "Icon=utilities-terminal\n"
        "Terminal=true\n"
        "Categories=Utility;\n"
        "NoDisplay=true\n"
        "X-OfficeOnline-Managed=true\n"
    )


def render_uploader_desktop_entry(launcher: Path, icon_file: Path) -> str:
    """Render the hidden desktop entry that gives the GTK window its app ID.

    The desktop file ID (its basename without ``.desktop``), the
    ``Gtk.Application`` application ID, ``StartupWMClass`` and the icon basename
    are deliberately identical.  On Wayland GNOME Shell matches the xdg-toplevel
    app_id to this desktop file; keeping the IDs in sync makes the dock icon
    reliable instead of falling back to a generic cog.  ``Icon`` is an absolute
    path as well as being installed in the per-user hicolor theme, so a stale
    pre-existing user icon-theme cache cannot hide the new artwork.
    """

    return (
        "# Managed by office-online install.py\n"
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=Office Online Upload\n"
        "GenericName=Document upload progress\n"
        "Comment=Upload progress for local documents opened in Office Online\n"
        "Exec=" + _desktop_exec_arg(str(launcher)) + " open --gui %F\n"
        "TryExec=" + _desktop_value(str(launcher)) + "\n"
        "Icon=" + _desktop_value(str(icon_file)) + "\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "StartupNotify=true\n"
        "StartupWMClass=" + upload_ui.APPLICATION_ID + "\n"
        "X-OfficeOnline-Managed=true\n"
    )


def render_uploader_icon(svg_text: str) -> str:
    """Reuse the extension artwork, adding this installer's ownership marker."""

    if not svg_text.strip():
        raise InstallError("extension icon is empty: extension/icons/icon.svg")
    marker = (
        f"<!-- {MANAGED_MARKER}. Re-run install.py to update this file. -->"
    )
    text = svg_text.lstrip("\ufeff")
    if text.lstrip().startswith("<?xml"):
        declaration_end = text.find("?>")
        if declaration_end != -1:
            # An XML declaration, when present, must remain the first thing in
            # the document; the comment follows it.
            first_line_end = declaration_end + 2
            text = (
                text[:first_line_end]
                + "\n"
                + marker
                + "\n"
                + text[first_line_end:].lstrip("\r\n")
            )
        else:
            text = marker + "\n" + text
    else:
        text = marker + "\n" + text
    if not text.endswith("\n"):
        text += "\n"
    return text


def _systemd_arg(value: str) -> str:
    value = value.replace("%", "%%")
    if value and re.fullmatch(r"[A-Za-z0-9_./:@+=-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_service_unit(launcher: Path, working_directory: Path) -> str:
    return (
        "# Managed by office-online install.py\n"
        "[Unit]\n"
        "Description=Office Online local integration daemon\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={_systemd_arg(str(working_directory))}\n"
        f"ExecStart={_systemd_arg(str(launcher))} daemon\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "UMask=0077\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "TimeoutStopSec=20\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def build_file_specs(
    paths: InstallPaths,
    python: Path,
    backend: Path,
    source_root: Path,
    extension_id: str,
    include_service: bool,
    uploader_icon_svg: str,
) -> list[FileSpec]:
    setup_action = setup_action_argv(paths)
    specs = [
        FileSpec(
            paths.launcher,
            render_launcher(python, backend),
            0o755,
        ),
        FileSpec(
            paths.native_launcher,
            render_native_launcher(python, backend),
            0o755,
        ),
        FileSpec(
            paths.chrome_native_manifest,
            render_native_manifest(extension_id, paths.native_launcher),
            0o644,
        ),
        FileSpec(
            paths.chromium_native_manifest,
            render_native_manifest(extension_id, paths.native_launcher),
            0o644,
        ),
        FileSpec(
            paths.nautilus_extension,
            (source_root / NAUTILUS_FILENAME).read_text(encoding="utf-8"),
            0o644,
        ),
        FileSpec(
            paths.desktop_file,
            render_desktop_entry(paths.launcher, setup_action),
            0o644,
        ),
        FileSpec(
            paths.setup_desktop_file,
            render_setup_desktop_entry(paths.launcher),
            0o644,
        ),
        FileSpec(
            paths.uploader_desktop_file,
            render_uploader_desktop_entry(paths.launcher, paths.uploader_icon_file),
            0o644,
        ),
        FileSpec(
            paths.uploader_icon_file,
            render_uploader_icon(uploader_icon_svg),
            0o644,
        ),
    ]
    if include_service:
        specs.append(
            FileSpec(
                paths.service_file,
                render_service_unit(paths.launcher, source_root),
                0o644,
            )
        )
    return specs


def _preflight(specs: Iterable[FileSpec], force: bool) -> None:
    seen: set[Path] = set()
    for spec in specs:
        if spec.path in seen:
            raise InstallError(f"internal error: duplicate install target {spec.path}")
        seen.add(spec.path)
        _preflight_parent(spec.path)
        if spec.path.is_dir():
            raise InstallError(f"refusing to replace directory {spec.path}")
        if spec.path.exists() or spec.path.is_symlink():
            if not is_managed_file(spec.path) and not force:
                raise InstallError(
                    f"refusing to overwrite unowned file {spec.path}\n"
                    "Move it aside, or use --force if you are certain."
                )


def _preflight_parent(path: Path) -> None:
    """Reject an existing non-directory ancestor before any write happens."""

    parent = path.parent
    while True:
        if parent.exists():
            if not parent.is_dir():
                raise InstallError(
                    f"cannot create {path}: {parent} is not a directory"
                )
            return
        if parent == parent.parent:
            return
        parent = parent.parent


def _install_manifest_payload(
    paths: InstallPaths,
    extension_id: str,
    extension_dir: Path,
    source_root: Path,
    python: Path,
    service: bool,
    files: Sequence[Path],
) -> str:
    payload = {
        "product": APP_NAME,
        "version": VERSION,
        "managed_by": MANAGED_MARKER,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "extension_id": extension_id,
        "extension_dir": str(extension_dir),
        "source_root": str(source_root),
        "python": str(python),
        "service": service,
        "files": [str(path) for path in files],
    }
    return json.dumps(payload, indent=2) + "\n"


def _run_systemctl(args: Sequence[str]) -> None:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise InstallError(
            "systemctl was not found; files were installed but the user "
            "service was not started"
        )
    try:
        result = subprocess.run(
            [systemctl, *args],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise InstallError(f"cannot run systemctl: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise InstallError(
            "systemctl " + " ".join(args) + " failed"
            + (f": {detail}" if detail else "")
            + "\nFiles were installed. Fix the user systemd session and re-run "
            "install.py, or use --no-service for a non-service test install."
        )


def _warn_dependencies(browser: str) -> None:
    onedrive = shutil.which("onedrive")
    if not onedrive:
        print(
            "warning: the OneDrive client is not on PATH. Install the Fedora "
            "onedrive package before signing in (see README).",
            file=sys.stderr,
        )
    if not Path(browser).is_absolute():
        if not shutil.which(browser):
            print(
                f"warning: browser {browser!r} is not on PATH; set \"browser\" "
                f"in {APP_NAME}/config.json if needed.",
                file=sys.stderr,
            )
    elif not os.access(browser, os.X_OK):
        print(
            f"warning: browser {browser!r} is not executable.",
            file=sys.stderr,
        )


def install(
    *,
    source_root: Path,
    extension_dir: Path,
    paths: InstallPaths,
    dry_run: bool = False,
    no_service: bool = False,
    force: bool = False,
) -> int:
    source_root = source_root.resolve(strict=False)
    backend = source_root / "office_online.py"
    provider = source_root / NAUTILUS_FILENAME
    if not backend.is_file():
        raise InstallError(f"backend not found: {backend}")
    if not provider.is_file():
        raise InstallError(f"Nautilus provider not found: {provider}")

    manifest_path = extension_dir / "manifest.json"
    manifest = load_extension_manifest(extension_dir)
    extension_id = extension_id_from_manifest(manifest, manifest_path)
    uploader_icon_source = extension_dir / "icons" / "icon.svg"
    try:
        uploader_icon_svg = uploader_icon_source.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallError(
            f"cannot read the extension artwork {uploader_icon_source}: {exc}\n"
            "The uploader window reuses extension/icons/icon.svg for its "
            "per-user application icon."
        ) from exc
    python = resolve_python_executable()

    config = read_app_config(paths.config_file)
    config["extension_id"] = extension_id

    include_service = not no_service
    try:
        specs = build_file_specs(
            paths,
            python,
            backend,
            source_root,
            extension_id,
            include_service,
            uploader_icon_svg,
        )
    except OSError as exc:
        raise InstallError(f"cannot read source file: {exc}") from exc

    config_mode = _config_mode(paths.config_file)
    config_text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    manifest_files = [spec.path for spec in specs]
    install_manifest_spec = FileSpec(
        paths.install_manifest,
        _install_manifest_payload(
            paths,
            extension_id,
            extension_dir,
            source_root,
            python,
            include_service,
            manifest_files,
        ),
        0o600,
    )
    _preflight([*specs, install_manifest_spec], force)
    _preflight_parent(paths.config_file)

    if dry_run:
        print("Dry run: no files will be changed.")
        print(f"Extension directory: {extension_dir}")
        print(f"Extension ID: {extension_id}")
        print(f"Would write config: {paths.config_file}")
        print(f"Would quiet only the OneDrive profile: {paths.config_dir / 'onedrive'}")
        for spec in specs:
            print(f"Would install: {spec.path}")
        print(f"Would write install manifest: {paths.install_manifest}")
        if include_service:
            print(
                "Would enable and start the systemd user service: "
                f"systemctl --user daemon-reload; "
                f"systemctl --user enable --now {SERVICE_NAME}"
            )
        else:
            print("Service: skipped (--no-service)")
        print("No browser or Nautilus process would be restarted.")
        return 0

    _ensure_dir(paths.config_dir, 0o700)
    _ensure_dir(paths.data_dir, 0o700)
    ensure_quiet_profile(paths.config_dir / "onedrive", sync_dir=paths.data_dir / "drive")
    for spec in specs:
        _write_atomic(spec.path, spec.content, spec.mode)
    _write_atomic(paths.config_file, config_text, config_mode)
    _write_atomic(
        install_manifest_spec.path,
        install_manifest_spec.content,
        install_manifest_spec.mode,
    )

    if include_service:
        _run_systemctl(["--user", "daemon-reload"])
        _run_systemctl(["--user", "enable", "--now", SERVICE_NAME])

    browser = config.get("browser", "google-chrome")
    if not isinstance(browser, str) or not browser:
        browser = "google-chrome"
    _warn_dependencies(browser)

    print("Office Online integration installed.")
    print(f"  Extension directory: {extension_dir}")
    print(f"  Extension ID:        {extension_id}")
    print(f"  Launcher:            {paths.launcher}")
    print(f"  Native wrapper:      {paths.native_launcher}")
    print(f"  Config:              {paths.config_file}")
    print(f"  Nautilus provider:   {paths.nautilus_extension}")
    print(f"  Desktop entry:       {paths.desktop_file}")
    print(f"  Setup helper:        {paths.setup_desktop_file}")
    print(f"  Uploader entry:      {paths.uploader_desktop_file}")
    print(f"  Uploader icon:       {paths.uploader_icon_file}")
    if include_service:
        print(f"  User service:        {paths.service_file} (enabled/started)")
    else:
        print("  User service:        skipped (--no-service)")
    print("")
    print("Next steps:")
    print("  1. Load the unpacked extension in Chrome/Chromium (developer mode).")
    print("  2. Run: office-online setup")
    print("  3. Reopen Files, or run 'nautilus -q' only when you are willing to")
    print("     restart Nautilus. The installer does not restart it for you.")
    return 0


def _default_source_root() -> Path:
    return Path(__file__).resolve().parent


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Install the per-user Office Online integration. No sudo and no "
            "account authentication are performed."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without writing files or touching systemd",
    )
    parser.add_argument(
        "--prefix",
        "--test-home",
        dest="prefix",
        metavar="DIR",
        help=(
            "treat DIR as a synthetic HOME (for tests); implies "
            ".config, .local/share and .local/bin under DIR"
        ),
    )
    parser.add_argument(
        "--no-service",
        action="store_true",
        help="do not install or start the systemd user service (tests)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace existing files that do not carry this project's marker",
    )
    parser.add_argument(
        "--source",
        default=str(_default_source_root()),
        help="project directory containing office_online.py (default: script dir)",
    )
    parser.add_argument(
        "--extension-dir",
        default=None,
        help="unpacked extension directory containing manifest.json",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        paths = resolve_paths(args.prefix)
        source_root = Path(args.source).expanduser()
        if not source_root.is_absolute():
            source_root = (Path.cwd() / source_root).resolve(strict=False)
        extension_dir = find_extension_dir(source_root, args.extension_dir)
        return install(
            source_root=source_root,
            extension_dir=extension_dir,
            paths=paths,
            dry_run=args.dry_run,
            no_service=args.no_service,
            force=args.force,
        )
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
