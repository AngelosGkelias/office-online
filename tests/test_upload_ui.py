#!/usr/bin/env python3
"""Unit tests for the GTK4 upload progress window, using no real display."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from office_online_backend import cli, upload_ui  # noqa: E402
from office_online_backend.errors import BackendError  # noqa: E402

try:
    import gi

    from gi.repository import Gio
except ImportError:  # pragma: no cover - GTK/PyGObject is not a test dependency
    Gio = None


class ProtocolParsingTests(unittest.TestCase):
    def test_text_progress_complete_and_error_commands(self) -> None:
        self.assertEqual(
            upload_ui.parse_command("text Uploading report.docx…"),
            upload_ui.UploadCommand("text", "Uploading report.docx…"),
        )
        self.assertEqual(
            upload_ui.parse_command("progress 42.5"),
            upload_ui.UploadCommand("progress", "42.5"),
        )
        self.assertEqual(
            upload_ui.parse_command("100"),
            upload_ui.UploadCommand("complete"),
        )
        self.assertEqual(
            upload_ui.parse_command("error OneDrive is unavailable"),
            upload_ui.UploadCommand("error", "OneDrive is unavailable"),
        )

    def test_blank_crlf_and_unknown_lines_are_ignored(self) -> None:
        for line in ("", "\n", "\r\n", "unknown command", "progress", "text"):
            self.assertIsNone(upload_ui.parse_command(line), line)

    def test_protocol_reader_handles_partial_and_multibyte_lines(self) -> None:
        reader = upload_ui.ProtocolReader()
        payload = "text Uploading working copy to OneDrive…\n".encode("utf-8")
        split = payload.index("…".encode("utf-8")) + 1
        self.assertEqual(reader.feed(payload[:split]), [])
        self.assertEqual(
            reader.feed(payload[split:]),
            [upload_ui.UploadCommand("text", "Uploading working copy to OneDrive…")],
        )

    def test_protocol_reader_handles_multiple_lines_and_final_partial(self) -> None:
        reader = upload_ui.ProtocolReader()
        commands = reader.feed(b"text Preparing\nprogress 25\n100\n")
        self.assertEqual(
            commands,
            [
                upload_ui.UploadCommand("text", "Preparing"),
                upload_ui.UploadCommand("progress", "25"),
                upload_ui.UploadCommand("complete"),
            ],
        )
        reader = upload_ui.ProtocolReader()
        self.assertEqual(reader.feed(b"text still uploading"), [])
        self.assertEqual(
            reader.finish(),
            [upload_ui.UploadCommand("text", "still uploading")],
        )


class ViewStateTests(unittest.TestCase):
    def test_text_progress_and_completion(self) -> None:
        state = upload_ui.UploadViewState()
        self.assertEqual(state.display_text, upload_ui.DEFAULT_STATUS)
        state.apply(upload_ui.UploadCommand("text", "Preparing files"))
        self.assertEqual(state.display_text, "Preparing files")
        state.apply(upload_ui.UploadCommand("progress", "37.5"))
        self.assertEqual(state.fraction, 0.375)
        state.apply(upload_ui.UploadCommand("progress", "150"))
        self.assertEqual(state.fraction, 1.0)
        state.apply(upload_ui.UploadCommand("progress", "-2"))
        self.assertEqual(state.fraction, 0.0)
        state.apply(upload_ui.UploadCommand("complete"))
        self.assertTrue(state.completed)
        self.assertEqual(state.fraction, 1.0)
        self.assertTrue(state.should_close_on_eof)

    def test_invalid_progress_is_ignored(self) -> None:
        state = upload_ui.UploadViewState()
        state.apply(upload_ui.UploadCommand("progress", "not-a-number"))
        self.assertIsNone(state.fraction)

    def test_error_is_terminal_and_kept_open_on_eof(self) -> None:
        state = upload_ui.UploadViewState()
        state.apply(upload_ui.UploadCommand("error", "OneDrive is unavailable"))
        self.assertEqual(state.error, "OneDrive is unavailable")
        self.assertEqual(
            state.display_text,
            "Upload failed\n\nOneDrive is unavailable",
        )
        self.assertFalse(state.should_close_on_eof)
        # Later progress/complete frames must not hide the error.
        state.apply(upload_ui.UploadCommand("text", "Ignored"))
        state.apply(upload_ui.UploadCommand("complete"))
        self.assertEqual(state.display_text, "Upload failed\n\nOneDrive is unavailable")
        self.assertFalse(state.completed)


class FakeStdin:
    def __init__(self, *, broken: bool = False) -> None:
        self.written = bytearray()
        self.broken = broken
        self.closed = False

    def write(self, data: bytes) -> int:
        if self.broken:
            raise BrokenPipeError("window dismissed")
        self.written.extend(data)
        return len(data)

    def flush(self) -> None:
        if self.broken:
            raise BrokenPipeError("window dismissed")

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, *, broken: bool = False) -> None:
        self.stdin = FakeStdin(broken=broken)


class HandleTests(unittest.TestCase):
    def test_handle_writes_protocol_and_closes_on_completion(self) -> None:
        process = FakeProcess()
        handle = upload_ui.UploadUIHandle(process)  # type: ignore[arg-type]
        handle.update("Uploading  report.docx\nnow")
        handle.progress(37.5)
        handle.complete()
        self.assertEqual(
            bytes(process.stdin.written),
            b"text Uploading report.docx now\nprogress 37.5\n100\n",
        )
        self.assertTrue(process.stdin.closed)
        handle.close()  # idempotent
        handle.complete()  # no late writes after close
        self.assertEqual(
            bytes(process.stdin.written),
            b"text Uploading report.docx now\nprogress 37.5\n100\n",
        )

    def test_handle_error_is_sanitized_and_closes_stdin(self) -> None:
        process = FakeProcess()
        handle = upload_ui.UploadUIHandle(process)  # type: ignore[arg-type]
        handle.fail("first line\nsecond line\twith tabs")
        self.assertEqual(
            bytes(process.stdin.written),
            b"error first line second line with tabs\n",
        )
        self.assertTrue(process.stdin.closed)

    def test_broken_pipe_is_not_fatal_anywhere(self) -> None:
        process = FakeProcess(broken=True)
        handle = upload_ui.UploadUIHandle(process)  # type: ignore[arg-type]
        handle.update("Uploading")
        handle.progress(50)
        handle.fail("boom")
        handle.complete()
        handle.close()

    def test_bad_progress_value_is_ignored(self) -> None:
        process = FakeProcess()
        handle = upload_ui.UploadUIHandle(process)  # type: ignore[arg-type]
        handle.progress("bad")  # type: ignore[arg-type]
        self.assertEqual(bytes(process.stdin.written), b"")


class LaunchTests(unittest.TestCase):
    def test_python_executable_is_real_python_not_bundle_launcher(self) -> None:
        found = upload_ui.python_executable()
        self.assertIsNotNone(found)
        self.assertTrue(Path(found).is_file())
        self.assertIn("python", Path(found).name.lower())

    def test_launch_uses_explicit_argv_and_nonblocking_settings(self) -> None:
        process = FakeProcess()
        with mock.patch.object(upload_ui, "gtk_available", return_value=True):
            with mock.patch.object(
                upload_ui.subprocess, "Popen", return_value=process
            ) as popen:
                handle = upload_ui.launch_upload_ui("Uploading 2 documents")
        self.assertIsNotNone(handle)
        args, kwargs = popen.call_args
        self.assertEqual(
            args[0],
            [upload_ui.python_executable(), str(Path(upload_ui.__file__).resolve())],
        )
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(
            bytes(process.stdin.written),
            b"text Uploading 2 documents\n",
        )

    def test_launch_returns_none_when_gtk_or_spawn_is_unavailable(self) -> None:
        with mock.patch.object(upload_ui, "gtk_available", return_value=False):
            with mock.patch.object(upload_ui.subprocess, "Popen") as popen:
                self.assertIsNone(upload_ui.launch_upload_ui())
                popen.assert_not_called()

        with mock.patch.object(upload_ui, "gtk_available", return_value=True):
            with mock.patch.object(
                upload_ui.subprocess,
                "Popen",
                side_effect=OSError("no display"),
            ):
                self.assertIsNone(upload_ui.launch_upload_ui())


class CliIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="office-online-ui-cli-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.document = self.root / "Report.docx"
        self.document.write_text("content", encoding="utf-8")

    def _run_open(self, response=None, error=None, gui=True):
        handle = mock.Mock()
        patchers = [
            mock.patch.object(cli, "socket_request"),
            mock.patch.object(cli.upload_ui, "launch_upload_ui", return_value=handle),
            mock.patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": str(self.root / "config"),
                    "XDG_DATA_HOME": str(self.root / "data"),
                    "XDG_RUNTIME_DIR": str(self.root / "run"),
                },
            ),
        ]
        with patchers[0] as request, patchers[1] as launch, patchers[2]:
            if error is not None:
                request.side_effect = error
            else:
                request.return_value = response
            code = cli.command_open([str(self.document)], gui=gui)
        return code, handle, launch

    def test_success_completes_ui_and_prints_json(self) -> None:
        response = {"id": "cli-open", "ok": True, "sessions": []}
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            code, handle, launch = self._run_open(response=response)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), response)
        launch.assert_called_once()
        self.assertIn("Uploading working copy", launch.call_args.args[0])
        handle.complete.assert_called_once_with()
        handle.fail.assert_not_called()
        handle.close.assert_not_called()

    def test_backend_failure_shows_error_without_raising(self) -> None:
        with mock.patch("sys.stderr", new_callable=io.StringIO) as errors:
            code, handle, _launch = self._run_open(
                response={"id": "cli-open", "ok": False, "error": "upload failed"}
            )
        self.assertEqual(code, 1)
        self.assertIn("office-online: upload failed", errors.getvalue())
        handle.fail.assert_called_once_with("upload failed")
        handle.complete.assert_not_called()

    def test_connection_failure_shows_error_without_raising(self) -> None:
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            code, handle, _launch = self._run_open(
                error=BackendError("daemon is not running")
            )
        self.assertEqual(code, 1)
        handle.fail.assert_called_once_with("daemon is not running")
        handle.complete.assert_not_called()

    def test_invalid_path_shows_ui_error(self) -> None:
        notes = self.root / "notes.txt"
        notes.write_text("not an office file", encoding="utf-8")
        handle = mock.Mock()
        with mock.patch.object(cli, "socket_request") as request:
            with mock.patch.object(
                cli.upload_ui, "launch_upload_ui", return_value=handle
            ):
                with mock.patch.dict(
                    os.environ,
                    {
                        "XDG_CONFIG_HOME": str(self.root / "config"),
                        "XDG_DATA_HOME": str(self.root / "data"),
                        "XDG_RUNTIME_DIR": str(self.root / "run"),
                    },
                ):
                    with mock.patch("sys.stderr", new_callable=io.StringIO):
                        code = cli.command_open([str(notes)], gui=True)
        self.assertEqual(code, 1)
        request.assert_not_called()
        self.assertIn("unsupported file type", handle.fail.call_args.args[0])
        handle.complete.assert_not_called()

    def test_unexpected_failure_closes_ui_and_propagates(self) -> None:
        handle = mock.Mock()
        with mock.patch.object(cli, "socket_request", side_effect=RuntimeError("boom")):
            with mock.patch.object(
                cli.upload_ui, "launch_upload_ui", return_value=handle
            ):
                with mock.patch.dict(
                    os.environ,
                    {
                        "XDG_CONFIG_HOME": str(self.root / "config"),
                        "XDG_DATA_HOME": str(self.root / "data"),
                        "XDG_RUNTIME_DIR": str(self.root / "run"),
                    },
                ):
                    with self.assertRaises(RuntimeError):
                        cli.command_open([str(self.document)], gui=True)
        handle.close.assert_called_once_with()
        handle.fail.assert_not_called()

    def test_no_gui_does_not_launch_ui(self) -> None:
        response = {"id": "cli-open", "ok": True, "sessions": []}
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            code, _handle, launch = self._run_open(response=response, gui=False)
        self.assertEqual(code, 0)
        launch.assert_not_called()


class ApplicationIdTests(unittest.TestCase):
    def test_installed_icon_path_uses_xdg_data_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="office-online-icon-lookup-") as directory:
            data_home = Path(directory)
            icon = (
                data_home
                / "icons"
                / "hicolor"
                / "scalable"
                / "apps"
                / f"{upload_ui.ICON_NAME}.svg"
            )
            icon.parent.mkdir(parents=True)
            icon.write_text("<svg xmlns=\"http://www.w3.org/2000/svg\"/>\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"XDG_DATA_HOME": str(data_home)}, clear=False
            ):
                self.assertEqual(upload_ui.installed_icon_path(), icon)

    def test_application_id_matches_desktop_and_icon_names(self) -> None:
        self.assertEqual(
            upload_ui.APPLICATION_ID, "org.local.OfficeOnline.Uploader"
        )
        self.assertEqual(
            upload_ui.DESKTOP_FILENAME,
            "org.local.OfficeOnline.Uploader.desktop",
        )
        self.assertEqual(upload_ui.ICON_NAME, upload_ui.APPLICATION_ID)

    @unittest.skipIf(Gio is None, "PyGObject/Gio not installed")
    def test_application_id_is_valid_for_gio(self) -> None:
        self.assertTrue(Gio.Application.id_is_valid(upload_ui.APPLICATION_ID))

    def test_extension_artwork_exists_for_installer_reuse(self) -> None:
        artwork = ROOT / "extension" / "icons" / "icon.svg"
        self.assertTrue(artwork.is_file())
        text = artwork.read_text(encoding="utf-8")
        self.assertIn("<svg", text)
        self.assertIn("#2563eb", text)


if __name__ == "__main__":
    unittest.main()
