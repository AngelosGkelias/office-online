"""Core backend service: sessions, upload/save state machine and recovery.

HTTP/cloud calls never run while holding the session lock.  All OneDrive work
runs on one daemon worker thread, so status and bind requests remain
responsive while an upload or download is in progress.
"""

from __future__ import annotations

import logging
import os
import queue
import stat
import threading
import time
import uuid
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .chrome import ChromeLauncher
from .config import REMOTE_ROOT, AppConfig, Paths
from .errors import BackendError, OneDriveError
from .onedrive import DownloadedFile, OneDriveService
from .sessions import Session, SessionStore
from .util import (
    conflict_sibling_path,
    copy_file_atomic,
    copy_file_verified,
    copy_to_unique_sibling,
    ensure_private_dir,
    extension_id_is_valid,
    notify_desktop,
    redact_secrets,
    resolve_document_path,
    sanitize_onedrive_filename,
    sha256_file,
    utcnow_iso,
)

LOGGER = logging.getLogger(__name__)


class SerialWorker:
    """One daemon worker thread; queue order is the operation order."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="office-online-worker", daemon=True
        )
        self._thread.start()

    def submit(self, function: Callable[..., Any], *args: Any) -> Future:
        future: Future = Future()
        self._queue.put((future, function, args))
        return future

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, function, args = item
            if future.cancelled():
                continue
            try:
                result = function(*args)
            except BaseException as error:  # keep the daemon worker alive
                if not future.cancelled():
                    future.set_exception(error)
            else:
                if not future.cancelled():
                    future.set_result(result)

    def shutdown(self) -> None:
        self._queue.put(None)


@dataclass(frozen=True)
class SaveOutcome:
    status: str  # success | conflict
    result_path: str
    sha256: str
    backup_path: Optional[str]
    message: Optional[str] = None


class BackendService:
    def __init__(
        self,
        paths: Paths,
        config: AppConfig,
        store: SessionStore,
        onedrive: OneDriveService,
        *,
        launcher: Optional[ChromeLauncher] = None,
        notifier: Callable[..., bool] = notify_desktop,
        save_grace_seconds: float = 2.0,
        retry_initial_seconds: float = 5.0,
        retry_max_seconds: float = 300.0,
    ):
        self.paths = paths
        self.config = config
        self.store = store
        self.onedrive = onedrive
        self.launcher = launcher or ChromeLauncher(config.browser)
        self.notifier = notifier
        self.save_grace_seconds = max(0.0, save_grace_seconds)
        self.retry_initial_seconds = max(0.1, retry_initial_seconds)
        self.retry_max_seconds = max(self.retry_initial_seconds, retry_max_seconds)

        self._lock = threading.RLock()
        self._open_lock = threading.Lock()
        self._worker = SerialWorker()
        self._connections: dict[str, set[str]] = {}
        self._save_dirty: set[str] = set()
        self._save_running: set[str] = set()
        self._disconnect_timers: dict[str, threading.Timer] = {}
        self._retry_timers: dict[str, threading.Timer] = {}
        self._closing = False
        self._cleanup_timer: Optional[threading.Timer] = None

    # ------------------------------------------------------------------ status

    def public_sessions(self) -> list[dict[str, Any]]:
        return self.store.public_sessions()

    # Cleanup runs on the same serial worker as uploads/downloads.
    def start_cleanup(self, delay: float = 30.0) -> None:
        with self._lock:
            if self._closing:
                return
            if self._cleanup_timer is not None:
                self._cleanup_timer.cancel()
            self._cleanup_timer = threading.Timer(delay, self._cleanup_tick)
            self._cleanup_timer.daemon = True
            self._cleanup_timer.start()

    def _cleanup_tick(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._worker.submit(self.cleanup_expired)
        self.start_cleanup(6 * 60 * 60)

    def request_cleanup(self, dry_run: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_not_closing()
            future = self._worker.submit(self.cleanup_expired, dry_run)
        try:
            return future.result(timeout=1800)
        except TimeoutError as error:
            raise BackendError("cleanup is still queued or running; check status later") from error

    def cleanup_expired(self, dry_run: bool = False, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """Remove completed session data seven days after the final save.

        A completed session's cloud folder, local working directory and backup
        directory are removed, and its history record is pruned only after the
        cloud deletion is confirmed.  A pre-existing ``cloud_deleted_at`` marker
        (legacy or partial cleanup) skips the cloud step and retries only the
        local work.  Open/errored/conflicted/bound sessions never qualify.

        Local targets are derived from the private app roots and the canonical
        session UUID; stored ``working_path``/``backup_path`` values are only
        validated, never removed.  Symlinked roots, ancestors or targets and
        targets containing any session's original/result document are rejected
        before the cloud is touched.  Local failures keep the marker and record
        so a later run retries locally without repeating the cloud deletion.
        Dry runs perform the same local validation without network or changes.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=7)
        # Keep a union across the run: records pruned earlier must not stop
        # protecting their documents from a later session's target.
        protected = _protected_document_paths(self.store.snapshots())
        results = []
        for session in self.store.snapshots():
            if self._closing:
                break
            if not _cleanup_eligible(session, cutoff):
                continue
            # Also include sessions opened concurrently since the loop began.
            for path in _protected_document_paths(self.store.snapshots()):
                if path not in protected:
                    protected.append(path)
            result = {"session_id": session.session_id, "name": session.name}
            problem = self._cleanup_local_problem(session, protected)
            if problem is not None:
                result["action"] = "error"
                result["error"] = problem
                LOGGER.warning(
                    "expired session %s local cleanup refused: %s",
                    session.session_id,
                    problem,
                )
                results.append(result)
                continue
            if dry_run:
                result["action"] = "would_delete"
                results.append(result)
                continue

            if not session.cloud_deleted_at:
                try:
                    self.onedrive.remove_session_directory(
                        session.session_id, session.remote_dir
                    )
                except Exception as error:
                    result["action"] = "error"
                    result["error"] = _safe_error(error)
                    LOGGER.warning(
                        "expired cloud session %s cleanup failed", session.session_id
                    )
                    results.append(result)
                    continue
                try:
                    # Persist the confirmation before touching local files so a
                    # local failure retries locally only.
                    self.store.update(
                        session.session_id,
                        cloud_deleted_at=utcnow_iso(),
                        url=None,
                    )
                except Exception as error:
                    result["action"] = "error"
                    result["error"] = _safe_error(error)
                    LOGGER.warning(
                        "could not record cloud cleanup for %s", session.session_id
                    )
                    results.append(result)
                    continue

            try:
                for target in self._cleanup_local_targets(session):
                    _remove_cleanup_directory(target, self.paths.data_dir)
            except Exception as error:
                result["action"] = "error"
                result["error"] = _safe_error(error)
                LOGGER.warning(
                    "expired session %s local cleanup failed; will retry",
                    session.session_id,
                )
                results.append(result)
                continue

            try:
                self.store.prune([session.session_id])
            except Exception as error:
                result["action"] = "error"
                result["error"] = _safe_error(error)
                LOGGER.warning(
                    "expired session %s history could not be pruned; will retry",
                    session.session_id,
                )
                results.append(result)
                continue

            result["action"] = "deleted"
            LOGGER.info("removed expired session %s cloud and local data", session.session_id)
            results.append(result)
        return results

    def _cleanup_local_targets(self, session: Session) -> list[Path]:
        """Return fixed, app-owned UUID directories for an eligible session."""

        canonical = str(uuid.UUID(session.session_id))
        return [
            self.paths.drive_dir / REMOTE_ROOT / canonical,
            self.paths.backups_dir / canonical,
        ]

    def _cleanup_local_problem(
        self, session: Session, protected: list[Path]
    ) -> Optional[str]:
        """Validate local deletion before any cloud call; never use stored paths."""

        try:
            working_target, backup_target = self._cleanup_local_targets(session)
        except (ValueError, TypeError, AttributeError):
            return "session id is not a canonical UUID"
        stored_checks = (
            (session.working_path, working_target, "working copy"),
            (session.backup_path, backup_target, "backup"),
        )
        for stored, expected, label in stored_checks:
            if stored is None:
                continue
            try:
                stored_path = Path(stored)
            except (TypeError, ValueError):
                return f"stored {label} path is invalid; refusing to remove anything"
            if not _path_within(stored_path, expected):
                return (
                    f"stored {label} path is outside the expected session "
                    "directory; refusing to remove anything"
                )
        for target in (working_target, backup_target):
            problem = _cleanup_target_problem(target, self.paths.data_dir, protected)
            if problem is not None:
                return problem
        return None

    # -------------------------------------------------------------------- open

    def open_paths(self, raw_paths: list[str]) -> list[dict[str, Any]]:
        if not isinstance(raw_paths, list) or not raw_paths:
            raise BackendError("open requires a non-empty list of document paths")
        if len(raw_paths) > 100:
            raise BackendError("too many files requested at once")
        resolved: list[Path] = []
        seen: set[str] = set()
        for raw in raw_paths:
            if not isinstance(raw, str):
                raise BackendError("open paths must be strings")
            path = resolve_document_path(raw)
            key = str(path)
            if key not in seen:
                seen.add(key)
                resolved.append(path)
        for path in resolved:
            self._open_one(path)
        return self.public_sessions()

    def _open_one(self, original: Path) -> None:
        with self._open_lock:
            self._ensure_not_closing()
            existing = self._reusable_session(original)
            if existing is not None:
                # Reopening an errored-but-uploaded session re-engages it
                # instead of leaving the bridge on a dead error page.  The
                # extension decides whether to focus the mapped tab or
                # navigate the bridge tab; no second upload happens here.
                if existing.state == "error" and existing.url:
                    with self._lock:
                        self._cancel_retry_locked(existing.session_id)
                        existing = self.store.update(
                            existing.session_id,
                            state="editing",
                            error=None,
                            finish_requested=False,
                        )
                self._launch_session(existing, mark_error=False)
                return

            self._preflight()
            session = self._create_session(original)
            self.store.add(session)
            future = self._worker.submit(self._initial_upload, session.session_id)
            timeout = (
                float(getattr(self.onedrive, "upload_timeout", 1800.0))
                + float(getattr(self.onedrive, "link_timeout", 120.0))
                + 60.0
            )
            try:
                future.result(timeout=timeout)
            except TimeoutError as error:
                raise BackendError(
                    "timed out waiting for the initial OneDrive upload; "
                    "the daemon may still finish it in the background"
                ) from error
            except BackendError as error:
                raise BackendError(str(error)) from error
            except Exception as error:
                LOGGER.error(
                    "initial upload failed for session %s: %s",
                    session.session_id,
                    _safe_error(error),
                )
                raise BackendError("initial upload failed") from error

            current = self.store.snapshot(session.session_id)
            if current is None or current.state != "editing" or not current.url:
                message = (
                    current.error
                    if current is not None and current.error
                    else "initial upload did not become ready"
                )
                raise BackendError(message)
            self._launch_session(current, mark_error=True)

    def _preflight(self) -> None:
        if not extension_id_is_valid(self.config.extension_id):
            raise BackendError(
                "extension_id is not configured correctly in config.json "
                "(expected 32 characters in a-p)"
            )
        self.launcher.validate()
        available = getattr(self.onedrive, "available", None)
        if callable(available) and not available():
            raise OneDriveError(
                "OneDrive client is not installed or not on PATH; "
                "install abraunegg/onedrive and run 'python3 office_online.py setup'"
            )
        authenticated = getattr(self.onedrive, "is_authenticated", None)
        if callable(authenticated) and not authenticated():
            raise OneDriveError(
                "the isolated office-online OneDrive profile is not authenticated; "
                "run 'python3 office_online.py setup' first"
            )

    def _reusable_session(self, original: Path) -> Optional[Session]:
        matches = [
            session
            for session in self.store.snapshots()
            if session.original_path == str(original)
            and session.url
            and session.state in {"opening", "editing", "saving", "error", "conflict"}
        ]
        if not matches:
            return None
        matches.sort(key=lambda item: (item.created_at, item.session_id))
        return matches[-1]

    def _create_session(self, original: Path) -> Session:
        session_id = str(uuid.uuid4())
        remote_dir = f"{REMOTE_ROOT}/{session_id}"
        filename = sanitize_onedrive_filename(original.name)
        working_dir = self.paths.drive_dir.joinpath(*remote_dir.split("/"))
        ensure_private_dir(working_dir)
        working_path = working_dir / filename
        digest = copy_file_verified(original, working_path)
        return Session(
            session_id=session_id,
            name=original.name,
            original_path=str(original),
            url=None,
            state="opening",
            tab_id=None,
            error=None,
            last_saved_at=None,
            result_path=None,
            working_path=str(working_path),
            remote_dir=remote_dir,
            remote_relpath=f"{remote_dir}/{filename}",
            local_hash=digest,
        )

    def _initial_upload(self, session_id: str) -> None:
        try:
            session = self.store.snapshot(session_id)
            if session is None:
                raise BackendError("session disappeared before upload")
            self.onedrive.upload_session(session.remote_dir)
            url, mode = self.onedrive.link_for_editing(session.remote_relpath)
            self.store.update(
                session_id,
                state="editing",
                url=url,
                link_mode=mode,
                error=None,
                retry_count=0,
            )
            LOGGER.info("session %s is ready for the extension bridge", session_id)
        except Exception as error:
            message = _safe_error(error)
            LOGGER.error("initial upload failed for %s: %s", session_id, message)
            try:
                self.store.update(session_id, state="error", error=message)
            except BackendError:
                pass
            self._notify("Office Online error", f"Could not open {self._name(session_id)}: {message}", "critical")
            raise

    def _launch_session(self, session: Session, *, mark_error: bool) -> None:
        try:
            self.launcher.launch(self.config.extension_id, session.session_id)
        except BackendError as error:
            if mark_error:
                try:
                    self.store.update(
                        session.session_id,
                        state="error",
                        error=_safe_error(error),
                    )
                except BackendError:
                    pass
            self._notify(
                "Office Online error",
                f"Could not start Chrome for {session.name}: {_safe_error(error)}",
                "critical",
            )
            raise

    # -------------------------------------------------------------------- save

    def request_save(self, session_id: str, finish: bool) -> list[dict[str, Any]]:
        if not isinstance(finish, bool):
            raise BackendError("finish must be a boolean")
        with self._lock:
            self._ensure_not_closing()
            session = self.store.snapshot(session_id)
            if session is None:
                raise BackendError(f"unknown session: {session_id}")
            if session.state == "saved":
                return self.store.public_sessions()
            if not session.url or not session.working_path:
                raise BackendError("session is not ready for saving")
            self._cancel_retry_locked(session_id)
            changes: dict[str, Any] = {"state": "saving", "error": None}
            if finish:
                changes["finish_requested"] = True
            self.store.update(session_id, **changes)
            self._save_dirty.add(session_id)
            self._schedule_save_locked(session_id)
        return self.store.public_sessions()

    def tab_closed(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_not_closing()
            session = self.store.snapshot(session_id)
            if session is None:
                raise BackendError(f"unknown session: {session_id}")
            if session.state == "saved":
                return self.store.public_sessions()
            if not session.url:
                raise BackendError("session has no editable OneDrive URL to save")
            self._cancel_disconnect_locked(session_id)
            self._detach_locked(session, None)
            # Queue while holding the lock so a reconnect cannot slip between
            # detaching the old tab and starting the final save.
            return self.request_save(session_id, finish=True)

    def retry(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_not_closing()
            session = self.store.snapshot(session_id)
            if session is None:
                raise BackendError(f"unknown session: {session_id}")
            if session.state == "saved":
                return self.store.public_sessions()
            if not session.url or not session.working_path:
                raise BackendError("this session has no recoverable cloud copy")
            self._cancel_retry_locked(session_id)
            self.store.update(
                session_id,
                state="saving",
                error=None,
                recovery_only=True,
                retry_count=0,
            )
            self._save_dirty.add(session_id)
            self._schedule_save_locked(session_id)
        return self.store.public_sessions()

    def _schedule_save_locked(self, session_id: str) -> None:
        if session_id in self._save_running:
            return
        self._save_running.add(session_id)
        self._worker.submit(self._save_loop, session_id)

    def _save_loop(self, session_id: str) -> None:
        while True:
            with self._lock:
                if self._closing:
                    self._save_running.discard(session_id)
                    return
                if session_id not in self._save_dirty:
                    self._save_running.discard(session_id)
                    return
                self._save_dirty.discard(session_id)
                session = self.store.snapshot(session_id)
                if session is None or session.state == "saved":
                    self._save_running.discard(session_id)
                    return
                finish = session.finish_requested
                recovery_only = session.recovery_only

            try:
                outcome = self._perform_save(
                    session_id, upload=not recovery_only
                )
            except Exception as error:
                self._handle_save_error(session_id, error)
                with self._lock:
                    # A manual retry may have arrived while the failing
                    # operation was still running.  Do not discard it.
                    if session_id in self._save_dirty and not self._closing:
                        continue
                    self._save_running.discard(session_id)
                    self._save_dirty.discard(session_id)
                return

            if outcome.status == "conflict":
                self._handle_conflict(session_id, outcome)
                with self._lock:
                    if session_id in self._save_dirty and not self._closing:
                        continue
                    self._save_running.discard(session_id)
                    self._save_dirty.discard(session_id)
                return

            saved_now = False
            notify_final = False
            with self._lock:
                current = self.store.snapshot(session_id)
                if current is None:
                    self._save_running.discard(session_id)
                    return
                now = utcnow_iso()
                self._cancel_retry_locked(session_id)
                if finish:
                    self.store.update(
                        session_id,
                        state="saved",
                        tab_id=None,
                        error=None,
                        last_saved_at=now,
                        result_path=outcome.result_path,
                        backup_path=outcome.backup_path,
                        local_hash=outcome.sha256,
                        finish_requested=False,
                        recovery_only=False,
                        retry_count=0,
                        bound_connection=None,
                    )
                    self._remove_from_connection_locked(current.bound_connection, session_id)
                    self._save_dirty.discard(session_id)
                    self._save_running.discard(session_id)
                    saved_now = True
                    notify_final = True
                else:
                    self.store.update(
                        session_id,
                        state="editing",
                        error=None,
                        last_saved_at=now,
                        result_path=outcome.result_path,
                        backup_path=outcome.backup_path,
                        local_hash=outcome.sha256,
                        recovery_only=False,
                        retry_count=0,
                    )
                    # A tab_closed/finish request may have arrived while the
                    # upload was running.  In that case run one more pass.
                    refreshed = self.store.snapshot(session_id)
                    if refreshed is not None and refreshed.finish_requested:
                        self._save_dirty.add(session_id)
                    if session_id not in self._save_dirty:
                        self._save_running.discard(session_id)
                        return
            if notify_final:
                self._notify(
                    "Office Online save complete",
                    f"{self._name(session_id)} was saved back to {outcome.result_path}",
                    "normal",
                )
                return
            if saved_now:
                return
            # Loop for the queued finishing save.

    def _perform_save(self, session_id: str, *, upload: bool) -> SaveOutcome:
        session = self.store.snapshot(session_id)
        if session is None:
            raise BackendError("session disappeared before save")
        if not session.working_path or not session.remote_dir or not session.remote_relpath:
            raise BackendError("session is missing its working-copy or OneDrive path")

        working = Path(session.working_path)
        if not _is_within(working, self.paths.drive_dir):
            raise BackendError("session working copy is outside the private drive directory")
        if not working.is_file():
            raise BackendError("session working copy is missing")

        # Saving is strictly cloud-to-local. Never upload a working snapshot
        # over edits made in Office Online. Only initial opening uploads.

        # Download into the same isolated working drive (not a second sync
        # root): the OneDrive client's database hashes and the working copy
        # then stay consistent, so a later upload cannot resurrect a stale
        # local snapshot over newer cloud edits.
        downloaded = self.onedrive.download_session(
            session.remote_dir,
            Path(session.remote_relpath).name,
            self.paths.drive_dir,
        )
        if not _is_within(downloaded.path, self.paths.drive_dir):
            raise BackendError("OneDrive download escaped the private drive directory")
        if not downloaded.path.is_file() or downloaded.path.is_symlink():
            raise BackendError("OneDrive download is not a regular file")
        actual_hash = sha256_file(downloaded.path)
        if actual_hash != downloaded.sha256:
            raise BackendError("downloaded file changed while it was being verified")
        if downloaded.path.resolve() != working.resolve():
            # Case-only path difference: keep the canonical working copy in
            # sync with the database and with the bytes we are about to apply.
            copy_file_atomic(
                downloaded.path,
                working,
                mode=0o600,
                preserve_metadata=True,
            )
        return self._save_back(session, downloaded)

    def _save_back(self, session: Session, downloaded: DownloadedFile) -> SaveOutcome:
        original = Path(session.original_path)
        try:
            info = original.lstat()
        except FileNotFoundError:
            info = None
        except OSError as error:
            raise BackendError(f"could not inspect the original file: {error.strerror}") from error

        if info is not None and stat.S_ISLNK(info.st_mode):
            # The user (or another process) replaced the original with a
            # symlink.  Never write through it; preserve the downloaded copy
            # as a sibling instead.
            proposed = conflict_sibling_path(original)
            sibling, digest = copy_to_unique_sibling(
                downloaded.path, original.parent, proposed
            )
            return SaveOutcome(
                status="conflict",
                result_path=str(sibling),
                sha256=digest,
                backup_path=None,
                message=(
                    "The original path became a symbolic link; it was left untouched. "
                    f"The downloaded cloud copy is at {sibling}."
                ),
            )

        if info is None:
            proposed = conflict_sibling_path(original)
            sibling, digest = copy_to_unique_sibling(
                downloaded.path, original.parent, proposed
            )
            return SaveOutcome(
                status="conflict",
                result_path=str(sibling),
                sha256=digest,
                backup_path=None,
                message=(
                    "The original file disappeared while saving; it was not recreated. "
                    f"The downloaded cloud copy is at {sibling}."
                ),
            )

        if not stat.S_ISREG(info.st_mode):
            raise BackendError("the original path is no longer a regular file")

        try:
            current_hash = sha256_file(original)
        except OSError as error:
            raise BackendError(f"could not hash the original file: {error.strerror}") from error

        backup_path = self._backup_original(session, original)
        expected_hash = session.local_hash
        mode = stat.S_IMODE(info.st_mode) or 0o600

        if current_hash == downloaded.sha256:
            # The original already contains the cloud version (for example a
            # user resolved a conflict by copying the downloaded sibling).
            # Normalise it atomically; there is no content to lose.
            copy_file_atomic(
                downloaded.path,
                original,
                mode=mode,
                preserve_metadata=True,
            )
            return SaveOutcome(
                status="success",
                result_path=str(original),
                sha256=downloaded.sha256,
                backup_path=backup_path,
            )

        if expected_hash and current_hash == expected_hash:
            # Recheck immediately before the atomic replace.  This narrows the
            # unavoidable race with external writers to the minimum.
            try:
                again = sha256_file(original)
            except OSError as error:
                raise BackendError(
                    f"could not re-check the original file: {error.strerror}"
                ) from error
            if again != expected_hash:
                proposed = conflict_sibling_path(original)
                sibling, digest = copy_to_unique_sibling(
                    downloaded.path, original.parent, proposed, mode=mode
                )
                return SaveOutcome(
                    status="conflict",
                    result_path=str(sibling),
                    sha256=digest,
                    backup_path=backup_path,
                    message=(
                        "The original file changed during save-back; it was preserved "
                        f"and the cloud copy was written to {sibling}."
                    ),
                )
            copy_file_atomic(
                downloaded.path,
                original,
                mode=mode,
                preserve_metadata=True,
            )
            return SaveOutcome(
                status="success",
                result_path=str(original),
                sha256=downloaded.sha256,
                backup_path=backup_path,
            )

        proposed = conflict_sibling_path(original)
        sibling, digest = copy_to_unique_sibling(
            downloaded.path, original.parent, proposed, mode=mode
        )
        return SaveOutcome(
            status="conflict",
            result_path=str(sibling),
            sha256=digest,
            backup_path=backup_path,
            message=(
                "The original file was modified outside Office Online; it was left "
                f"untouched and the downloaded cloud copy was written to {sibling}."
            ),
        )

    def _backup_original(self, session: Session, original: Path) -> str:
        backup_dir = self.paths.backups_dir / session.session_id
        ensure_private_dir(backup_dir)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        safe_name = sanitize_onedrive_filename(session.name or original.name)
        backup, _ = copy_to_unique_sibling(
            original,
            backup_dir,
            f"{stamp}-{safe_name}",
        )
        return str(backup)

    def _handle_save_error(self, session_id: str, error: Exception) -> None:
        message = _safe_error(error)
        LOGGER.error("save failed for %s: %s", session_id, message)
        with self._lock:
            session = self.store.snapshot(session_id)
            if session is None:
                return
            retries = session.retry_count + 1
            try:
                self.store.update(
                    session_id,
                    state="error",
                    error=message,
                    retry_count=retries,
                    # A retry must download before it can possibly upload, so
                    # a stale working snapshot can never overwrite cloud edits.
                    recovery_only=True,
                )
            except BackendError:
                return
            if self._closing:
                return
            first_failure = retries <= 1
        if first_failure:
            self._notify(
                "Office Online save error",
                f"Could not save {self._name(session_id)}: {message}. It will be retried.",
                "critical",
            )
        self._schedule_retry(session_id)

    def _handle_conflict(self, session_id: str, outcome: SaveOutcome) -> None:
        message = outcome.message or "the file changed while it was being saved"
        with self._lock:
            try:
                self.store.update(
                    session_id,
                    state="conflict",
                    error=message,
                    result_path=outcome.result_path,
                    backup_path=outcome.backup_path,
                    retry_count=0,
                )
            except BackendError:
                return
            self._cancel_retry_locked(session_id)
        self._notify(
            "Office Online conflict",
            f"{self._name(session_id)} was not overwritten. The cloud copy is at "
            f"{outcome.result_path}.",
            "critical",
        )

    # ----------------------------------------------------------------- recovery

    def recover_startup(self) -> None:
        """Make interrupted work durable and schedule safe retries."""

        immediate_retries: list[str] = []
        with self._lock:
            for session in self.store.snapshots():
                changes: dict[str, Any] = {}
                if session.bound_connection is not None:
                    changes["bound_connection"] = None
                if session.tab_id is not None:
                    changes["tab_id"] = None
                schedule = False
                if session.state == "opening":
                    changes.update(
                        state="error",
                        error=(
                            "opening was interrupted by a daemon restart; "
                            "open the document again or use retry if it has a URL"
                        ),
                    )
                elif session.state == "saving":
                    changes.update(
                        state="error",
                        error=(
                            "saving was interrupted by a daemon restart; "
                            "the latest cloud copy will be recovered"
                        ),
                        recovery_only=True,
                        finish_requested=True,
                        retry_count=0,
                    )
                    schedule = True
                elif session.state == "editing":
                    changes.update(
                        state="error",
                        error=(
                            "the daemon restarted while this document was editing; "
                            "reopen it or use retry to recover the latest cloud copy"
                        ),
                        recovery_only=True,
                        finish_requested=True,
                        retry_count=0,
                    )
                    schedule = True
                elif (
                    session.state == "error"
                    and session.url
                    and (session.finish_requested or session.recovery_only)
                ):
                    schedule = True
                if changes:
                    try:
                        self.store.update(session.session_id, **changes)
                    except BackendError:
                        LOGGER.exception("could not persist recovery for %s", session.session_id)
                if schedule:
                    immediate_retries.append(session.session_id)
            if self._closing:
                return
        for session_id in immediate_retries:
            LOGGER.info("scheduling recovery for session %s", session_id)
            self._schedule_retry(session_id, immediate=True)

    def _schedule_retry(self, session_id: str, *, immediate: bool = False) -> None:
        with self._lock:
            if self._closing:
                return
            existing = self._retry_timers.get(session_id)
            if existing is not None and existing.is_alive():
                return
            session = self.store.snapshot(session_id)
            if session is None or session.state != "error" or not session.url:
                return
            if immediate:
                delay = min(1.0, self.retry_initial_seconds)
            else:
                exponent = max(0, session.retry_count - 1)
                delay = min(
                    self.retry_initial_seconds * (2 ** min(exponent, 8)),
                    self.retry_max_seconds,
                )
            timer = threading.Timer(delay, self._retry_fired, args=(session_id,))
            timer.daemon = True
            self._retry_timers[session_id] = timer
            timer.start()

    def _retry_fired(self, session_id: str) -> None:
        with self._lock:
            self._retry_timers.pop(session_id, None)
            if self._closing:
                return
            session = self.store.snapshot(session_id)
            if session is None or session.state != "error" or not session.url:
                return
            self.store.update(session_id, state="saving", error=None)
            self._save_dirty.add(session_id)
            self._schedule_save_locked(session_id)

    def _cancel_retry_locked(self, session_id: str) -> None:
        timer = self._retry_timers.pop(session_id, None)
        if timer is not None:
            timer.cancel()

    # -------------------------------------------------------------- connections

    def bind(self, connection_id: str, session_id: str, tab_id: int) -> list[dict[str, Any]]:
        if not isinstance(connection_id, str) or not connection_id:
            raise BackendError("invalid native connection")
        if not isinstance(tab_id, int) or isinstance(tab_id, bool) or tab_id < 0:
            raise BackendError("tab_id must be a non-negative integer")
        with self._lock:
            self._ensure_not_closing()
            session = self.store.snapshot(session_id)
            if session is None:
                raise BackendError(f"unknown session: {session_id}")
            if session.state == "saved":
                raise BackendError("this session has already finished")
            self._cancel_disconnect_locked(session_id)
            self._cancel_retry_locked(session_id)
            changes: dict[str, Any] = {
                "tab_id": tab_id,
                "bound_connection": connection_id,
            }
            if session.state in {"error", "opening"} and session.url:
                changes.update(state="editing", error=None)
            if (
                session.finish_requested
                and session.state in {"error", "editing", "conflict"}
            ):
                changes["finish_requested"] = False
            if (
                session.bound_connection
                and session.bound_connection != connection_id
            ):
                self._connections.get(session.bound_connection, set()).discard(session_id)
            self._connections.setdefault(connection_id, set()).add(session_id)
            self.store.update(session_id, **changes)
        return self.store.public_sessions()

    def connection_closed(self, connection_id: str) -> None:
        with self._lock:
            session_ids = self._connections.pop(connection_id, set())
            if self._closing:
                return
            for session_id in list(session_ids):
                session = self.store.snapshot(session_id)
                if session is None or session.state == "saved":
                    continue
                if session.bound_connection != connection_id:
                    continue
                self._cancel_disconnect_locked(session_id)
                if self.save_grace_seconds <= 0:
                    self._disconnect_finalize(session_id, connection_id)
                    continue
                timer = threading.Timer(
                    self.save_grace_seconds,
                    self._disconnect_finalize,
                    args=(session_id, connection_id),
                )
                timer.daemon = True
                self._disconnect_timers[session_id] = timer
                timer.start()

    def _disconnect_finalize(self, session_id: str, connection_id: str) -> None:
        with self._lock:
            self._disconnect_timers.pop(session_id, None)
            if self._closing:
                return
            session = self.store.snapshot(session_id)
            if session is None or session.state == "saved":
                return
            if session.bound_connection != connection_id:
                return
            self._detach_locked(session, connection_id)
            self.store.update(
                session_id,
                tab_id=None,
                finish_requested=True,
            )
            try:
                self.request_save(session_id, finish=True)
            except BackendError as error:
                LOGGER.warning(
                    "could not queue final save for %s: %s", session_id, error
                )

    def _detach_locked(self, session: Session, connection_id: Optional[str]) -> None:
        old = session.bound_connection
        if old:
            self._connections.get(old, set()).discard(session.session_id)
        self.store.update(
            session.session_id,
            bound_connection=None,
            tab_id=None,
        )

    def _cancel_disconnect_locked(self, session_id: str) -> None:
        timer = self._disconnect_timers.pop(session_id, None)
        if timer is not None:
            timer.cancel()

    def _remove_from_connection_locked(
        self, connection_id: Optional[str], session_id: str
    ) -> None:
        if connection_id:
            self._connections.get(connection_id, set()).discard(session_id)

    # ----------------------------------------------------------------- shutdown

    def shutdown(self) -> None:
        with self._lock:
            self._closing = True
            if self._cleanup_timer is not None:
                self._cleanup_timer.cancel()
            timers = list(self._retry_timers.values()) + list(
                self._disconnect_timers.values()
            )
            self._retry_timers.clear()
            self._disconnect_timers.clear()
        for timer in timers:
            timer.cancel()
        self._worker.shutdown()

    # ------------------------------------------------------------------- helpers

    def _ensure_not_closing(self) -> None:
        if self._closing:
            raise BackendError("backend is shutting down")

    def _name(self, session_id: str) -> str:
        session = self.store.snapshot(session_id)
        return session.name if session is not None else "document"

    def _notify(self, title: str, body: str, urgency: str) -> None:
        try:
            self.notifier(title, body, urgency=urgency)
        except Exception:
            LOGGER.debug("desktop notification failed", exc_info=True)


def _safe_error(error: Exception) -> str:
    if isinstance(error, BackendError):
        return redact_secrets(str(error), limit=500)
    # Do not log the raw exception message: unexpected failures can contain
    # command output or URLs with credentials.
    LOGGER.debug("unexpected backend error type %s", type(error).__name__)
    return "unexpected backend error"


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path = path.resolve(strict=False)
        directory = directory.resolve(strict=False)
    except OSError:
        return False
    try:
        return os.path.commonpath([str(path), str(directory)]) == str(directory)
    except ValueError:
        return False


# --------------------------------------------------------------- cleanup helpers


def _cleanup_eligible(session: Session, cutoff: datetime) -> bool:
    """Select completed, unbound, canonical sessions eligible for cleanup.

    A recorded cloud deletion is itself proof that the seven-day cloud rule was
    met, so legacy/partially-cleaned records qualify without re-checking the
    save date.  Everything else needs an old, timezone-aware final save.
    """

    if (
        session.state != "saved"
        or session.bound_connection
        or session.tab_id is not None
    ):
        return False
    try:
        canonical = str(uuid.UUID(session.session_id))
    except (ValueError, TypeError, AttributeError):
        return False
    if session.session_id != canonical:
        return False
    if (
        session.remote_dir != f"{REMOTE_ROOT}/{canonical}"
        and not session.cloud_deleted_at
    ):
        return False
    if session.cloud_deleted_at:
        # The cloud step is already confirmed; a legacy record no longer
        # needs a usable remote path to finish local cleanup.
        return True
    if not session.last_saved_at:
        return False
    try:
        saved_at = datetime.fromisoformat(
            session.last_saved_at.replace("Z", "+00:00")
        )
    except (ValueError, TypeError, AttributeError):
        return False
    if saved_at.tzinfo is None:
        return False
    return saved_at < cutoff


def _protected_document_paths(sessions: Iterable[Session]) -> list[Path]:
    """All user-facing originals/results that cleanup must never touch."""

    paths: list[Path] = []
    seen: set[str] = set()
    for session in sessions:
        for value in (session.original_path, session.result_path):
            if not isinstance(value, str) or not value:
                continue
            try:
                path = Path(value)
            except (TypeError, ValueError):
                continue
            key = str(path)
            if key not in seen:
                seen.add(key)
                paths.append(path)
    return paths


def _path_variants(path: Path) -> set[Path]:
    """Lexical and symlink-resolved forms used for defensive comparisons."""

    variants = {Path(os.path.abspath(path))}
    try:
        variants.add(Path(os.path.realpath(path)))
    except (OSError, ValueError):
        pass
    return variants


def _contains_path(directory: Path, path: Path) -> bool:
    try:
        directory_text = os.path.abspath(directory)
        path_text = os.path.abspath(path)
        return (
            os.path.commonpath([directory_text, path_text]) == directory_text
        )
    except (OSError, ValueError):
        return False


def _path_within(path: Path, directory: Path) -> bool:
    for candidate in _path_variants(path):
        for root in _path_variants(directory):
            if _contains_path(root, candidate):
                return True
    return False


def _is_app_session_target(parts: tuple[str, ...]) -> bool:
    """Only ``drive/OfficeOnline/<uuid>`` and ``backups/<uuid>`` are allowed."""

    if len(parts) == 3 and parts[0] == "drive" and parts[1] == REMOTE_ROOT:
        candidate = parts[2]
    elif len(parts) == 2 and parts[0] == "backups":
        candidate = parts[1]
    else:
        return False
    try:
        return str(uuid.UUID(candidate)) == candidate
    except (ValueError, TypeError, AttributeError):
        return False


def _protected_conflict(target: Path, protected: list[Path]) -> Optional[Path]:
    target_variants = _path_variants(target)
    for item in protected:
        for candidate in _path_variants(item):
            for root in target_variants:
                if _contains_path(root, candidate):
                    return item
    return None


def _cleanup_target_problem(
    target: Path, data_dir: Path, protected: list[Path]
) -> Optional[str]:
    """Validate one fixed UUID directory without modifying anything."""

    data_abs = Path(os.path.abspath(data_dir))
    target_abs = Path(os.path.abspath(target))
    try:
        relative = target_abs.relative_to(data_abs)
    except ValueError:
        return "cleanup target escapes the private app data directory"
    if not relative.parts:
        return "refusing to remove the private app data directory"
    if any(part in ("", ".", "..") for part in relative.parts):
        return "cleanup target contains an unsafe path component"
    if not _is_app_session_target(relative.parts):
        return "refusing to remove a non-session cleanup target"
    conflict = _protected_conflict(target_abs, protected)
    if conflict is not None:
        return (
            f"cleanup target contains protected original or result document "
            f"{conflict}; refusing to remove it"
        )

    current = data_abs
    try:
        info = current.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        return f"could not inspect the private data directory: {error.strerror or error}"
    if stat.S_ISLNK(info.st_mode):
        return "the private data directory is a symbolic link; refusing cleanup"
    if not stat.S_ISDIR(info.st_mode):
        return "the private data directory is not a directory; refusing cleanup"

    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            # Missing local copies are fine and need no deletion.
            return None
        except OSError as error:
            return f"could not inspect cleanup path: {error.strerror or error}"
        if stat.S_ISLNK(info.st_mode):
            return f"cleanup path is a symbolic link: {current}"
        if current == target_abs:
            if not stat.S_ISDIR(info.st_mode):
                return f"cleanup target is not a directory: {current}"
        elif not stat.S_ISDIR(info.st_mode):
            return f"cleanup path ancestor is not a directory: {current}"
    return None


def _remove_cleanup_directory(target: Path, data_dir: Path) -> None:
    """Delete one app-owned directory without following any symlink.

    The path is walked from the private data root with ``O_NOFOLLOW`` directory
    descriptors.  Entries are unlinked/rmdir'ed relative to their parent file
    descriptor, so a symlinked target or ancestor is refused and a symlink
    inside the tree is removed as a link, never followed.
    """

    data_abs = Path(os.path.abspath(data_dir))
    target_abs = Path(os.path.abspath(target))
    try:
        parts = target_abs.relative_to(data_abs).parts
    except ValueError as error:
        raise BackendError("cleanup target escapes the private app data directory") from error
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise BackendError("refusing to remove an unsafe cleanup target")
    if not _is_app_session_target(parts):
        raise BackendError("refusing to remove a non-session cleanup target")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(data_abs, flags | nofollow)
    except FileNotFoundError:
        return
    except OSError as error:
        raise BackendError(
            f"could not open the private data directory for cleanup: "
            f"{error.strerror or error}"
        ) from error

    try:
        for part in parts[:-1]:
            try:
                child_fd = os.open(part, flags | nofollow, dir_fd=directory_fd)
            except FileNotFoundError:
                return
            except OSError as error:
                raise BackendError(
                    f"could not safely open cleanup path: {error.strerror or error}"
                ) from error
            os.close(directory_fd)
            directory_fd = child_fd

        name = parts[-1]
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as error:
            raise BackendError(
                f"could not inspect cleanup target: {error.strerror or error}"
            ) from error
        if not stat.S_ISDIR(info.st_mode):
            raise BackendError(
                f"refusing to remove a non-directory cleanup target: {target_abs}"
            )
        try:
            target_fd = os.open(name, flags | nofollow, dir_fd=directory_fd)
        except OSError as error:
            raise BackendError(
                f"could not safely open cleanup target: {error.strerror or error}"
            ) from error
        try:
            _remove_cleanup_contents(target_fd, target_abs)
        finally:
            os.close(target_fd)
        try:
            os.rmdir(name, dir_fd=directory_fd)
        except OSError as error:
            raise BackendError(
                f"could not remove cleanup target: {error.strerror or error}"
            ) from error
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
    finally:
        os.close(directory_fd)


def _remove_cleanup_contents(directory_fd: int, display: Path) -> None:
    try:
        with os.scandir(directory_fd) as entries:
            names = [entry.name for entry in entries]
    except OSError as error:
        raise BackendError(
            f"could not list cleanup directory {display}: {error.strerror or error}"
        ) from error

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for name in names:
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise BackendError(
                f"could not inspect cleanup entry: {error.strerror or error}"
            ) from error
        if stat.S_ISDIR(info.st_mode):
            try:
                child_fd = os.open(
                    name, flags | nofollow, dir_fd=directory_fd
                )
            except FileNotFoundError:
                continue
            except OSError as error:
                raise BackendError(
                    f"could not safely open cleanup subdirectory: "
                    f"{error.strerror or error}"
                ) from error
            try:
                _remove_cleanup_contents(child_fd, display / name)
            finally:
                os.close(child_fd)
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise BackendError(
                    f"could not remove cleanup subdirectory: "
                    f"{error.strerror or error}"
                ) from error
        else:
            # Regular files, devices and symlinks are unlinked, never followed.
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise BackendError(
                    f"could not remove cleanup file: {error.strerror or error}"
                ) from error
