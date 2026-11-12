from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from office_online_backend import backend as backend_module
from office_online_backend.backend import BackendService
from office_online_backend.cli import main
from office_online_backend.config import AppConfig, REMOTE_ROOT, Paths
from office_online_backend.errors import BackendError, OneDriveError
from office_online_backend.onedrive import OneDriveService
from office_online_backend.sessions import Session, SessionStore

EXTENSION_ID = "a" * 32
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
CUTOFF = (NOW - timedelta(days=7)).isoformat()
OLD = (NOW - timedelta(days=7, minutes=1)).isoformat()
RECENT = (NOW - timedelta(days=6, hours=23)).isoformat()
_UNSET = object()


class FakeCleanupOneDrive:
    """Records cleanup commands and injects failures without touching OneDrive."""

    def __init__(self) -> None:
        self.removed: list[tuple[str, str]] = []
        self.failures: list[Exception] = []

    def remove_session_directory(self, session_id: str, remote_dir: str) -> None:
        self.removed.append((session_id, remote_dir))
        if self.failures:
            raise self.failures.pop(0)


class FakeLauncher:
    def validate(self):
        return ["/usr/bin/google-chrome"]

    def launch(self, extension_id, session_id):
        return None


class BackendCleanupTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="office-online-cleanup-")
        self.root = Path(self._temporary.name)
        self.paths = Paths(
            config_home=self.root / "config",
            data_home=self.root / "data",
            runtime_home=self.root / "run",
        )
        self.paths.ensure_base_dirs()
        self.config = AppConfig(
            extension_id=EXTENSION_ID,
            browser="google-chrome",
            onedrive_binary="onedrive",
        )
        self.store = SessionStore(self.paths)
        self.onedrive = FakeCleanupOneDrive()
        self.backend = BackendService(
            self.paths,
            self.config,
            self.store,
            self.onedrive,
            launcher=FakeLauncher(),
            notifier=lambda *args, **kwargs: None,
        )
        self.original = self.root / "Report.docx"
        self.original.write_bytes(b"original local bytes")
        self.original_mtime = self.original.stat().st_mtime_ns

    def tearDown(self):
        try:
            self.backend.shutdown()
        finally:
            self._temporary.cleanup()

    def working_dir(self, session_id: str) -> Path:
        return self.paths.drive_dir / REMOTE_ROOT / session_id

    def backup_dir(self, session_id: str) -> Path:
        return self.paths.backups_dir / session_id

    def make_session(
        self,
        *,
        session_id: str | None = None,
        state: str = "saved",
        saved_at: str | None = OLD,
        remote_dir=_UNSET,
        created_at: str = "2026-10-01T00:00:00+00:00",
        tab_id: int | None = None,
        bound_connection: str | None = None,
        cloud_deleted_at: str | None = None,
        original_path: str | None = None,
        result_path: str | None = None,
        working_path=_UNSET,
        backup_path: str | None = None,
        url: str | None = "https://tenant.sharepoint.com/personal/test/Report.docx",
    ) -> Session:
        session_id = str(uuid.uuid4()) if session_id is None else session_id
        if remote_dir is _UNSET:
            remote_dir = f"{REMOTE_ROOT}/{session_id}"
        if working_path is _UNSET:
            working_path = str(self.working_dir(session_id) / "Report.docx")
        if original_path is None:
            original_path = str(self.original)
        if result_path is None:
            result_path = original_path
        return Session(
            session_id=session_id,
            name="Report.docx",
            original_path=original_path,
            url=url,
            state=state,
            tab_id=tab_id,
            error=None,
            last_saved_at=saved_at,
            result_path=result_path,
            created_at=created_at,
            working_path=working_path,
            remote_dir=remote_dir,
            remote_relpath=f"{remote_dir}/Report.docx" if remote_dir else None,
            local_hash="a" * 64,
            backup_path=backup_path,
            cloud_deleted_at=cloud_deleted_at,
            bound_connection=bound_connection,
        )

    def add(self, session: Session) -> Session:
        self.store.add(session)
        return session

    def create_local_copies(self, session: Session, content: bytes = b"session bytes"):
        working = self.working_dir(session.session_id) / "Report.docx"
        working.parent.mkdir(parents=True, exist_ok=True)
        working.write_bytes(content)
        backup_dir = self.backup_dir(session.session_id)
        backup_dir.mkdir(parents=True)
        backup = backup_dir / "20261001-000000-Report.docx"
        backup.write_bytes(b"backup " + content)
        self.store.update(
            session.session_id,
            working_path=str(working),
            backup_path=str(backup),
        )
        return working, backup

    def cleanup(self, *, dry_run: bool = False):
        return self.backend.cleanup_expired(dry_run=dry_run, now=NOW)

    def test_cleanup_request_after_shutdown_fails_immediately(self):
        self.backend.shutdown()
        with self.assertRaises(BackendError):
            self.backend.request_cleanup()

    def test_only_old_saved_unbound_canonical_sessions_are_removed(self):
        eligible_old = self.add(self.make_session())
        eligible_zulu = self.add(self.make_session(
            saved_at=(NOW - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ))
        eligible_old_creation = self.add(self.make_session(
            created_at="2026-09-01T00:00:00+00:00"
        ))
        expected = {
            eligible_old.session_id,
            eligible_zulu.session_id,
            eligible_old_creation.session_id,
        }
        for session in (eligible_old, eligible_zulu, eligible_old_creation):
            self.create_local_copies(session)

        ineligible = [
            self.add(self.make_session(state="editing")),
            self.add(self.make_session(state="saving")),
            self.add(self.make_session(state="opening")),
            self.add(self.make_session(state="error")),
            self.add(self.make_session(state="conflict")),
            self.add(self.make_session(saved_at=RECENT)),
            self.add(self.make_session(saved_at=CUTOFF)),
            self.add(self.make_session(saved_at="not-a-timestamp")),
            self.add(self.make_session(saved_at="2026-11-01T00:00:00")),
            self.add(self.make_session(saved_at=None)),
            self.add(self.make_session(bound_connection="live-connection")),
            self.add(self.make_session(tab_id=17)),
        ]

        mismatched_id = str(uuid.uuid4())
        ineligible.append(self.add(self.make_session(
            session_id=mismatched_id,
            remote_dir=f"{REMOTE_ROOT}/{uuid.uuid4()}",
        )))
        root_escape_id = str(uuid.uuid4())
        ineligible.append(self.add(self.make_session(
            session_id=root_escape_id,
            remote_dir=REMOTE_ROOT,
        )))
        traversal_id = str(uuid.uuid4())
        ineligible.append(self.add(self.make_session(
            session_id=traversal_id,
            remote_dir=f"{REMOTE_ROOT}/../{traversal_id}",
        )))
        ineligible.append(self.add(self.make_session(
            session_id="not-a-uuid",
            remote_dir=f"{REMOTE_ROOT}/not-a-uuid",
        )))

        uppercase = str(uuid.uuid4()).upper()
        ineligible.append(self.add(self.make_session(
            session_id=uppercase,
            remote_dir=f"{REMOTE_ROOT}/{uppercase}",
        )))

        results = self.cleanup()

        self.assertEqual({result["session_id"] for result in results}, expected)
        self.assertEqual({result["action"] for result in results}, {"deleted"})
        self.assertEqual({session_id for session_id, _ in self.onedrive.removed}, expected)
        self.assertEqual(len(self.onedrive.removed), len(expected))

        for session_id in expected:
            self.assertIsNone(self.store.snapshot(session_id))
            self.assertFalse(self.working_dir(session_id).exists())
            self.assertFalse(self.backup_dir(session_id).exists())

        for session in ineligible:
            current = self.store.snapshot(session.session_id)
            self.assertIsNotNone(current)
            self.assertEqual(current.cloud_deleted_at, session.cloud_deleted_at)
            self.assertEqual(current.url, session.url)

    def test_age_is_measured_from_the_final_save_not_session_creation(self):
        old_save_recent_open = self.add(self.make_session(
            created_at="2026-11-12T11:00:00+00:00",
            saved_at=OLD,
        ))
        recent_save_old_open = self.add(self.make_session(
            created_at="2026-09-01T00:00:00+00:00",
            saved_at=RECENT,
        ))

        results = self.cleanup()

        self.assertEqual(
            [result["session_id"] for result in results],
            [old_save_recent_open.session_id],
        )
        self.assertEqual(
            [session_id for session_id, _ in self.onedrive.removed],
            [old_save_recent_open.session_id],
        )
        self.assertIsNone(self.store.snapshot(old_save_recent_open.session_id))
        self.assertIsNone(
            self.store.snapshot(recent_save_old_open.session_id).cloud_deleted_at
        )

    def test_dry_run_reports_but_never_calls_onedrive_or_changes_files(self):
        session = self.add(self.make_session())
        working, backup = self.create_local_copies(session)

        results = self.cleanup(dry_run=True)

        self.assertEqual(results, [{
            "session_id": session.session_id,
            "name": session.name,
            "action": "would_delete",
        }])
        self.assertEqual(self.onedrive.removed, [])
        current = self.store.snapshot(session.session_id)
        self.assertIsNone(current.cloud_deleted_at)
        self.assertEqual(current.url, session.url)
        self.assertEqual(working.read_bytes(), b"session bytes")
        self.assertTrue(backup.exists())

        results = self.cleanup()

        self.assertEqual([result["action"] for result in results], ["deleted"])
        self.assertEqual(self.onedrive.removed, [(
            session.session_id, session.remote_dir
        )])
        self.assertFalse(self.working_dir(session.session_id).exists())
        self.assertFalse(self.backup_dir(session.session_id).exists())
        self.assertIsNone(self.store.snapshot(session.session_id))

    def test_failed_cloud_delete_preserves_all_local_copies_and_retries(self):
        session = self.add(self.make_session())
        working, backup = self.create_local_copies(session)
        self.onedrive.failures.append(OneDriveError("simulated cleanup failure"))

        results = self.cleanup()

        self.assertEqual(results[0]["action"], "error")
        self.assertIn("simulated cleanup failure", results[0]["error"])
        current = self.store.snapshot(session.session_id)
        self.assertIsNone(current.cloud_deleted_at)
        self.assertEqual(current.url, session.url)
        self.assertEqual(current.state, "saved")
        self.assertEqual(current.result_path, session.result_path)
        self.assertEqual(working.read_bytes(), b"session bytes")
        self.assertTrue(backup.exists())

        results = self.cleanup()

        self.assertEqual([result["action"] for result in results], ["deleted"])
        self.assertEqual(len(self.onedrive.removed), 2)
        self.assertIsNone(self.store.snapshot(session.session_id))
        self.assertFalse(self.working_dir(session.session_id).exists())
        self.assertFalse(self.backup_dir(session.session_id).exists())

    def test_unexpected_delete_error_is_redacted_and_remains_eligible(self):
        session = self.add(self.make_session())
        self.onedrive.failures.append(RuntimeError("token=supersecret"))

        results = self.cleanup()

        self.assertEqual(results[0]["action"], "error")
        self.assertEqual(results[0]["error"], "unexpected backend error")
        self.assertNotIn("supersecret", json.dumps(results))
        self.assertIsNone(
            self.store.snapshot(session.session_id).cloud_deleted_at
        )

        results = self.cleanup()
        self.assertEqual([result["action"] for result in results], ["deleted"])

    def test_success_removes_working_backup_and_history_from_main_and_backup(self):
        session = self.add(self.make_session())
        working, backup = self.create_local_copies(session)
        original_before = (self.original.read_bytes(), self.original.stat().st_mtime_ns)

        first = self.cleanup()

        self.assertEqual([result["action"] for result in first], ["deleted"])
        self.assertIsNone(self.store.snapshot(session.session_id))
        self.assertFalse(working.exists())
        self.assertFalse(backup.exists())
        self.assertFalse(self.working_dir(session.session_id).exists())
        self.assertFalse(self.backup_dir(session.session_id).exists())
        self.assertEqual(
            (self.original.read_bytes(), self.original.stat().st_mtime_ns),
            original_before,
        )
        for path in (
            self.paths.sessions_file,
            self.paths.sessions_file.with_name("sessions.json.bak"),
        ):
            self.assertNotIn(session.session_id, path.read_text(encoding="utf-8"))
        self.assertFalse(
            self.paths.sessions_file.with_name("sessions.json.bak.bak").exists()
        )
        reloaded = SessionStore(self.paths)
        self.assertIsNone(reloaded.snapshot(session.session_id))

        second = self.cleanup()
        self.assertEqual(second, [])
        self.assertEqual(len(self.onedrive.removed), 1)

    def test_legacy_cloud_marker_cleans_local_data_without_repeating_cloud(self):
        session = self.add(self.make_session(
            saved_at=RECENT,
            cloud_deleted_at="2026-11-01T00:00:00+00:00",
            url=None,
        ))
        working, backup = self.create_local_copies(session)

        results = self.cleanup()

        self.assertEqual(results, [{
            "session_id": session.session_id,
            "name": session.name,
            "action": "deleted",
        }])
        self.assertEqual(self.onedrive.removed, [])
        self.assertFalse(working.exists())
        self.assertFalse(backup.exists())
        self.assertIsNone(self.store.snapshot(session.session_id))

    def test_local_failure_keeps_marker_and_retries_without_cloud_delete(self):
        session = self.add(self.make_session())
        working, backup = self.create_local_copies(session)

        with mock.patch(
            "office_online_backend.backend._remove_cleanup_directory",
            side_effect=BackendError("simulated local cleanup failure"),
        ):
            first = self.cleanup()

        self.assertEqual(first[0]["action"], "error")
        self.assertIn("simulated local cleanup failure", first[0]["error"])
        marked = self.store.snapshot(session.session_id)
        self.assertIsNotNone(marked.cloud_deleted_at)
        self.assertEqual(marked.state, "saved")
        self.assertIsNone(marked.url)
        self.assertTrue(working.exists())
        self.assertTrue(backup.exists())

        second = self.cleanup()

        self.assertEqual([result["action"] for result in second], ["deleted"])
        self.assertEqual(len(self.onedrive.removed), 1)
        self.assertFalse(self.working_dir(session.session_id).exists())
        self.assertFalse(self.backup_dir(session.session_id).exists())
        self.assertIsNone(self.store.snapshot(session.session_id))

    def test_partial_local_failure_retries_only_the_remaining_target(self):
        session = self.add(self.make_session())
        working, backup = self.create_local_copies(session)
        real_remove = backend_module._remove_cleanup_directory
        calls = []

        def remove_with_second_failure(target, data_dir):
            calls.append(target)
            if len(calls) == 2:
                raise BackendError("simulated backup removal failure")
            return real_remove(target, data_dir)

        with mock.patch(
            "office_online_backend.backend._remove_cleanup_directory",
            side_effect=remove_with_second_failure,
        ):
            first = self.cleanup()

        self.assertEqual(first[0]["action"], "error")
        self.assertIn("simulated backup removal failure", first[0]["error"])
        self.assertFalse(working.exists())
        self.assertTrue(backup.exists())
        current = self.store.snapshot(session.session_id)
        self.assertIsNotNone(current.cloud_deleted_at)

        second = self.cleanup()

        self.assertEqual([result["action"] for result in second], ["deleted"])
        self.assertEqual(len(self.onedrive.removed), 1)
        self.assertFalse(backup.exists())
        self.assertIsNone(self.store.snapshot(session.session_id))

    def test_history_prune_failure_keeps_marker_and_retries_locally(self):
        session = self.add(self.make_session())
        working, backup = self.create_local_copies(session)

        with mock.patch.object(
            self.store,
            "prune",
            side_effect=BackendError("simulated prune failure"),
        ):
            first = self.cleanup()

        self.assertEqual(first[0]["action"], "error")
        self.assertIn("simulated prune failure", first[0]["error"])
        self.assertFalse(working.exists())
        self.assertFalse(backup.exists())
        current = self.store.snapshot(session.session_id)
        self.assertIsNotNone(current.cloud_deleted_at)

        second = self.cleanup()

        self.assertEqual([result["action"] for result in second], ["deleted"])
        self.assertEqual(len(self.onedrive.removed), 1)
        self.assertIsNone(self.store.snapshot(session.session_id))

    def test_recent_error_conflict_and_active_sessions_keep_all_local_data(self):
        sessions = [
            self.add(self.make_session(state="editing")),
            self.add(self.make_session(state="saving")),
            self.add(self.make_session(state="opening")),
            self.add(self.make_session(state="error")),
            self.add(self.make_session(state="conflict")),
            self.add(self.make_session(saved_at=RECENT)),
            self.add(self.make_session(bound_connection="live-connection")),
            self.add(self.make_session(tab_id=17)),
        ]
        copied = {
            session.session_id: self.create_local_copies(session)
            for session in sessions
        }

        results = self.cleanup()

        self.assertEqual(results, [])
        self.assertEqual(self.onedrive.removed, [])
        for session in sessions:
            current = self.store.snapshot(session.session_id)
            self.assertIsNotNone(current)
            self.assertEqual(current.state, session.state)
            self.assertEqual(current.cloud_deleted_at, session.cloud_deleted_at)
            working, backup = copied[session.session_id]
            self.assertTrue(working.exists())
            self.assertTrue(backup.exists())

    def test_original_and_result_outside_session_targets_are_retained(self):
        kept_original = self.paths.data_dir / "kept-original.docx"
        kept_original.write_bytes(b"user original under app data")
        session = self.add(self.make_session(
            original_path=str(kept_original),
            result_path=str(kept_original),
        ))
        self.create_local_copies(session)

        results = self.cleanup()

        self.assertEqual([result["action"] for result in results], ["deleted"])
        self.assertEqual(kept_original.read_bytes(), b"user original under app data")
        self.assertFalse(self.working_dir(session.session_id).exists())
        self.assertFalse(self.backup_dir(session.session_id).exists())
        self.assertIsNone(self.store.snapshot(session.session_id))

    def test_target_containing_original_document_is_never_deleted(self):
        target = self.working_dir(str(uuid.uuid4()))
        target.mkdir(parents=True)
        protected_original = target / "Report.docx"
        protected_original.write_bytes(b"original inside working target")
        session_id = target.name
        session = self.add(self.make_session(
            session_id=session_id,
            original_path=str(protected_original),
            result_path=str(protected_original),
        ))

        results = self.cleanup()

        self.assertEqual(results[0]["action"], "error")
        self.assertIn("protected", results[0]["error"].lower())
        self.assertEqual(self.onedrive.removed, [])
        self.assertEqual(
            protected_original.read_bytes(), b"original inside working target"
        )
        current = self.store.snapshot(session.session_id)
        self.assertIsNotNone(current)
        self.assertEqual(current.state, "saved")
        self.assertIsNone(current.cloud_deleted_at)

    def test_target_containing_result_document_is_never_deleted(self):
        session = self.add(self.make_session())
        target = self.working_dir(session.session_id)
        target.mkdir(parents=True)
        protected_result = target / "Report (conflict).docx"
        protected_result.write_bytes(b"result inside working target")
        self.store.update(session.session_id, result_path=str(protected_result))

        results = self.cleanup()

        self.assertEqual(results[0]["action"], "error")
        self.assertIn("protected", results[0]["error"].lower())
        self.assertEqual(self.onedrive.removed, [])
        self.assertEqual(
            protected_result.read_bytes(), b"result inside working target"
        )
        self.assertIsNotNone(self.store.snapshot(session.session_id))

    def test_document_of_an_earlier_pruned_session_still_protects_later_target(self):
        first = self.add(self.make_session(
            created_at="2026-09-01T00:00:00+00:00"
        ))
        second = self.add(self.make_session(
            created_at="2026-10-01T00:00:00+00:00"
        ))
        second_target = self.working_dir(second.session_id)
        second_target.mkdir(parents=True)
        protected_original = second_target / "First original.docx"
        protected_original.write_bytes(b"must survive")
        self.store.update(
            first.session_id,
            original_path=str(protected_original),
            result_path=str(protected_original),
        )
        self.store.update(second.session_id, working_path=str(second_target / "Report.docx"))

        results = self.cleanup()

        self.assertEqual(
            [(item["session_id"], item["action"]) for item in results],
            [(first.session_id, "deleted"), (second.session_id, "error")],
        )
        self.assertIsNone(self.store.snapshot(first.session_id))
        self.assertIsNotNone(self.store.snapshot(second.session_id))
        self.assertEqual(protected_original.read_bytes(), b"must survive")
        self.assertEqual(
            [session_id for session_id, _ in self.onedrive.removed],
            [first.session_id],
        )

    def test_symlinked_target_is_refused_and_never_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        victim = outside / "victim.txt"
        victim.write_bytes(b"do not delete")
        session = self.add(self.make_session())
        target = self.working_dir(session.session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(outside, target_is_directory=True)

        results = self.cleanup()

        self.assertEqual(results[0]["action"], "error")
        self.assertIn("symbolic link", results[0]["error"])
        self.assertEqual(self.onedrive.removed, [])
        self.assertEqual(victim.read_bytes(), b"do not delete")
        self.assertIsNotNone(self.store.snapshot(session.session_id))

    def test_symlinked_ancestor_is_refused_and_never_followed(self):
        outside = self.root / "outside-ancestor"
        outside.mkdir()
        session = self.add(self.make_session())
        victim_dir = outside / session.session_id
        victim_dir.mkdir()
        victim = victim_dir / "victim.txt"
        victim.write_bytes(b"do not delete")
        remote_parent = self.paths.drive_dir / REMOTE_ROOT
        remote_parent.parent.mkdir(parents=True, exist_ok=True)
        remote_parent.symlink_to(outside, target_is_directory=True)

        results = self.cleanup()

        self.assertEqual(results[0]["action"], "error")
        self.assertIn("symbolic link", results[0]["error"])
        self.assertEqual(self.onedrive.removed, [])
        self.assertEqual(victim.read_bytes(), b"do not delete")
        self.assertIsNotNone(self.store.snapshot(session.session_id))

    def test_symlink_inside_target_is_unlinked_without_following_it(self):
        outside = self.root / "outside-link"
        outside.mkdir()
        victim = outside / "victim.txt"
        victim.write_bytes(b"do not delete")
        session = self.add(self.make_session())
        working, _ = self.create_local_copies(session)
        link = working.parent / "escape-link"
        link.symlink_to(outside, target_is_directory=True)

        results = self.cleanup()

        self.assertEqual([result["action"] for result in results], ["deleted"])
        self.assertFalse(link.exists())
        self.assertFalse(link.is_symlink())
        self.assertEqual(victim.read_bytes(), b"do not delete")
        self.assertFalse(self.working_dir(session.session_id).exists())

    def test_stored_working_or_backup_path_outside_uuid_directory_fails_safely(self):
        cases = []
        working_escape = self.add(self.make_session(
            working_path=str(self.paths.drive_dir / "working.docx")
        ))
        cases.append(working_escape)
        backup_escape = self.add(self.make_session(
            backup_path=str(self.paths.data_dir / "stray-backup.docx")
        ))
        cases.append(backup_escape)

        stray = self.paths.drive_dir / "working.docx"
        stray.write_bytes(b"stray working copy")
        sentinels = []
        for session in cases:
            target = self.working_dir(session.session_id)
            target.mkdir(parents=True)
            sentinel = target / "sentinel.txt"
            sentinel.write_bytes(b"keep")
            sentinels.append(sentinel)

        results = self.cleanup()

        self.assertEqual(
            {item["session_id"] for item in results},
            {session.session_id for session in cases},
        )
        self.assertEqual({item["action"] for item in results}, {"error"})
        for result in results:
            self.assertIn(
                "outside the expected session directory", result["error"]
            )
        self.assertEqual(self.onedrive.removed, [])
        for sentinel in sentinels:
            self.assertTrue(sentinel.exists())
        self.assertEqual(stray.read_bytes(), b"stray working copy")
        for session in cases:
            self.assertIsNotNone(self.store.snapshot(session.session_id))

    def test_cleanup_timer_starts_after_thirty_seconds_then_six_hours(self):
        created = []

        class RecordingTimer:
            def __init__(self, interval, function, args=(), kwargs=None):
                self.interval = interval
                self.function = function
                self.args = args
                self.kwargs = kwargs or {}
                self.daemon = False
                self.started = False
                self.cancelled = False
                created.append(self)

            def start(self):
                self.started = True

            def cancel(self):
                self.cancelled = True

            def is_alive(self):
                return self.started and not self.cancelled

        with mock.patch("office_online_backend.backend.threading.Timer", RecordingTimer):
            self.backend.start_cleanup()
            self.assertEqual(len(created), 1)
            first = created[0]
            self.assertEqual(first.interval, 30.0)
            self.assertTrue(first.started)
            self.assertTrue(first.daemon)

            first.function(*first.args, **first.kwargs)

        self.assertEqual(len(created), 2)
        self.assertEqual(created[1].interval, 6 * 60 * 60)
        self.assertTrue(created[1].started)
        self.assertTrue(created[1].daemon)


class SessionStorePruneTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="office-online-prune-")
        self.root = Path(self._temporary.name)
        self.paths = Paths(
            config_home=self.root / "config",
            data_home=self.root / "data",
            runtime_home=self.root / "run",
        )
        self.paths.ensure_base_dirs()
        self.store = SessionStore(self.paths)

    def tearDown(self):
        self._temporary.cleanup()

    def make_session(self, state="saved", marker=None):
        session_id = str(uuid.uuid4())
        return Session(
            session_id=session_id,
            name="Report.docx",
            original_path=str(self.root / "Report.docx"),
            url="https://tenant.sharepoint.com/x" if state != "saved" else None,
            state=state,
            tab_id=None,
            error=None,
            last_saved_at=OLD,
            result_path=str(self.root / "Report.docx"),
            working_path=str(
                self.paths.drive_dir / REMOTE_ROOT / session_id / "Report.docx"
            ),
            remote_dir=f"{REMOTE_ROOT}/{session_id}",
            remote_relpath=f"{REMOTE_ROOT}/{session_id}/Report.docx",
            cloud_deleted_at=marker,
        )

    def test_prune_requires_saved_and_cloud_deleted_and_cleans_fallback(self):
        marked = self.make_session(marker="2026-11-01T00:00:00+00:00")
        unmarked = self.make_session()
        editing_marked = self.make_session(
            state="editing", marker="2026-11-01T00:00:00+00:00"
        )
        for session in (marked, unmarked, editing_marked):
            self.store.add(session)

        pruned = self.store.prune([
            marked.session_id,
            unmarked.session_id,
            editing_marked.session_id,
            "missing-session",
        ])

        self.assertEqual(pruned, [marked.session_id])
        self.assertIsNone(self.store.snapshot(marked.session_id))
        self.assertIsNotNone(self.store.snapshot(unmarked.session_id))
        self.assertIsNotNone(self.store.snapshot(editing_marked.session_id))
        for path in (
            self.paths.sessions_file,
            self.paths.sessions_file.with_name("sessions.json.bak"),
        ):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(marked.session_id, text)
            self.assertIn(unmarked.session_id, text)
            self.assertIn(editing_marked.session_id, text)
        self.assertFalse(
            self.paths.sessions_file.with_name("sessions.json.bak.bak").exists()
        )
        reloaded = SessionStore(self.paths)
        self.assertIsNone(reloaded.snapshot(marked.session_id))
        self.assertIsNotNone(reloaded.snapshot(unmarked.session_id))

        # The fallback itself must be sanitized, not just the main file.
        self.paths.sessions_file.write_text("{broken json", encoding="utf-8")
        recovered = SessionStore(self.paths)
        self.assertTrue(recovered.recovered_from_backup)
        self.assertIsNone(recovered.snapshot(marked.session_id))
        self.assertIsNotNone(recovered.snapshot(unmarked.session_id))

    def test_prune_restores_records_if_persistence_fails(self):
        marked = self.make_session(marker="2026-11-01T00:00:00+00:00")
        self.store.add(marked)

        with mock.patch.object(
            self.store, "_persist_locked", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.store.prune([marked.session_id])

        self.assertIsNotNone(self.store.snapshot(marked.session_id))


class CleanupCliTests(unittest.TestCase):
    def run_cleanup_cli(self, *arguments, response=None):
        response = response or {
            "id": "cli-cleanup",
            "ok": True,
            "cleanup": [{
                "session_id": str(uuid.uuid4()),
                "name": "Report.docx",
                "action": "would_delete",
            }],
            "sessions": [],
        }
        output = io.StringIO()
        with mock.patch(
            "office_online_backend.cli.socket_request", return_value=response
        ) as request, redirect_stdout(output):
            code = main(["cleanup", *arguments])
        return code, request, output.getvalue()

    def test_cleanup_dry_run_sends_boolean_and_returns_success(self):
        code, request, output = self.run_cleanup_cli("--dry-run")
        self.assertEqual(code, 0)
        paths, payload = request.call_args.args
        self.assertEqual(payload, {
            "id": "cli-cleanup",
            "action": "cleanup",
            "dry_run": True,
        })
        self.assertEqual(request.call_args.kwargs, {"timeout": 1800})
        self.assertIn("would_delete", output)
        self.assertEqual(paths.socket_path.name, "control.sock")

    def test_cleanup_without_dry_run_sends_false_and_delete_errors_fail(self):
        response = {
            "id": "cli-cleanup",
            "ok": True,
            "cleanup": [{
                "session_id": str(uuid.uuid4()),
                "name": "Report.docx",
                "action": "error",
                "error": "simulated cleanup failure",
            }],
            "sessions": [],
        }
        code, request, output = self.run_cleanup_cli(response=response)
        self.assertEqual(code, 1)
        _, payload = request.call_args.args
        self.assertFalse(payload["dry_run"])
        self.assertIn("simulated cleanup failure", output)


class FakeRunner:
    def __init__(self, callback):
        self.calls = []
        self.callback = callback

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        return self.callback(argv, kwargs)


class OneDriveCleanupTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="office-online-remove-")
        self.root = Path(self._temporary.name)
        self.paths = Paths(
            config_home=self.root / "config",
            data_home=self.root / "data",
            runtime_home=self.root / "run",
        )
        self.paths.ensure_base_dirs()
        self.binary = self.root / "fake-onedrive"
        self.binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(self.binary, 0o700)
        self.refresh = self.paths.onedrive_config_dir / "refresh_token"
        self.refresh.write_text("token", encoding="utf-8")
        os.chmod(self.refresh, 0o600)
        self.session_id = str(uuid.uuid4())
        self.remote_dir = f"{REMOTE_ROOT}/{self.session_id}"

    def tearDown(self):
        self._temporary.cleanup()

    def service(self, callback, *, link_timeout=14):
        runner = FakeRunner(callback)
        service = OneDriveService(
            str(self.binary),
            self.paths.onedrive_config_dir,
            self.paths.drive_dir,
            runner=runner,
            link_timeout=link_timeout,
        )
        return service, runner

    @staticmethod
    def completed(argv, *, code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)

    def test_remove_directory_uses_exact_documented_argv_without_sync(self):
        service, runner = self.service(
            lambda argv, kwargs: self.completed(
                argv,
                stdout=(
                    "The requested directory to delete online has been deleted\n"
                ),
            )
        )

        service.remove_session_directory(self.session_id, self.remote_dir)

        self.assertEqual(len(runner.calls), 1)
        argv, kwargs = runner.calls[0]
        self.assertEqual(argv, [
            str(self.binary),
            "--confdir",
            str(self.paths.onedrive_config_dir),
            "--syncdir",
            str(self.paths.drive_dir),
            "--remove-directory",
            self.remote_dir,
        ])
        for forbidden in (
            "--sync",
            "--upload-only",
            "--download-only",
            "--monitor",
            "--resync",
            "--cleanup-local-files",
            "--single-directory",
            "--get-file-link",
            "--create-share-link",
        ):
            self.assertNotIn(forbidden, argv)
        self.assertFalse(kwargs["shell"])
        self.assertFalse(kwargs["check"])
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")
        self.assertEqual(kwargs["timeout"], 14)

    def test_exit_zero_without_positive_acknowledgment_is_an_error(self):
        for stdout in (
            "",
            "some unrelated output\n",
            "The requested path to delete is not a directory - aborting deletion attempt\n",
        ):
            with self.subTest(stdout=stdout):
                service, runner = self.service(
                    lambda argv, kwargs: self.completed(argv, stdout=stdout)
                )
                with self.assertRaises(OneDriveError) as caught:
                    service.remove_session_directory(self.session_id, self.remote_dir)
                self.assertIn("did not confirm", str(caught.exception))
                self.assertEqual(len(runner.calls), 1)

    def test_missing_directory_acknowledgment_is_idempotent_success(self):
        message = (
            "The requested directory to delete was not found on OneDrive - "
            "skipping removing the remote directory online as it does not exist\n"
        )
        service, runner = self.service(
            lambda argv, kwargs: self.completed(argv, stdout=message)
        )

        service.remove_session_directory(self.session_id, self.remote_dir)

        self.assertEqual(len(runner.calls), 1)

    def test_client_error_output_or_nonzero_status_raises(self):
        cases = [
            (0, "ERROR: Microsoft OneDrive API returned an error\n", ""),
            (1, "The requested directory to delete online has been deleted\n", ""),
            (0, "", "FATAL: could not delete\n"),
        ]
        for code, stdout, stderr in cases:
            with self.subTest(code=code, stdout=stdout, stderr=stderr):
                service, runner = self.service(
                    lambda argv, kwargs: self.completed(
                        argv, code=code, stdout=stdout, stderr=stderr
                    )
                )
                with self.assertRaises(OneDriveError):
                    service.remove_session_directory(self.session_id, self.remote_dir)
                self.assertEqual(len(runner.calls), 1)

    def test_rejects_path_escape_root_and_session_id_mismatch_without_running_client(self):
        other_id = str(uuid.uuid4())
        valid_id = self.session_id
        cases = [
            ("not-a-uuid", f"{REMOTE_ROOT}/not-a-uuid"),
            (valid_id, REMOTE_ROOT),
            (valid_id, f"{REMOTE_ROOT}/{other_id}"),
            (valid_id, f"{REMOTE_ROOT}/../{valid_id}"),
            (valid_id, f"/{REMOTE_ROOT}/{valid_id}"),
            (valid_id, f"OtherRoot/{valid_id}"),
            (valid_id, ""),
            (valid_id, None),
            (valid_id.upper(), f"{REMOTE_ROOT}/{valid_id.upper()}"),
            ("", f"{REMOTE_ROOT}/"),
        ]
        service, runner = self.service(
            lambda argv, kwargs: self.completed(
                argv,
                stdout="The requested directory to delete online has been deleted\n",
            )
        )

        for session_id, remote_dir in cases:
            with self.subTest(session_id=session_id, remote_dir=remote_dir):
                with self.assertRaises(OneDriveError):
                    service.remove_session_directory(session_id, remote_dir)

        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
