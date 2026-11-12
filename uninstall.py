#!/usr/bin/env python3
"""Uninstall the per-user Office Online integration.

Only files carrying the installer's ownership marker are removed.  By default
the app config, dedicated OneDrive authentication, session database, working
drive, and save-back backups are retained.  ``--purge`` explicitly removes the
app-specific config and data directories as well.

This script never removes the unpacked browser extension, never deletes cloud
copies, and never restarts or kills Nautilus/Chrome.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from install import (
    APP_NAME,
    InstallError,
    InstallPaths,
    MANAGED_MARKER,
    SERVICE_NAME,
    is_managed_file,
    resolve_paths,
)


def _read_install_manifest(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("product") != APP_NAME or data.get("managed_by") != MANAGED_MARKER:
        return None
    return data


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _run_systemctl_user(args: Sequence[str]) -> tuple[bool, str]:
    executable = shutil.which("systemctl")
    if not executable:
        return False, "systemctl was not found"
    try:
        result = subprocess.run(
            [executable, *args],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or result.stdout or "").strip()


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise InstallError(f"cannot remove {path}: {exc}") from exc


def _remove_app_directory(path: Path) -> None:
    if not _path_present(path):
        return
    try:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        raise InstallError(f"cannot remove {path}: {exc}") from exc


def _candidate_targets(paths: InstallPaths) -> list[tuple[Path, str]]:
    return [
        (paths.launcher, "launcher"),
        (paths.native_launcher, "native-messaging wrapper"),
        (paths.chrome_native_manifest, "Chrome native-messaging host"),
        (paths.chromium_native_manifest, "Chromium native-messaging host"),
        (paths.nautilus_extension, "Nautilus provider"),
        (paths.desktop_file, "desktop entry"),
        (paths.setup_desktop_file, "desktop setup helper"),
        (paths.uploader_desktop_file, "uploader desktop entry"),
        (paths.uploader_icon_file, "uploader icon"),
        (paths.service_file, "systemd user unit"),
        (paths.install_manifest, "install manifest"),
    ]


def uninstall(
    *,
    paths: InstallPaths,
    dry_run: bool = False,
    no_service: bool = False,
    purge: bool = False,
) -> int:
    manifest = _read_install_manifest(paths.install_manifest)
    manifest_service = bool(manifest and manifest.get("service") is True)
    service_file_managed = is_managed_file(paths.service_file)
    should_stop_service = not no_service and (
        manifest_service or service_file_managed
    )

    removable: list[tuple[Path, str]] = []
    skipped: list[tuple[Path, str]] = []
    for path, label in _candidate_targets(paths):
        if not _path_present(path):
            continue
        if path.is_dir():
            skipped.append((path, f"{label} is a directory"))
            continue
        if not is_managed_file(path):
            skipped.append((path, f"{label} is not owned by this project"))
            continue
        removable.append((path, label))

    if dry_run:
        print("Dry run: no files will be removed.")
        if should_stop_service:
            print(
                "Would stop and disable the systemd user service: "
                f"systemctl --user disable --now {SERVICE_NAME}"
            )
        elif no_service and _path_present(paths.service_file):
            print("Would leave the running service alone (--no-service).")
        for path, label in removable:
            print(f"Would remove: {path}  ({label})")
        for path, reason in skipped:
            print(f"Would leave:  {path}  ({reason})")
        if purge:
            print(f"Would purge config directory: {paths.config_dir}")
            print(f"Would purge data directory:   {paths.data_dir}")
        else:
            print(f"Would keep config and authentication: {paths.config_dir}")
            print(f"Would keep data, sessions, and backups: {paths.data_dir}")
        print("No browser or Nautilus process would be restarted.")
        return 0

    errors = 0

    if should_stop_service:
        ok, detail = _run_systemctl_user(
            ["--user", "disable", "--now", SERVICE_NAME]
        )
        if not ok:
            print(
                "warning: could not stop/disable the user service"
                + (f": {detail}" if detail else ""),
                file=sys.stderr,
            )
    elif no_service and _path_present(paths.service_file):
        print(
            "warning: --no-service skipped stopping the installed user service",
            file=sys.stderr,
        )

    service_file_removed = False
    for path, label in removable:
        try:
            _remove_file(path)
            print(f"Removed {label}: {path}")
            if path == paths.service_file:
                service_file_removed = True
        except InstallError as exc:
            errors += 1
            print(f"error: {exc}", file=sys.stderr)

    for path, reason in skipped:
        print(f"Left untouched: {path}  ({reason})")

    if service_file_removed and not no_service:
        ok, detail = _run_systemctl_user(["--user", "daemon-reload"])
        if not ok:
            print(
                "warning: systemctl --user daemon-reload failed"
                + (f": {detail}" if detail else ""),
                file=sys.stderr,
            )

    if purge:
        for directory in (paths.config_dir, paths.data_dir):
            try:
                _remove_app_directory(directory)
                print(f"Purged {directory}")
            except InstallError as exc:
                errors += 1
                print(f"error: {exc}", file=sys.stderr)
    else:
        print("")
        print(f"Kept app config and OneDrive authentication: {paths.config_dir}")
        print(f"Kept data, sessions, drive, and backups:      {paths.data_dir}")
        print("Use --purge only if you also want those deleted.")

    print("")
    print("The unpacked Chrome/Chromium extension was not removed.")
    print("Remove it manually in chrome://extensions if desired.")
    print(
        "Reopen Files, or run 'nautilus -q' only when you are willing to "
        "restart Nautilus, to unload the provider."
    )
    print("Chrome and Nautilus were not restarted or killed by this script.")
    return 1 if errors else 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove only the files installed by install.py. User data, "
            "authentication, and backups are kept unless --purge is used."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be removed without changing anything",
    )
    parser.add_argument(
        "--prefix",
        "--test-home",
        dest="prefix",
        metavar="DIR",
        help="treat DIR as the synthetic HOME used at install time",
    )
    parser.add_argument(
        "--no-service",
        action="store_true",
        help="do not call systemctl (the unit file is still removed if owned)",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help=(
            "also delete the app config/data directories, including dedicated "
            "OneDrive authentication, sessions, drive, and backups"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        paths = resolve_paths(args.prefix)
        return uninstall(
            paths=paths,
            dry_run=args.dry_run,
            no_service=args.no_service,
            purge=args.purge,
        )
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
