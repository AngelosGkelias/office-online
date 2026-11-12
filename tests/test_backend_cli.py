from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from office_online_backend.cli import socket_request
from office_online_backend.native import encode_frame, pop_frames

PROJECT = Path(__file__).resolve().parents[1]
EXTENSION_ID = "a" * 32
# The test runner may itself be a bundled application whose sys.executable is
# not a Python interpreter.  Use the real system interpreter for subprocesses.
PYTHON = shutil.which("python3") or sys.executable


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="office-online-cli-")
        self.root = Path(self._temporary.name)
        self.env = dict(os.environ)
        self.env.update(
            {
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_RUNTIME_DIR": str(self.root / "run"),
            }
        )
        self.config_dir = self.root / "config" / "office-online"
        self.config_dir.mkdir(parents=True, mode=0o700)
        os.chmod(self.config_dir, 0o700)
        self.config_file = self.config_dir / "config.json"
        self.browser = self.root / "fake-browser"
        self.browser.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "path = os.environ.get('FAKE_BROWSER_LOG')\n"
            "if path:\n"
            "    with open(path, 'a', encoding='utf-8') as handle:\n"
            "        handle.write(sys.argv[-1] + '\\n')\n",
            encoding="utf-8",
        )
        os.chmod(self.browser, 0o700)
        self.browser_log = self.root / "browser.log"
        self.env["FAKE_BROWSER_LOG"] = str(self.browser_log)
        self.env["FAKE_DOWNLOAD_TEXT"] = "downloaded from fake OneDrive"
        self.onedrive = self.root / "fake-onedrive"
        self.onedrive.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "def value(name):\n"
            "    return args[args.index(name) + 1]\n"
            "def log():\n"
            "    path = os.environ.get('FAKE_ONEDRIVE_LOG')\n"
            "    if path:\n"
            "        with open(path, 'a', encoding='utf-8') as handle:\n"
            "            handle.write(json.dumps(sys.argv) + '\\n')\n"
            "log()\n"
            "if '--version' in args:\n"
            "    print('onedrive v2.5.0-test')\n"
            "    raise SystemExit(0)\n"
            "if '--get-file-link' in args:\n"
            "    print('WebURL: https://tenant.sharepoint.com/personal/test/Report.docx')\n"
            "    raise SystemExit(0)\n"
            "if '--create-share-link' in args:\n"
            "    print('File Shareable Link: https://1drv.ms/w/s!fake')\n"
            "    raise SystemExit(0)\n"
            "if '--download-only' in args:\n"
            "    target = Path(value('--syncdir')) / value('--single-directory') / 'Report.docx'\n"
            "    target.parent.mkdir(parents=True, exist_ok=True)\n"
            "    target.write_text(os.environ.get('FAKE_DOWNLOAD_TEXT', 'downloaded'), encoding='utf-8')\n"
            "    raise SystemExit(0)\n"
            "if '--upload-only' in args:\n"
            "    raise SystemExit(0)\n"
            "if '--sync' not in args and '--monitor' not in args and '--resync' not in args:\n"
            "    confdir = Path(value('--confdir'))\n"
            "    confdir.mkdir(parents=True, exist_ok=True)\n"
            "    (confdir / 'refresh_token').write_text('fake-token', encoding='utf-8')\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        os.chmod(self.onedrive, 0o700)
        self.write_config()

    def write_config(self, onedrive_binary=None):
        self.config_file.write_text(
            json.dumps(
                {
                    "extension_id": EXTENSION_ID,
                    "browser": str(self.browser),
                    "onedrive_binary": onedrive_binary or str(self.onedrive),
                }
            ),
            encoding="utf-8",
        )
        os.chmod(self.config_file, 0o600)

    def tearDown(self):
        self._temporary.cleanup()

    def run_cli(self, *arguments, timeout=15):
        return subprocess.run(
            [PYTHON, str(PROJECT / "office_online.py"), *arguments],
            cwd=str(PROJECT),
            env=self.env,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def make_document(self, name="Report.docx", content="original"):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def start_daemon(self):
        log_path = self.root / "daemon.log"
        log = open(log_path, "w", encoding="utf-8")
        process = subprocess.Popen(
            [PYTHON, str(PROJECT / "office_online.py"), "daemon"],
            cwd=str(PROJECT),
            env=self.env,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            start_new_session=True,
        )
        self.addCleanup(_stop_process, process, log)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                response = socket_request(
                    _paths(self.env), {"id": "ready", "action": "status"}, timeout=0.5
                )
                if response.get("ok"):
                    return process
            except Exception:
                time.sleep(0.05)
        log.flush()
        details = log_path.read_text(encoding="utf-8", errors="replace")
        self.fail(f"daemon did not start: {details}")
        raise AssertionError("unreachable")

    def wait_for_session(self, session_id, state, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = socket_request(
                _paths(self.env), {"id": "poll", "action": "status"}, timeout=2
            )
            for session in response.get("sessions", []):
                if session["session_id"] == session_id and session["state"] == state:
                    return session
            time.sleep(0.05)
        self.fail(f"session did not reach {state}")


def _paths(environment):
    with mock.patch.dict(os.environ, environment, clear=False):
        from office_online_backend.config import Paths

        return Paths.from_env()


def _stop_process(process, log=None):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            process.wait(timeout=5)
    if log is not None:
        try:
            log.close()
        except Exception:
            pass


class MissingDependencyTests(CliTestCase):
    def test_doctor_gracefully_reports_missing_onedrive(self):
        self.write_config(onedrive_binary=str(self.root / "does-not-exist"))
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 1)
        self.assertIn("OneDrive client", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_setup_gracefully_reports_missing_onedrive(self):
        self.write_config(onedrive_binary=str(self.root / "does-not-exist"))
        result = self.run_cli("setup")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.config_dir / "onedrive" / "refresh_token").exists())

    def test_status_without_daemon_is_json_and_fails(self):
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("daemon is not running", payload["error"])

    def test_open_rejects_bad_type_without_traceback(self):
        path = self.root / "notes.txt"
        path.write_text("x", encoding="utf-8")
        result = self.run_cli("open", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("unsupported file type", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_setup_and_doctor_with_a_mocked_client(self):
        log = self.root / "setup.log"
        self.env["FAKE_ONEDRIVE_LOG"] = str(log)
        setup = self.run_cli("setup")
        self.assertEqual(setup.returncode, 0, setup.stderr)
        token = self.config_dir / "onedrive" / "refresh_token"
        self.assertTrue(token.is_file())
        self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
        self.assertIn("No files were synchronised", setup.stdout)

        # The first setup call had no token, so it must be an auth-only call.
        setups = []
        for line in log.read_text(encoding="utf-8").splitlines():
            setups.append(json.loads(line))
        self.assertTrue(setups)
        self.assertNotIn("--sync", setups[0])

        doctor = self.run_cli("doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("[ok]   OneDrive client", doctor.stdout)
        self.assertIn("[ok]   config.json", doctor.stdout)


class DaemonEndToEndTests(CliTestCase):
    def test_daemon_open_native_proxy_tab_close_and_status(self):
        setup = self.run_cli("setup")
        self.assertEqual(setup.returncode, 0, setup.stderr)
        daemon = self.start_daemon()
        socket_path = self.root / "run" / "office-online" / "control.sock"
        self.assertTrue(socket_path.is_socket())
        self.assertEqual(stat.S_IMODE(socket_path.stat().st_mode), 0o600)

        document = self.make_document(content="original")
        opened = self.run_cli("open", str(document), timeout=30)
        self.assertEqual(opened.returncode, 0, opened.stderr + opened.stdout)
        payload = json.loads(opened.stdout)
        self.assertTrue(payload["ok"])
        session = payload["sessions"][0]
        self.assertEqual(session["state"], "editing")
        self.assertTrue(session["url"].startswith("https://tenant.sharepoint.com/"))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.browser_log.exists():
            time.sleep(0.02)
        self.assertTrue(self.browser_log.exists())
        browser_url = self.browser_log.read_text(encoding="utf-8").strip()
        self.assertIn(f"chrome-extension://{EXTENSION_ID}/open.html?session=", browser_url)
        self.assertIn(session["session_id"], browser_url)

        # Chrome native-messaging proxy with exact 4-byte length framing.
        native = subprocess.Popen(
            [
                PYTHON,
                str(PROJECT / "office_online.py"),
                "native-host",
                f"chrome-extension://{EXTENSION_ID}/",
                "--parent-window=0",
            ],
            cwd=str(PROJECT),
            env=self.env,
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        bind_request = json.dumps(
            {
                "id": "native-2",
                "action": "bind",
                "session_id": session["session_id"],
                "tab_id": 101,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        frame = encode_frame(
            b'{"id":"native-1","action":"status"}'
        ) + encode_frame(bind_request)
        stdout, stderr = native.communicate(frame, timeout=10)
        self.assertEqual(native.returncode, 0, stderr.decode("utf-8", "replace"))
        buffer = bytearray(stdout)
        frames = pop_frames(buffer)
        self.assertEqual(len(frames), 2)
        self.assertEqual(buffer, bytearray())
        native_responses = [json.loads(item.decode("utf-8")) for item in frames]
        self.assertEqual(
            [response["id"] for response in native_responses], ["native-1", "native-2"]
        )
        for native_response in native_responses:
            self.assertTrue(native_response["ok"])
            self.assertEqual(
                native_response["sessions"][0]["session_id"], session["session_id"]
            )
        self.assertEqual(native_responses[1]["sessions"][0]["tab_id"], 101)

        # The native-host process exited with stdin EOF, closing the persistent
        # socket.  The daemon must treat that EOF as a final save for the bound
        # session even though no tab_closed request was delivered.
        saved = self.wait_for_session(session["session_id"], "saved", timeout=15)
        self.assertEqual(document.read_text(encoding="utf-8"), self.env["FAKE_DOWNLOAD_TEXT"])
        self.assertEqual(saved["result_path"], str(document.resolve()))
        backups = [
            path
            for path in (self.root / "data" / "office-online" / "backups").rglob("*")
            if path.is_file()
        ]
        self.assertTrue(backups)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "original")

        status = self.run_cli("status")
        self.assertEqual(status.returncode, 0)
        status_payload = json.loads(status.stdout)
        self.assertTrue(status_payload["ok"])
        self.assertEqual(status_payload["sessions"][0]["state"], "saved")

        self.assertIsNone(daemon.poll())


if __name__ == "__main__":
    unittest.main()
