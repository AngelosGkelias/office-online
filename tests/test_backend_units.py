from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from office_online_backend.config import AppConfig, Paths, load_config
from office_online_backend.errors import BackendError, FrameError, OneDriveError
from office_online_backend.native import (
    MAX_OUTPUT_FRAME,
    encode_frame,
    pop_frames,
)
from office_online_backend.onedrive import OneDriveService
from office_online_backend.sessions import Session, SessionStore
from office_online_backend.util import (
    copy_file_verified,
    extension_bridge_url,
    extension_id_is_valid,
    is_microsoft_https_url,
    notify_desktop,
    replace_with_backup,
    resolve_document_path,
    sanitize_onedrive_filename,
    url_from_link_output,
)

EXTENSION_ID = "a" * 32


class FakeRunner:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if self.callback is not None:
            return self.callback(argv, kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class TemporaryEnvironment(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="office-online-test-")
        root = Path(self._temporary.name)
        self.environment = {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "run"),
        }
        self._patch = mock.patch.dict(os.environ, self.environment, clear=False)
        self._patch.start()
        self.root = root
        self.paths = Paths.from_env()
        self.paths.ensure_base_dirs()

    def tearDown(self):
        self._patch.stop()
        self._temporary.cleanup()

    def make_document(self, name="Report.docx", content=b"original bytes"):
        path = self.root / name
        path.write_bytes(content)
        return path


class ConfigAndUtilTests(TemporaryEnvironment):
    def test_xdg_paths_and_private_modes(self):
        self.assertEqual(self.paths.config_file.parent, Path(self.environment["XDG_CONFIG_HOME"]) / "office-online")
        self.assertEqual(self.paths.socket_path, Path(self.environment["XDG_RUNTIME_DIR"]) / "office-online" / "control.sock")
        for path in (
            self.paths.config_dir,
            self.paths.data_dir,
            self.paths.drive_dir,
            self.paths.onedrive_config_dir,
            self.paths.socket_dir,
        ):
            self.assertTrue(path.is_dir())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.paths.socket_dir.stat().st_mode), 0o700)

    def test_config_defaults_and_validation(self):
        config = load_config(self.paths)
        self.assertEqual(config.browser, "google-chrome")
        self.assertEqual(config.onedrive_binary, "onedrive")
        self.assertEqual(config.extension_id, "")

        self.paths.config_file.write_text(
            json.dumps(
                {
                    "extension_id": EXTENSION_ID,
                    "browser": "/usr/bin/google-chrome",
                    "onedrive_binary": "/usr/bin/onedrive",
                }
            ),
            encoding="utf-8",
        )
        os.chmod(self.paths.config_file, 0o600)
        config = load_config(self.paths)
        self.assertEqual(config.extension_id, EXTENSION_ID)
        self.assertTrue(extension_id_is_valid(config.extension_id))
        self.assertFalse(extension_id_is_valid("z" * 32))
        self.assertFalse(extension_id_is_valid("a" * 31))
        with self.assertRaises(BackendError):
            extension_bridge_url("not-valid", "session")

    def test_resolve_document_accepts_file_uri_and_rejects_other_types(self):
        document = self.make_document("Quarterly Report.docx")
        self.assertEqual(resolve_document_path(str(document)), document.resolve())
        uri = "file://" + str(document).replace(" ", "%20")
        self.assertEqual(resolve_document_path(uri), document.resolve())
        upper = self.make_document("Slides.PPTX", b"slides")
        self.assertEqual(resolve_document_path(str(upper)), upper.resolve())
        text = self.make_document("notes.txt", b"x")
        with self.assertRaises(BackendError):
            resolve_document_path(str(text))
        with self.assertRaises(BackendError):
            resolve_document_path("file://otherhost/tmp/x.docx")
        with self.assertRaises(BackendError):
            resolve_document_path(str(self.root / "missing.docx"))

    def test_copy_is_real_file_and_verified(self):
        source = self.make_document()
        link = self.root / "source-link.docx"
        link.symlink_to(source)
        destination = self.paths.drive_dir / "copy.docx"
        digest = copy_file_verified(link, destination)
        self.assertFalse(destination.is_symlink())
        self.assertTrue(destination.is_file())
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(len(digest), 64)

    def test_onedrive_safe_filename(self):
        self.assertEqual(sanitize_onedrive_filename("Report.docx"), "Report.docx")
        sanitized = sanitize_onedrive_filename('bad:*?<>| name .docx')
        self.assertNotIn(":", sanitized)
        self.assertTrue(sanitized.endswith(".docx"))
        self.assertTrue(sanitize_onedrive_filename("CON.docx").startswith("_"))

    def test_link_output_and_url_validation(self):
        self.assertEqual(
            url_from_link_output("noise\nWebURL: https://tenant.sharepoint.com/x/y.docx\n"),
            "https://tenant.sharepoint.com/x/y.docx",
        )
        self.assertEqual(
            url_from_link_output("File Shareable Link: https://1drv.ms/w/s!abc\n"),
            "https://1drv.ms/w/s!abc",
        )
        self.assertIsNone(url_from_link_output("nothing here"))
        self.assertTrue(is_microsoft_https_url("https://tenant-my.sharepoint.com/personal/x"))
        self.assertTrue(is_microsoft_https_url("https://1drv.ms/w/s!abc"))
        self.assertFalse(is_microsoft_https_url("http://tenant.sharepoint.com/x"))
        self.assertFalse(is_microsoft_https_url("https://evil.example/x"))
        self.assertFalse(is_microsoft_https_url("https://tenant.sharepoint.com.evil.example/x"))
        self.assertFalse(is_microsoft_https_url("https://user:pass@tenant.sharepoint.com/x"))

    def test_atomic_replace_keeps_previous_backup(self):
        path = self.paths.data_dir / "state.json"
        replace_with_backup(path, b"first")
        replace_with_backup(path, b"second")
        self.assertEqual(path.read_bytes(), b"second")
        self.assertEqual(path.with_name("state.json.bak").read_bytes(), b"first")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_desktop_notification_uses_no_shell(self):
        calls = []
        with mock.patch("office_online_backend.util.shutil.which", return_value="/usr/bin/notify-send"):
            result = notify_desktop(
                "Title",
                "Body",
                urgency="critical",
                runner=lambda argv, **kwargs: calls.append((argv, kwargs)),
            )
        self.assertTrue(result)
        argv, kwargs = calls[0]
        self.assertEqual(argv[0], "/usr/bin/notify-send")
        self.assertIn("--urgency=critical", argv)
        self.assertFalse(kwargs["shell"])


class NativeFramingTests(unittest.TestCase):
    def test_partial_and_multiple_frames(self):
        buffer = bytearray()
        first = encode_frame(b'{"id":1}')
        second = encode_frame(b'{"id":2}')
        encoded = first + second
        buffer.extend(encoded[:3])
        self.assertEqual(pop_frames(buffer), [])
        buffer.extend(encoded[3:7])
        self.assertEqual(pop_frames(buffer), [])
        buffer.extend(encoded[7:])
        self.assertEqual(pop_frames(buffer), [b'{"id":1}', b'{"id":2}'])
        self.assertEqual(buffer, bytearray())

    def test_zero_and_oversized_frames_rejected(self):
        with self.assertRaises(FrameError):
            pop_frames(bytearray((0).to_bytes(4, "little")))
        with self.assertRaises(FrameError):
            pop_frames(bytearray((64 * 1024 * 1024 + 1).to_bytes(4, "little")))
        with self.assertRaises(FrameError):
            encode_frame(b"")
        with self.assertRaises(FrameError):
            encode_frame(b"x" * (MAX_OUTPUT_FRAME + 1))

    def test_frame_roundtrip(self):
        payload = b'{"id":"x","action":"status"}'
        encoded = encode_frame(payload)
        self.assertEqual(int.from_bytes(encoded[:4], "little"), len(payload))
        buffer = bytearray(encoded)
        self.assertEqual(pop_frames(buffer), [payload])


class SessionStoreTests(TemporaryEnvironment):
    def session(self):
        return Session(
            session_id="11111111-1111-1111-1111-111111111111",
            name="Report.docx",
            original_path=str(self.root / "Report.docx"),
            url="https://tenant.sharepoint.com/x",
            state="editing",
            tab_id=7,
            error=None,
            last_saved_at=None,
            result_path=None,
        )

    def test_roundtrip_and_public_shape(self):
        store = SessionStore(self.paths)
        store.add(self.session())
        reloaded = SessionStore(self.paths)
        snap = reloaded.snapshot("11111111-1111-1111-1111-111111111111")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.tab_id, 7)
        self.assertEqual(
            set(snap.public()),
            {
                "session_id",
                "name",
                "original_path",
                "url",
                "state",
                "tab_id",
                "error",
                "last_saved_at",
                "result_path",
                "cloud_deleted_at",
            },
        )
        self.assertEqual(stat.S_IMODE(self.paths.sessions_file.stat().st_mode), 0o600)

    def test_corrupt_main_recovers_from_backup(self):
        store = SessionStore(self.paths)
        store.add(self.session())
        store.update(self.session().session_id, tab_id=9)
        self.paths.sessions_file.write_text("{not json", encoding="utf-8")
        recovered = SessionStore(self.paths)
        self.assertTrue(recovered.recovered_from_backup)
        self.assertEqual(recovered.snapshot(self.session().session_id).tab_id, 7)


class OneDriveServiceTests(TemporaryEnvironment):
    def setUp(self):
        super().setUp()
        self.binary = self.root / "fake-onedrive"
        self.binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(self.binary, 0o700)
        self.refresh = self.paths.onedrive_config_dir / "refresh_token"
        self.refresh.write_text("token", encoding="utf-8")
        os.chmod(self.refresh, 0o600)

    def service(self, runner):
        return OneDriveService(
            str(self.binary),
            self.paths.onedrive_config_dir,
            self.paths.drive_dir,
            runner=runner,
            upload_timeout=12,
            download_timeout=13,
            link_timeout=14,
        )

    def test_upload_flags_are_narrow_and_never_delete_remote(self):
        runner = FakeRunner()
        service = self.service(runner)
        service.upload_session("OfficeOnline/session")
        argv, kwargs = runner.calls[0]
        self.assertEqual(argv[0], str(self.binary))
        self.assertIn("--confdir", argv)
        self.assertIn(str(self.paths.onedrive_config_dir), argv)
        self.assertIn("--syncdir", argv)
        self.assertIn(str(self.paths.drive_dir), argv)
        self.assertIn("--upload-only", argv)
        self.assertIn("--no-remote-delete", argv)
        self.assertIn("--single-directory", argv)
        self.assertIn("OfficeOnline/session", argv)
        self.assertNotIn("--resync", argv)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 12)

    def test_download_uses_fresh_staging_and_verifies_file(self):
        staging = self.paths.temporary_dir / "stage"

        def callback(argv, kwargs):
            sync_dir = Path(argv[argv.index("--syncdir") + 1])
            target = sync_dir / "OfficeOnline" / "session" / "Report.docx"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"cloud bytes")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        runner = FakeRunner(callback)
        service = self.service(runner)
        downloaded = service.download_session("OfficeOnline/session", "Report.docx", staging)
        argv, kwargs = runner.calls[0]
        self.assertIn("--download-only", argv)
        self.assertIn("--cleanup-local-files", argv)
        self.assertIn("--single-directory", argv)
        self.assertIn("OfficeOnline/session", argv)
        self.assertEqual(downloaded.path.read_bytes(), b"cloud bytes")
        self.assertEqual(kwargs["timeout"], 13)
        self.assertEqual(len(downloaded.sha256), 64)

    def test_link_is_authenticated_and_never_creates_sharing_links(self):
        outputs = [
            subprocess.CompletedProcess([], 0, stdout="WebURL: https://tenant.sharepoint.com/doc\n", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="not synced"),
        ]
        runner = FakeRunner(lambda argv, kwargs: outputs.pop(0))
        service = self.service(runner)
        url, mode = service.link_for_editing("OfficeOnline/session/Report.docx")
        self.assertEqual((url, mode), ("https://tenant.sharepoint.com/doc", "weburl"))
        with self.assertRaises(OneDriveError):
            service.link_for_editing("OfficeOnline/session/Report.docx")
        for argv, _ in runner.calls:
            self.assertNotIn("--create-share-link", argv)

    def test_non_microsoft_output_is_never_accepted(self):
        runner = FakeRunner(
            lambda argv, kwargs: subprocess.CompletedProcess(
                argv, 0, stdout="WebURL: https://evil.example/file.docx\n", stderr=""
            )
        )
        service = self.service(runner)
        with self.assertRaises(OneDriveError):
            service.link_for_editing("OfficeOnline/x/Report.docx")

    def test_missing_download_file_and_timeout_are_errors(self):
        runner = FakeRunner()
        service = self.service(runner)
        staging = self.paths.temporary_dir / "stage-missing"
        with self.assertRaises(OneDriveError):
            service.download_session("OfficeOnline/x", "Report.docx", staging)

        def timeout(argv, kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        service = self.service(FakeRunner(timeout))
        with self.assertRaises(OneDriveError) as caught:
            service.upload_session("OfficeOnline/x")
        self.assertIn("timed out", str(caught.exception))

    def test_setup_never_syncs_and_reauth_is_explicit(self):
        service = self.service(FakeRunner())
        command = service.setup_command()
        self.assertNotIn("--sync", command)
        self.assertNotIn("--monitor", command)
        self.assertNotIn("--resync", command)
        self.assertNotIn("--download-only", command)
        self.assertIn("--reauth", command)
        self.assertIn(str(self.paths.drive_dir), command)

        calls = []
        with mock.patch("office_online_backend.onedrive.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            self.assertEqual(service.setup_interactive(), 0)
            calls.append(run.call_args)
        argv, kwargs = calls[0]
        self.assertIn("--reauth", argv[0])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], service.setup_timeout)
        profile = (self.paths.onedrive_config_dir / "config").read_text()
        self.assertIn(f'sync_dir = "{self.paths.drive_dir}"\n', profile)


if __name__ == "__main__":
    unittest.main()
