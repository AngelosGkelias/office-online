"""OneDrive Client for Linux integration.

Verified against the current abraunegg/onedrive documentation and source
(November 2026):
  https://github.com/abraunegg/onedrive/blob/master/docs/usage.md
  https://github.com/abraunegg/onedrive/blob/master/docs/application-config-options.md

* ``onedrive --confdir DIR --syncdir DIR`` with no sync option performs
  authorisation only.  ``setup`` uses that form (or ``--reauth`` when a token
  already exists), so it never synchronises the whole drive.
* Upload is ``--sync --upload-only --no-remote-delete --single-directory``
  scoped to this session's folder.  Deletions are never mirrored online.
* Download is ``--sync --download-only --single-directory`` scoped to one
  session directory inside the isolated working drive. No cleanup is
  used outside the private session directory. Cleanup inside that directory
  ensures a deleted cloud file is never mistaken for a successful download.
* ``--get-file-link`` prints the authenticated ``webUrl`` of a synced item.
  Failure to retrieve it is an error; sharing permissions are never changed.

All invocations are list argv with ``shell=False`` and a timeout.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from .errors import OneDriveError
from .profile import ensure_quiet_profile
from .util import (
    ensure_private_dir,
    process_error_text,
    sha256_file,
    url_from_link_output,
)

LOGGER = logging.getLogger(__name__)

_AUTH_HINT = "run 'python3 office_online.py setup' first"
_ERROR_MARKER = re.compile(r"(?m)^\s*(?:ERROR|FATAL):")


@dataclass(frozen=True)
class DownloadedFile:
    path: Path
    sha256: str
    size: int


class OneDriveService:
    """Serialised-by-the-caller wrapper around the isolated OneDrive client."""

    def __init__(
        self,
        binary: str,
        config_dir: Path,
        drive_dir: Path,
        *,
        upload_timeout: float = 1800.0,
        download_timeout: float = 900.0,
        link_timeout: float = 120.0,
        version_timeout: float = 10.0,
        setup_timeout: float = 1800.0,
        runner: Optional[Callable[..., Any]] = None,
    ):
        self.binary = binary
        self.config_dir = Path(config_dir)
        self.drive_dir = Path(drive_dir)
        self.upload_timeout = upload_timeout
        self.download_timeout = download_timeout
        self.link_timeout = link_timeout
        self.version_timeout = version_timeout
        self.setup_timeout = setup_timeout
        self._runner = runner or subprocess.run

    @property
    def refresh_token_path(self) -> Path:
        return self.config_dir / "refresh_token"

    def is_authenticated(self) -> bool:
        path = self.refresh_token_path
        try:
            return path.is_file() and not path.is_symlink() and path.stat().st_size > 0
        except OSError:
            return False

    def binary_path(self) -> str:
        if "/" in self.binary or self.binary.startswith("."):
            candidate = Path(self.binary).expanduser()
            if not candidate.is_file():
                raise OneDriveError(
                    f"OneDrive client not found at {candidate}; install abraunegg/onedrive"
                )
            if not os.access(candidate, os.X_OK):
                raise OneDriveError(f"OneDrive client is not executable: {candidate}")
            return str(candidate)
        found = shutil.which(self.binary)
        if found is None:
            raise OneDriveError(
                f"OneDrive client '{self.binary}' was not found on PATH; "
                "install abraunegg/onedrive or set onedrive_binary in config.json"
            )
        return found

    def available(self) -> bool:
        try:
            self.binary_path()
            return True
        except OneDriveError:
            return False

    def version(self) -> str:
        binary = self.binary_path()
        result = self._run([binary, "--version"], timeout=self.version_timeout)
        if result.returncode != 0:
            detail = process_error_text(result.stderr, result.stdout)
            raise OneDriveError(
                "OneDrive client --version failed"
                + (f": {detail}" if detail else "")
            )
        first = (result.stdout or result.stderr or "").strip().splitlines()
        return first[0].strip() if first else "unknown"

    def _base_args(self) -> list[str]:
        return [self.binary_path(), "--confdir", str(self.config_dir)]

    def _run(self, argv: list[str], *, timeout: float) -> Any:
        LOGGER.debug("running OneDrive client action %s", _action_name(argv))
        try:
            return self._runner(
                argv,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError as error:
            raise OneDriveError(
                f"OneDrive client not found: {argv[0]}; install abraunegg/onedrive"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise OneDriveError(
                f"OneDrive {_action_name(argv)} timed out after {int(timeout)} seconds"
            ) from error
        except OSError as error:
            raise OneDriveError(
                f"could not start OneDrive client: {error.strerror}"
            ) from error

    def _ensure_authenticated(self) -> None:
        if not self.is_authenticated():
            raise OneDriveError(
                "the isolated Office Online OneDrive profile is not authenticated; "
                + _AUTH_HINT
            )

    @staticmethod
    def _raise_for_result(action: str, result: Any) -> None:
        combined = f"{result.stderr or ''}\n{result.stdout or ''}"
        if result.returncode != 0 or _ERROR_MARKER.search(combined):
            detail = process_error_text(result.stderr, result.stdout)
            message = f"OneDrive {action} failed"
            if detail:
                message += f": {detail}"
            raise OneDriveError(message)

    def upload_session(self, remote_dir: str) -> None:
        """Upload only this session's folder, never mirroring deletes online."""

        self._ensure_authenticated()
        _validate_relative_path(remote_dir, "remote directory")
        argv = self._base_args() + [
            "--syncdir",
            str(self.drive_dir),
            "--sync",
            "--upload-only",
            "--no-remote-delete",
            "--single-directory",
            remote_dir,
        ]
        result = self._run(argv, timeout=self.upload_timeout)
        self._raise_for_result("upload", result)

    def download_session(
        self, remote_dir: str, filename: str, sync_root: Path
    ) -> DownloadedFile:
        """Download only the session folder inside the isolated sync root."""

        self._ensure_authenticated()
        _validate_relative_path(remote_dir, "remote directory")
        _validate_filename(filename)
        root = Path(sync_root)
        ensure_private_dir(root)
        argv = self._base_args() + [
            "--syncdir",
            str(root),
            "--sync",
            "--download-only",
            "--cleanup-local-files",
            "--single-directory",
            remote_dir,
        ]
        result = self._run(argv, timeout=self.download_timeout)
        self._raise_for_result("download", result)

        expected = root.joinpath(*PurePosixPath(remote_dir).parts, filename)
        if expected.is_symlink():
            raise OneDriveError("OneDrive download produced a symlink; refusing to use it")
        if not expected.is_file():
            # The client is expected to preserve names, but a case-only
            # difference is harmless and still inside the narrow session folder.
            directory = expected.parent
            alternative = None
            if directory.is_dir():
                for candidate in directory.iterdir():
                    if candidate.name.casefold() == filename.casefold() and candidate.is_file():
                        if candidate.is_symlink():
                            raise OneDriveError(
                                "OneDrive download produced a symlink; refusing to use it"
                            )
                        alternative = candidate
                        break
            if alternative is None:
                raise OneDriveError(
                    "OneDrive reported a successful download but the expected file "
                    f"was not found ({filename})"
                )
            expected = alternative
        try:
            size = expected.stat().st_size
            digest = sha256_file(expected)
        except OSError as error:
            raise OneDriveError(
                f"could not read downloaded file: {error.strerror}"
            ) from error
        if size <= 0:
            raise OneDriveError("OneDrive downloaded an empty file")
        return DownloadedFile(path=expected, sha256=digest, size=size)

    def web_link(self, remote_relpath: str) -> Optional[str]:
        """Return the authenticated Microsoft webUrl, or None.

        ``--get-file-link`` does not create a sharing link; it returns the
        item's ``webUrl``.  The account that opens it must still authenticate
        with Microsoft, which is exactly what we want for normal editing.
        """

        self._ensure_authenticated()
        _validate_relative_path(remote_relpath, "remote path")
        argv = self._base_args() + [
            "--syncdir",
            str(self.drive_dir),
            "--get-file-link",
            remote_relpath,
        ]
        result = self._run(argv, timeout=self.link_timeout)
        if result.returncode != 0:
            LOGGER.info("--get-file-link did not return an authenticated web URL")
            return None
        return url_from_link_output(result.stdout or "")

    def link_for_editing(self, remote_relpath: str) -> tuple[str, str]:
        """Get the authenticated web URL without changing sharing permissions."""
        web = self.web_link(remote_relpath)
        if not web:
            raise OneDriveError(
                "OneDrive returned no authenticated Microsoft web URL. "
                "The uploaded copy is retained; no sharing link was created."
            )
        return web, "weburl"

    def remove_session_directory(self, session_id: str, remote_dir: str) -> None:
        """Delete only a specific app-created UUID directory, never its root."""
        import uuid
        from .config import REMOTE_ROOT
        try:
            canonical = str(uuid.UUID(session_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise OneDriveError("invalid cleanup session id") from error
        if session_id != canonical or remote_dir != f"{REMOTE_ROOT}/{canonical}":
            raise OneDriveError("cleanup path does not match an app session directory")
        self._ensure_authenticated()
        result = self._run(self._base_args() + [
            "--syncdir", str(self.drive_dir), "--remove-directory", remote_dir,
        ], timeout=self.link_timeout)
        self._raise_for_result("expired session cleanup", result)
        # This client can return exit 0 on some API errors. Require a positive
        # acknowledgment before recording a deletion as completed.
        output = (result.stdout or "").lower()
        if not any(text in output for text in (
            "requested directory to delete online has been deleted",
            "skipping removing the remote directory online as it does not exist",
        )):
            raise OneDriveError("OneDrive did not confirm expired session removal")

    def setup_command(self) -> list[str]:
        """Return the authentication-only command, never a sync command."""

        argv = self._base_args() + ["--syncdir", str(self.drive_dir)]
        if self.refresh_token_path.is_file():
            argv.append("--reauth")
        return argv

    def setup_interactive(self) -> int:
        """Run terminal authentication with inherited stdio.

        The command has no ``--sync``, ``--monitor``, ``--resync`` or
        ``--download-only`` flag, so the client authorises and exits rather
        than synchronising the account.
        """

        self.binary_path()
        ensure_quiet_profile(self.config_dir, sync_dir=self.drive_dir)
        ensure_private_dir(self.drive_dir)
        argv = self.setup_command()
        LOGGER.info("starting OneDrive authentication-only setup")
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                check=False,
                timeout=self.setup_timeout,
            )
        except FileNotFoundError as error:
            raise OneDriveError(
                "OneDrive client was not found; install abraunegg/onedrive"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise OneDriveError(
                f"OneDrive setup timed out after {int(self.setup_timeout)} seconds"
            ) from error
        except OSError as error:
            raise OneDriveError(f"could not start OneDrive setup: {error.strerror}") from error
        return int(completed.returncode)

    def doctor_summary(self) -> str:
        return (
            "OneDrive: "
            + ("configured" if self.is_authenticated() else "not authenticated")
            + f"; config={self.config_dir}; sync={self.drive_dir}; "
            "upload=upload-only/no-remote-delete; download=download-only/single-directory"
        )


def _validate_relative_path(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise OneDriveError(f"{label} is empty")
    if "\x00" in value:
        raise OneDriveError(f"{label} contains a NUL byte")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise OneDriveError(f"unsafe {label}: {value!r}")


def _validate_filename(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise OneDriveError("unsafe downloaded filename")


def _action_name(argv: list[str]) -> str:
    for option in (
        "--upload-only",
        "--download-only",
        "--get-file-link",
        "--create-share-link",
        "--version",
    ):
        if option in argv:
            return option.lstrip("-")
    return "operation"
