"""Command-line interface and daemon wiring."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import socket
import stat
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from . import __version__, upload_ui
from .backend import BackendService
from .chrome import ChromeLauncher
from .config import AppConfig, Paths, default_config, load_config
from .errors import BackendError
from .native import run_native_host
from .onedrive import OneDriveService
from .server import MAX_REQUEST_LINE, UnixControlServer
from .sessions import SessionStore
from .util import (
    DOCUMENT_SUFFIXES,
    ensure_private_file,
    extension_id_is_valid,
    resolve_document_path,
)

LOGGER = logging.getLogger(__name__)

OPEN_TIMEOUT = 2100.0
STATUS_TIMEOUT = 5.0


def main(argv: Optional[list[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.command == "daemon":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s office-online[%(process)d] %(levelname)s %(message)s",
            stream=sys.stderr,
        )
    elif args.command == "native-host":
        logging.basicConfig(
            level=logging.WARNING,
            format="office-online-native: %(levelname)s %(message)s",
            stream=sys.stderr,
        )

    try:
        if args.command == "daemon":
            return command_daemon()
        if args.command == "native-host":
            return run_native_host(Paths.from_env())
        if args.command == "open":
            return command_open(args.paths, gui=args.gui)
        if args.command == "status":
            return command_status()
        if args.command == "setup":
            return command_setup()
        if args.command == "doctor":
            return command_doctor()
        if args.command == "cleanup":
            response = socket_request(Paths.from_env(),
                {"id": "cli-cleanup", "action": "cleanup", "dry_run": args.dry_run},
                timeout=1800)
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 0 if response.get("ok") and not any(
                item.get("action") == "error" for item in response.get("cleanup", [])
            ) else 1
        parser.print_help(sys.stderr)
        return 2
    except BackendError as error:
        print(f"office-online: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="office_online.py",
        description="Local DOCX/PPTX/XLSX editing through Office Online and a dedicated OneDrive profile.",
        epilog=(
            "OneDrive access: the preferred session URL is the authenticated Microsoft "
            "webUrl returned by 'onedrive --get-file-link'. Sharing permissions are "
            "never changed and anonymous links are never created. "
            "'setup' authenticates the isolated profile only and never syncs "
            "the whole drive. See CONTRACT.md for the extension/native protocol."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("daemon", help="run the long-lived user service (foreground)")
    native_parser = subparsers.add_parser(
        "native-host", help="Chrome native messaging proxy (internal)"
    )
    # Chrome appends the calling extension's origin (and may add
    # --parent-window) after the configured command.  It is informational for
    # this host, but argparse must not reject it.
    native_parser.add_argument("chrome_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    open_parser = subparsers.add_parser(
        "open", help="upload and open local DOCX/PPTX/XLSX files"
    )
    open_parser.add_argument("--gui", action="store_true", help="show upload progress and errors")
    open_parser.add_argument(
        "paths",
        nargs="+",
        help=(
            "local paths or file:// URIs; accepted suffixes: "
            + ", ".join(sorted(DOCUMENT_SUFFIXES))
        ),
    )
    subparsers.add_parser("status", help="print daemon/session status as JSON")
    subparsers.add_parser("setup", help="authenticate the isolated OneDrive profile")
    subparsers.add_parser("doctor", help="run offline installation checks")
    cleanup = subparsers.add_parser("cleanup", help="remove completed cloud copies older than seven days")
    cleanup.add_argument("--dry-run", action="store_true", help="list eligible copies without deleting")
    return parser


# --------------------------------------------------------------------- daemon


def command_daemon() -> int:
    paths = Paths.from_env()
    paths.ensure_base_dirs()
    config = load_config(paths, create_directory=True)
    if not extension_id_is_valid(config.extension_id):
        LOGGER.warning(
            "extension_id is missing/invalid in %s; 'open' will fail until configured",
            paths.config_file,
        )

    onedrive = OneDriveService(
        config.onedrive_binary,
        paths.onedrive_config_dir,
        paths.drive_dir,
    )
    store = SessionStore(paths)
    backend = BackendService(paths, config, store, onedrive)
    server = UnixControlServer(paths, backend)
    stop_event = threading.Event()

    def request_stop(signum: int, _frame: Any) -> None:
        LOGGER.info("received signal %s; shutting down", signum)
        stop_event.set()

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    try:
        # Take the singleton lock before loading/recovering session state so a
        # second daemon cannot rewrite sessions.json and then fail to bind.
        server.prepare()
        backend.recover_startup()
        server.bind()
        backend.start_cleanup()
        LOGGER.info(
            "office_online daemon started; config=%s data=%s",
            paths.config_dir,
            paths.data_dir,
        )
        server.serve_forever(stop_event)
        return 0
    finally:
        try:
            server.close()
        finally:
            backend.shutdown()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


# ----------------------------------------------------------------------- open


def command_open(raw_paths: list[str], *, gui: bool = False) -> int:
    paths = Paths.from_env()
    progress = None
    if gui:
        count = len(raw_paths)
        message = (
            upload_ui.DEFAULT_STATUS
            if count == 1
            else f"Uploading {count} working copies to OneDrive…"
        )
        progress = upload_ui.launch_upload_ui(message)
    try:
        resolved = [str(resolve_document_path(raw)) for raw in raw_paths]
        response = socket_request(
            paths,
            {"id": "cli-open", "action": "open", "paths": resolved},
            timeout=OPEN_TIMEOUT,
        )
        if not response.get("ok"):
            raise BackendError(
                str(response.get("error") or "could not open the document")
            )
    except BackendError as error:
        # The CLI remains the source of truth: closing or crashing the window
        # never cancels or alters the upload.  Show the message in a persistent
        # GTK error window when the uploader is available, and always keep the
        # terminal diagnostic.
        print(f"office-online: {error}", file=sys.stderr)
        if progress is not None:
            progress.fail(str(error))
            return 1
        raise
    except BaseException:
        # Ctrl-C or an unexpected failure: dismiss progress without pretending
        # it was an upload error.
        if progress is not None:
            progress.close()
        raise
    else:
        if progress is not None:
            progress.complete()
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


def command_status() -> int:
    paths = Paths.from_env()
    try:
        response = socket_request(
            paths,
            {"id": "cli-status", "action": "status"},
            timeout=STATUS_TIMEOUT,
        )
    except BackendError as error:
        response = {"id": "cli-status", "ok": False, "error": str(error)}
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0 if response.get("ok") else 1


# ---------------------------------------------------------------------- setup


def command_setup() -> int:
    paths = Paths.from_env()
    paths.ensure_base_dirs()
    config = load_config(paths, create_directory=True)
    if not extension_id_is_valid(config.extension_id):
        print(
            "office-online: warning: extension_id is not configured yet; "
            f"add it to {paths.config_file} before using 'open'",
            file=sys.stderr,
        )

    service = OneDriveService(
        config.onedrive_binary,
        paths.onedrive_config_dir,
        paths.drive_dir,
    )
    print(
        "Authenticating the isolated Office Online OneDrive profile.\n"
        f"  config: {paths.onedrive_config_dir}\n"
        f"  sync dir: {paths.drive_dir}\n"
        "This runs authentication only (no --sync, --monitor or --resync); "
        "it will not synchronise your whole OneDrive.",
        file=sys.stderr,
    )
    try:
        returncode = service.setup_interactive()
    except BackendError as error:
        print(f"office-online: {error}", file=sys.stderr)
        return 1
    if returncode != 0:
        print(
            f"office-online: OneDrive setup exited with status {returncode}",
            file=sys.stderr,
        )
        return 1
    if not service.is_authenticated():
        print(
            "office-online: OneDrive setup exited without creating a refresh token; "
            "authentication did not complete",
            file=sys.stderr,
        )
        return 1
    try:
        ensure_private_file(service.refresh_token_path)
    except BackendError as error:
        print(f"office-online: warning: {error}", file=sys.stderr)
    print(
        "OneDrive authentication is configured. No files were synchronised.\n"
        "Run 'office_online.py doctor' to verify the installation."
    )
    return 0


# --------------------------------------------------------------------- doctor


def command_doctor() -> int:
    paths = Paths.from_env()
    checks: list[tuple[str, str, str]] = []

    def add(level: str, label: str, detail: str = "") -> None:
        checks.append((level, label, detail))

    if sys.version_info >= (3, 9):
        add("ok", "Python", f"{sys.version.split()[0]} (need >= 3.9)")
    else:
        add("fail", "Python", f"{sys.version.split()[0]} is older than 3.9")

    config: AppConfig
    try:
        config = load_config(paths)
        if paths.config_file.exists():
            add("ok", "config.json", str(paths.config_file))
        else:
            add("fail", "config.json", f"missing: {paths.config_file}")
    except BackendError as error:
        add("fail", "config.json", str(error))
        config = default_config()

    if extension_id_is_valid(config.extension_id):
        add("ok", "extension_id", "32 characters in a-p")
    else:
        add(
            "fail",
            "extension_id",
            "missing or invalid; set 32 characters in a-p in config.json",
        )

    _check_private_path(
        checks, paths.config_dir, want_directory=True, required=False, label="config directory"
    )
    if paths.config_file.exists() or paths.config_file.is_symlink():
        _check_private_path(
            checks, paths.config_file, want_directory=False, required=True, label="config file"
        )
    else:
        add("fail", "config file", f"missing: {paths.config_file}")

    _check_private_path(
        checks, paths.data_dir, want_directory=True, required=False, label="data directory"
    )
    _check_private_path(
        checks, paths.drive_dir, want_directory=True, required=False, label="working drive"
    )
    _check_private_path(
        checks, paths.socket_dir, want_directory=True, required=False, label="socket directory"
    )
    if paths.drive_dir.is_dir():
        if os.access(paths.drive_dir, os.W_OK):
            add("ok", "working drive", f"writable: {paths.drive_dir}")
        else:
            add("fail", "working drive", f"not writable: {paths.drive_dir}")

    onedrive = OneDriveService(
        config.onedrive_binary,
        paths.onedrive_config_dir,
        paths.drive_dir,
    )
    try:
        binary = onedrive.binary_path()
    except BackendError as error:
        add("fail", "OneDrive client", str(error))
    else:
        add("ok", "OneDrive client", binary)
        try:
            version = onedrive.version()
            add("ok", "OneDrive version", version)
        except BackendError as error:
            add("fail", "OneDrive version", str(error))

    _check_private_path(
        checks,
        paths.onedrive_config_dir,
        want_directory=True,
        required=False,
        label="OneDrive config directory",
    )
    if onedrive.is_authenticated():
        add("ok", "OneDrive authentication", "isolated refresh token present")
        if onedrive.refresh_token_path.is_file():
            _check_private_path(
                checks,
                onedrive.refresh_token_path,
                want_directory=False,
                required=True,
                label="OneDrive token",
            )
    else:
        add(
            "fail",
            "OneDrive authentication",
            "missing; run 'python3 office_online.py setup'",
        )

    try:
        ChromeLauncher(config.browser).validate()
        add("ok", "Chrome", config.browser)
    except BackendError as error:
        add("fail", "Chrome", str(error))

    if shutil.which("notify-send"):
        add("ok", "notifications", "notify-send available")
    else:
        add("warn", "notifications", "notify-send not found; desktop notifications disabled")

    _check_daemon(checks, paths)

    for level, label, detail in checks:
        marker = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[fail]"}[level]
        line = f"{marker} {label}"
        if detail:
            line += f": {detail}"
        print(line)
    failures = sum(1 for level, _, _ in checks if level == "fail")
    warnings = sum(1 for level, _, _ in checks if level == "warn")
    print(
        f"\n{failures} failure(s), {warnings} warning(s). "
        + ("Installation is ready." if failures == 0 else "Installation needs attention.")
    )
    return 1 if failures else 0


def _check_private_path(
    checks: list[tuple[str, str, str]],
    path: Path,
    *,
    want_directory: bool,
    required: bool,
    label: Optional[str] = None,
) -> None:
    name = label or path.name
    try:
        info = path.lstat()
    except FileNotFoundError:
        level = "fail" if required else "warn"
        checks.append((level, name, f"missing: {path}"))
        return
    except OSError as error:
        checks.append(("fail", name, f"{path}: {error.strerror}"))
        return
    if stat.S_ISLNK(info.st_mode):
        checks.append(("fail", name, f"symlink refused: {path}"))
        return
    if want_directory and not stat.S_ISDIR(info.st_mode):
        checks.append(("fail", name, f"not a directory: {path}"))
        return
    if not want_directory and not stat.S_ISREG(info.st_mode):
        checks.append(("fail", name, f"not a regular file: {path}"))
        return
    if info.st_mode & 0o077:
        checks.append(("fail", name, f"permissions are not private (chmod 700/600): {path}"))
    else:
        checks.append(("ok", name, f"private: {path}"))


def _check_daemon(checks: list[tuple[str, str, str]], paths: Paths) -> None:
    socket_path = paths.socket_path
    try:
        info = socket_path.lstat()
    except FileNotFoundError:
        checks.append(
            ("warn", "daemon", "control socket not present (service is not running)")
        )
        return
    except OSError as error:
        checks.append(("fail", "daemon", f"{socket_path}: {error.strerror}"))
        return
    if not stat.S_ISSOCK(info.st_mode):
        checks.append(("fail", "daemon", f"not a Unix socket: {socket_path}"))
        return
    if info.st_mode & 0o077:
        checks.append(("fail", "daemon socket", f"permissions are not 0600: {socket_path}"))
    try:
        response = socket_request(
            paths,
            {"id": "doctor", "action": "status"},
            timeout=2.0,
        )
    except BackendError as error:
        checks.append(("fail", "daemon", f"socket exists but is not responding: {error}"))
        return
    if response.get("ok"):
        checks.append(("ok", "daemon", "control socket responds"))
    else:
        checks.append(("fail", "daemon", str(response.get("error", "status failed"))))


# ---------------------------------------------------------------- socket client


def socket_request(paths: Paths, request: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(paths.socket_path))
        sock.sendall(payload + b"\n")
        reader = sock.makefile("rb")
        try:
            line = reader.readline(MAX_REQUEST_LINE + 1)
        finally:
            reader.close()
    except FileNotFoundError as error:
        raise BackendError(
            f"daemon is not running (no socket at {paths.socket_path})"
        ) from error
    except ConnectionRefusedError as error:
        raise BackendError(
            "daemon is not accepting connections; start the user service"
        ) from error
    except socket.timeout as error:
        raise BackendError("timed out waiting for the office_online daemon") from error
    except OSError as error:
        raise BackendError(f"could not talk to the daemon: {error.strerror}") from error
    finally:
        sock.close()
    if not line:
        raise BackendError("daemon closed the control connection without a response")
    if not line.endswith(b"\n"):
        raise BackendError("daemon response was incomplete or too large")
    try:
        response = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackendError("daemon returned invalid JSON") from error
    if not isinstance(response, dict) or "id" not in response or "ok" not in response:
        raise BackendError("daemon returned an invalid response")
    if response.get("id") != request.get("id"):
        raise BackendError("daemon response id did not match the request")
    return response
