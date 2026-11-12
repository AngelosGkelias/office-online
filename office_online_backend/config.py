"""XDG paths and application configuration."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .errors import BackendError
from .util import ensure_private_dir, read_file_no_follow

DEFAULT_BROWSER = "google-chrome"
DEFAULT_ONEDRIVE_BINARY = "onedrive"
REMOTE_ROOT = "OfficeOnline"
NATIVE_HOST_NAME = "org.local.office_online"


def _xdg_home(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    if value:
        return Path(value).expanduser()
    return fallback


@dataclass(frozen=True)
class Paths:
    config_home: Path
    data_home: Path
    runtime_home: Path

    @classmethod
    def from_env(cls) -> "Paths":
        home = Path.home()
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            runtime_home = Path(runtime).expanduser()
        else:
            # XDG_RUNTIME_DIR should be set in a desktop session.  This
            # per-uid fallback keeps direct CLI use working without ever using
            # a world-writable socket directory.
            runtime_home = Path(tempfile.gettempdir()) / f"office-online-runtime-{os.getuid()}"
        return cls(
            config_home=_xdg_home("XDG_CONFIG_HOME", home / ".config"),
            data_home=_xdg_home("XDG_DATA_HOME", home / ".local" / "share"),
            runtime_home=runtime_home,
        )

    @property
    def config_dir(self) -> Path:
        return self.config_home / "office-online"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def onedrive_config_dir(self) -> Path:
        return self.config_dir / "onedrive"

    @property
    def data_dir(self) -> Path:
        return self.data_home / "office-online"

    @property
    def drive_dir(self) -> Path:
        return self.data_dir / "drive"

    @property
    def sessions_file(self) -> Path:
        return self.data_dir / "sessions.json"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def temporary_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def socket_dir(self) -> Path:
        return self.runtime_home / "office-online"

    @property
    def socket_path(self) -> Path:
        return self.socket_dir / "control.sock"

    def ensure_base_dirs(self) -> None:
        """Create the private directories this backend owns."""

        ensure_private_dir(self.config_dir)
        ensure_private_dir(self.data_dir)
        ensure_private_dir(self.drive_dir)
        ensure_private_dir(self.onedrive_config_dir)
        ensure_private_dir(self.backups_dir)
        ensure_private_dir(self.temporary_dir)
        ensure_private_dir(self.socket_dir)


@dataclass(frozen=True)
class AppConfig:
    extension_id: str
    browser: str
    onedrive_binary: str
    source: Optional[Path] = None


def default_config() -> AppConfig:
    return AppConfig(
        extension_id="",
        browser=DEFAULT_BROWSER,
        onedrive_binary=DEFAULT_ONEDRIVE_BINARY,
    )


def load_config(paths: Paths, *, create_directory: bool = False) -> AppConfig:
    """Load config.json, falling back to documented defaults if it is absent.

    A malformed config is a hard error; silently ignoring it would make the
    daemon use an empty extension id and fail later in a confusing way.
    """

    if create_directory:
        ensure_private_dir(paths.config_dir)
    path = paths.config_file
    try:
        raw = read_file_no_follow(path, max_bytes=128 * 1024)
    except BackendError as error:
        if not path.exists() and not path.is_symlink():
            return default_config()
        raise error
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackendError(f"could not parse {path}: {error}") from error
    if not isinstance(decoded, dict):
        raise BackendError(f"{path} must contain a JSON object")

    extension_id = decoded.get("extension_id", "")
    browser = decoded.get("browser", DEFAULT_BROWSER)
    onedrive_binary = decoded.get("onedrive_binary", DEFAULT_ONEDRIVE_BINARY)
    if not isinstance(extension_id, str):
        raise BackendError("config.json: extension_id must be a string")
    if not isinstance(browser, str) or not browser.strip():
        raise BackendError("config.json: browser must be a non-empty string")
    if not isinstance(onedrive_binary, str) or not onedrive_binary.strip():
        raise BackendError("config.json: onedrive_binary must be a non-empty string")
    return AppConfig(
        extension_id=extension_id.strip(),
        browser=browser.strip(),
        onedrive_binary=onedrive_binary.strip(),
        source=path,
    )


def config_as_dict(config: AppConfig) -> dict[str, Any]:
    return {
        "extension_id": config.extension_id,
        "browser": config.browser,
        "onedrive_binary": config.onedrive_binary,
    }
