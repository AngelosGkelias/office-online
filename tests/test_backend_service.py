from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from office_online_backend.backend import BackendService
from office_online_backend.cli import socket_request
from office_online_backend.config import AppConfig, Paths
from office_online_backend.errors import BackendError, OneDriveError
from office_online_backend.onedrive import DownloadedFile
from office_online_backend.server import UnixControlServer
from office_online_backend.sessions import SessionStore

EXTENSION_ID = "a" * 32


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeOneDrive:
    upload_timeout = 5.0
    link_timeout = 2.0

    def __init__(self):
        self.uploads = []
        self.downloads = []
        self.available = True
        self.authenticated = True
        self.upload_error = None
        self.download_error = None
        self.download_bytes = b"cloud version"
        self.upload_started = threading.Event()
        self.upload_gate = None

    def available(self):
        return self.available

    def is_authenticated(self):
        return self.authenticated

    def upload_session(self, remote_dir):
        self.uploads.append(remote_dir)
        self.upload_started.set()
        if self.upload_gate is not None:
            if not self.upload_gate.wait(5):
                raise OneDriveError("test upload gate timed out")
        if self.upload_error is not None:
            raise self.upload_error

    def link_for_editing(self, remote_relpath):
        return f"https://tenant.sharepoint.com/personal/office/{remote_relpath}", "weburl"

    def download_session(self, remote_dir, filename, staging_dir):
        self.downloads.append((remote_dir, filename))
        if self.download_error is not None:
            raise self.download_error
        path = Path(staging_dir) / remote_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.download_bytes)
        return DownloadedFile(path=path, sha256=sha(self.download_bytes), size=len(self.download_bytes))


class FakeLauncher:
    def __init__(self):
        self.launched = []
        self.callback = None

    def validate(self):
        return ["/usr/bin/google-chrome"]

    def launch(self, extension_id, session_id):
        self.launched.append((extension_id, session_id))
        if self.callback is not None:
            self.callback(session_id)


class BackendTestCase(unittest.TestCase):
    retry_initial = 60.0

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="office-online-service-")
        self.root = Path(self._temporary.name)
        self.environment = {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "run"),
        }
        self._patch = mock.patch.dict(os.environ, self.environment, clear=False)
        self._patch.start()
        self.paths = Paths.from_env()
        self.paths.ensure_base_dirs()
        os.chmod(self.paths.config_dir, 0o700)
        self.config = AppConfig(
            extension_id=EXTENSION_ID,
            browser="google-chrome",
            onedrive_binary="onedrive",
        )
        self.store = SessionStore(self.paths)
        self.onedrive = FakeOneDrive()
        self.launcher = FakeLauncher()
        self.notifications = []
        self.backend = self.make_backend()

    def make_backend(self):
        return BackendService(
            self.paths,
            self.config,
            self.store,
            self.onedrive,
            launcher=self.launcher,
            notifier=lambda title, body, urgency="normal": self.notifications.append(
                (title, body, urgency)
            ),
            save_grace_seconds=0.05,
            retry_initial_seconds=self.retry_initial,
            retry_max_seconds=0.2,
        )

    def tearDown(self):
        try:
            self.backend.shutdown()
        finally:
            self._patch.stop()
            self._temporary.cleanup()

    def document(self, name="Report.docx", content=b"original"):
        path = self.root / name
        path.write_bytes(content)
        return path

    def open(self, *paths):
        return self.backend.open_paths([str(path) for path in paths])

    def wait_until(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail("condition was not reached before timeout")

    def latest_session(self):
        sessions = self.store.public_sessions()
        return sessions[-1] if sessions else None

    def backup_files(self):
        return [path for path in self.paths.backups_dir.rglob("*") if path.is_file()]


class OpenTests(BackendTestCase):
    def test_open_uploads_before_launching_ready_bridge(self):
        observed = []
        self.launcher.callback = lambda session_id: observed.append(
            self.store.snapshot(session_id)
        )
        document = self.document()
        sessions = self.open(document)
        session = sessions[-1]
        self.assertEqual(session["state"], "editing")
        self.assertTrue(session["url"].startswith("https://tenant.sharepoint.com/"))
        self.assertEqual(len(self.onedrive.uploads), 1)
        self.assertEqual(len(self.launcher.launched), 1)
        self.assertEqual(observed[0].state, "editing")
        self.assertTrue(observed[0].url)
        working = Path(self.store.snapshot(session["session_id"]).working_path)
        self.assertTrue(working.is_file())
        self.assertFalse(working.is_symlink())
        self.assertEqual(working.read_bytes(), document.read_bytes())
        self.assertTrue(self.launcher.launched[0][1] == session["session_id"])

    def test_duplicate_open_focuses_without_reupload(self):
        document = self.document()
        first = self.open(document)[-1]
        second = self.open(document)[-1]
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(len(self.onedrive.uploads), 1)
        self.assertEqual(len(self.launcher.launched), 2)

    def test_open_validates_all_paths_before_uploading(self):
        document = self.document()
        with self.assertRaises(BackendError):
            self.backend.open_paths([str(document), str(self.root / "missing.docx")])
        self.assertEqual(self.onedrive.uploads, [])
        self.assertEqual(self.store.public_sessions(), [])

    def test_status_socket_stays_responsive_while_upload_blocks(self):
        gate = threading.Event()
        self.onedrive.upload_gate = gate
        server = UnixControlServer(self.paths, self.backend)
        server.prepare()
        server.bind()
        stop_event = threading.Event()
        server_thread = threading.Thread(
            target=server.serve_forever, args=(stop_event,), daemon=True
        )
        server_thread.start()
        document = self.document()
        error = []

        def open_in_thread():
            try:
                self.open(document)
            except BaseException as caught:  # pragma: no cover - reported below
                error.append(caught)

        opener = threading.Thread(target=open_in_thread)
        opener.start()
        try:
            self.assertTrue(self.onedrive.upload_started.wait(2))
            started = time.monotonic()
            response = socket_request(
                self.paths, {"id": "status-while-open", "action": "status"}, timeout=2
            )
            elapsed = time.monotonic() - started
            self.assertTrue(response["ok"])
            self.assertLess(elapsed, 1.0)
            self.assertEqual(response["sessions"][0]["state"], "opening")
            gate.set()
            opener.join(5)
            self.assertFalse(opener.is_alive())
            self.assertEqual(error, [])
            self.assertEqual(self.store.public_sessions()[0]["state"], "editing")
        finally:
            gate.set()
            stop_event.set()
            server_thread.join(3)
            server.close()
            opener.join(3)


class SaveTests(BackendTestCase):
    def test_manual_save_backs_up_and_atomically_replaces_original(self):
        document = self.document(content=b"original")
        session = self.open(document)[-1]
        self.onedrive.download_bytes = b"cloud edit one"
        self.backend.request_save(session["session_id"], finish=False)
        self.wait_until(
            lambda: self.store.snapshot(session["session_id"]).result_path is not None
        )
        current = self.store.snapshot(session["session_id"])
        self.assertEqual(current.state, "editing")
        self.assertEqual(document.read_bytes(), b"cloud edit one")
        self.assertEqual(current.local_hash, sha(b"cloud edit one"))
        self.assertEqual(current.result_path, str(document.resolve()))
        self.assertEqual(Path(current.working_path).read_bytes(), b"cloud edit one")
        backups = self.backup_files()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"original")
        self.assertEqual(len(self.onedrive.uploads), 1)

        self.onedrive.download_bytes = b"cloud edit two"
        self.backend.request_save(session["session_id"], finish=False)
        self.wait_until(
            lambda: self.store.snapshot(session["session_id"]).local_hash
            == sha(b"cloud edit two")
        )
        self.assertEqual(document.read_bytes(), b"cloud edit two")
        self.assertEqual(self.store.snapshot(session["session_id"]).state, "editing")
        self.assertEqual(len(self.onedrive.uploads), 1)

    def test_final_save_ends_tracking_and_notifies(self):
        document = self.document(content=b"original")
        session = self.open(document)[-1]
        self.onedrive.download_bytes = b"final cloud"
        self.backend.request_save(session["session_id"], finish=True)
        self.wait_until(
            lambda: self.store.snapshot(session["session_id"]).state == "saved"
        )
        current = self.store.snapshot(session["session_id"])
        self.assertEqual(document.read_bytes(), b"final cloud")
        self.assertIsNone(current.tab_id)
        self.assertIsNotNone(current.last_saved_at)
        self.assertTrue(
            any("save complete" in title.lower() for title, _, _ in self.notifications)
        )

    def test_external_change_preserves_original_and_writes_sibling(self):
        document = self.document(content=b"original")
        session = self.open(document)[-1]
        document.write_bytes(b"external edit")
        self.onedrive.download_bytes = b"cloud copy"
        self.backend.request_save(session["session_id"], finish=False)
        self.wait_until(
            lambda: self.store.snapshot(session["session_id"]).state == "conflict"
        )
        current = self.store.snapshot(session["session_id"])
        self.assertEqual(document.read_bytes(), b"external edit")
        sibling = Path(current.result_path)
        self.assertNotEqual(sibling, document)
        self.assertTrue(sibling.is_file())
        self.assertEqual(sibling.read_bytes(), b"cloud copy")
        self.assertEqual(current.local_hash, sha(b"original"))
        self.assertTrue(self.backup_files())
        self.assertEqual(self.backup_files()[0].read_bytes(), b"external edit")
        self.assertTrue(
            any("conflict" in title.lower() for title, _, _ in self.notifications)
        )

    def test_download_failure_does_not_touch_original_and_retry_recovers(self):
        document = self.document(content=b"original")
        session = self.open(document)[-1]
        self.onedrive.download_error = OneDriveError("simulated download failure")
        self.backend.request_save(session["session_id"], finish=False)
        self.wait_until(
            lambda: self.store.snapshot(session["session_id"]).state == "error"
        )
        current = self.store.snapshot(session["session_id"])
        self.assertEqual(document.read_bytes(), b"original")
        self.assertIn("simulated download failure", current.error)
        self.assertIsNone(current.result_path)
        self.assertEqual(self.backup_files(), [])
        self.assertTrue(
            any("save error" in title.lower() for title, _, _ in self.notifications)
        )
        uploads_before_retry = len(self.onedrive.uploads)

        self.onedrive.download_error = None
        self.onedrive.download_bytes = b"recovered cloud"
        self.backend.retry(session["session_id"])
        self.wait_until(
            lambda: self.store.snapshot(session["session_id"]).state == "editing"
        )
        self.assertEqual(document.read_bytes(), b"recovered cloud")
        self.assertEqual(len(self.onedrive.uploads), uploads_before_retry)

    def test_tab_closed_queues_final_save(self):
        document = self.document(content=b"original")
        session = self.open(document)[-1]
        self.onedrive.download_bytes = b"closed final"
        self.backend.tab_closed(session["session_id"])
        self.wait_until(
            lambda: self.store.snapshot(session["session_id"]).state == "saved"
        )
        self.assertEqual(document.read_bytes(), b"closed final")
        self.assertTrue(
            any("save complete" in title.lower() for title, _, _ in self.notifications)
        )

    def test_native_connection_eof_saves_only_that_connections_sessions(self):
        first_doc = self.document("First.docx", b"first original")
        second_doc = self.document("Second.docx", b"second original")
        first = self.open(first_doc)[-1]
        second = self.open(second_doc)[-1]
        self.backend.bind("connection-a", first["session_id"], 11)
        self.backend.bind("connection-b", second["session_id"], 22)
        self.onedrive.download_bytes = b"saved on eof"
        self.backend.connection_closed("connection-a")
        self.wait_until(
            lambda: self.store.snapshot(first["session_id"]).state == "saved"
        )
        self.assertEqual(self.store.snapshot(second["session_id"]).state, "editing")
        self.assertEqual(first_doc.read_bytes(), b"saved on eof")
        self.assertEqual(second_doc.read_bytes(), b"second original")
        # Two initial uploads; the final save only downloads.
        self.assertEqual(len(self.onedrive.uploads), 2)


class ReconnectTests(BackendTestCase):
    def test_reconnect_within_grace_cancels_final_save(self):
        document = self.document(content=b"still editing")
        session = self.open(document)[-1]
        self.backend.bind("connection-old", session["session_id"], 5)
        self.backend.connection_closed("connection-old")
        self.backend.bind("connection-new", session["session_id"], 6)
        time.sleep(self.backend.save_grace_seconds * 3 + 0.05)
        current = self.store.snapshot(session["session_id"])
        self.assertEqual(current.state, "editing")
        self.assertEqual(current.tab_id, 6)
        self.assertEqual(document.read_bytes(), b"still editing")
        self.assertEqual(self.onedrive.downloads, [])


class RecoveryTests(BackendTestCase):
    retry_initial = 0.05

    def test_restart_retries_saving_and_marks_editing_for_recovery(self):
        saving_doc = self.document("Saving.docx", b"saving original")
        editing_doc = self.document("Editing.docx", b"editing original")
        saving = self.open(saving_doc)[-1]
        editing = self.open(editing_doc)[-1]
        self.store.update(
            saving["session_id"], state="saving", finish_requested=True
        )
        self.store.update(editing["session_id"], state="editing")
        self.backend.shutdown()
        uploads_before = len(self.onedrive.uploads)

        new_store = SessionStore(self.paths)
        new_backend = BackendService(
            self.paths,
            self.config,
            new_store,
            self.onedrive,
            launcher=self.launcher,
            notifier=lambda *args, **kwargs: None,
            save_grace_seconds=0.05,
            retry_initial_seconds=0.05,
            retry_max_seconds=0.2,
        )
        self.backend = new_backend
        self.onedrive.download_bytes = b"recovered final"
        new_backend.recover_startup()
        self.wait_until(
            lambda: new_store.snapshot(saving["session_id"]).state == "saved"
        )
        self.assertEqual(saving_doc.read_bytes(), b"recovered final")
        self.assertEqual(len(self.onedrive.uploads), uploads_before)
        self.wait_until(
            lambda: new_store.snapshot(editing["session_id"]).state == "saved"
        )
        self.assertEqual(editing_doc.read_bytes(), b"recovered final")


class AutoRetryTests(BackendTestCase):
    retry_initial = 0.15

    def test_network_failure_is_retried_periodically(self):
        document = self.document(content=b"original")
        session = self.open(document)[-1]
        self.onedrive.download_error = OneDriveError("transient network error")
        self.backend.request_save(session["session_id"], finish=False)
        self.wait_until(
            lambda: self.store.snapshot(session["session_id"]).state == "error"
        )
        self.onedrive.download_error = None
        self.onedrive.download_bytes = b"after retry"
        self.wait_until(
            lambda: self.store.snapshot(session["session_id"]).state == "editing",
            timeout=5,
        )
        self.assertEqual(document.read_bytes(), b"after retry")
        # Recovery retries download first; they never re-upload a stale copy.
        self.assertEqual(len(self.onedrive.uploads), 1)


if __name__ == "__main__":
    unittest.main()
