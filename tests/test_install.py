#!/usr/bin/env python3
"""Mocked, tempdir-only tests for install.py and uninstall.py."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# The packaged execution environment can expose a non-Python sys.executable; tests invoke
# install.py with a real python3 from PATH.
PYTHON = Path(shutil.which("python3") or sys.executable).resolve()

import install  # noqa: E402
import uninstall  # noqa: E402

# Public key/ID pair from the documented Chrome extension-ID derivation.
KNOWN_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA8vj7SK0NZ6ak7K6m6KEA"
    "kfGaNfKUahqFFms6W8rq+voaW7nETrpsMqNyhmBQ+ea0KkyI/S5XIrDQPqDcNpves"
    "Ylg9lsmi7CQBZjJw7zNqKkvn0oYaP4SNtWZfZopBumqFbzFi5cst2PT+XU9CBitxX"
    "NtocRtcjOsa44W1gPA5xanmtlF258N6Nann+rSOAdhIWqSo/J6fj72cxTNfmqLkwA"
    "vhdS4Zyux4F87vxp4YTSwElfYXFsHZWi7h66uuuMzqyOyJz5grhCJ24rtTshMQUCxQ"
    "WyhO2XT2J1tVfUN1YVw6xdKUz3aGyKZeXCuql5klHmlqE9PTlbKj/1VMiIgCQIDAQAB"
)
KNOWN_ID = "mfabfdnimhipcapcioneheloaehhoggk"
MIME_TYPES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)


class InstallFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="office-online-install-test-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.source = self.base / "project"
        self.source.mkdir()
        # A tiny absolute-path backend fixture: prints the argv it received.
        (self.source / "office_online.py").write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "print(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        shutil.copyfile(
            ROOT / "nautilus_office_online.py",
            self.source / "nautilus_office_online.py",
        )
        self.extension = self.source / "extension"
        self.extension.mkdir()
        icon_dir = self.extension / "icons"
        icon_dir.mkdir()
        shutil.copyfile(
            ROOT / "extension" / "icons" / "icon.svg",
            icon_dir / "icon.svg",
        )
        self.manifest = {
            "manifest_version": 3,
            "name": "Office Online test extension",
            "version": "1.0",
            "key": KNOWN_KEY,
        }
        self.write_manifest()

        self.prefix = self.base / "fake-home"
        self.real_home = self.base / "real-home"
        self.fakebin = self.base / "fakebin"
        self.fakebin.mkdir()
        self.systemctl_log = self.base / "systemctl.log"
        fake_systemctl = self.fakebin / "systemctl"
        fake_systemctl.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        fake_systemctl.chmod(0o755)

    def write_manifest(self, manifest: dict | None = None) -> None:
        (self.extension / "manifest.json").write_text(
            json.dumps(self.manifest if manifest is None else manifest, indent=2) + "\n",
            encoding="utf-8",
        )

    def env(self, **overrides: str) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("XDG_CONFIG_HOME", None)
        env.pop("XDG_DATA_HOME", None)
        env["HOME"] = str(self.real_home)
        env["PATH"] = f"{self.fakebin}:/usr/bin:/bin"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["SYSTEMCTL_LOG"] = str(self.systemctl_log)
        env.update(overrides)
        return env

    def run_install(
        self,
        *args: str,
        use_prefix: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            str(PYTHON),
            str(ROOT / "install.py"),
            "--source",
            str(self.source),
        ]
        if use_prefix:
            command.extend(["--prefix", str(self.prefix)])
        command.extend(args)
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env or self.env(),
            text=True,
            capture_output=True,
            check=False,
        )

    def run_uninstall(
        self,
        *args: str,
        use_prefix: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(PYTHON), str(ROOT / "uninstall.py")]
        if use_prefix:
            command.extend(["--prefix", str(self.prefix)])
        command.extend(args)
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env or self.env(),
            text=True,
            capture_output=True,
            check=False,
        )

    def paths(self) -> install.InstallPaths:
        return install.resolve_paths(str(self.prefix))

    def installed_config(self) -> dict:
        return json.loads(self.paths().config_file.read_text(encoding="utf-8"))


class ExtensionIdTests(unittest.TestCase):
    def test_known_chrome_key_vector(self) -> None:
        self.assertEqual(install.compute_extension_id(KNOWN_KEY), KNOWN_ID)

    def test_manifest_without_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            with self.assertRaises(install.InstallError) as caught:
                install.extension_id_from_manifest(
                    {"manifest_version": 3}, manifest_path
                )
            self.assertIn("stable", str(caught.exception))

    def test_invalid_base64_is_rejected(self) -> None:
        with self.assertRaises(install.InstallError):
            install.compute_extension_id("not base64!!!")

    def test_extension_id_matches_known_algorithm(self) -> None:
        # The ID is the first 128 bits of SHA-256, mapped 0-f -> a-p.
        identifier = install.compute_extension_id(KNOWN_KEY)
        self.assertRegex(identifier, r"^[a-p]{32}$")
        self.assertEqual(identifier, KNOWN_ID)


class InstallTests(InstallFixture):
    def test_uploader_icon_marker_keeps_xml_declaration_first(self) -> None:
        rendered = install.render_uploader_icon(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg"/>\n'
        )
        self.assertTrue(rendered.startswith('<?xml version="1.0"'))
        declaration_end = rendered.index("?>") + 2
        marker_at = rendered.index(install.MANAGED_MARKER)
        self.assertGreater(marker_at, declaration_end)
        self.assertIn("<svg", rendered)

    def test_dry_run_writes_nothing_and_does_not_call_systemctl(self) -> None:
        result = self.run_install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run", result.stdout)
        self.assertIn(KNOWN_ID, result.stdout)
        self.assertIn("Would install", result.stdout)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.systemctl_log.exists())

    def test_install_layout_absolute_launchers_and_config(self) -> None:
        result = self.run_install("--no-service")
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = self.paths()

        expected_files = [
            paths.launcher,
            paths.native_launcher,
            paths.chrome_native_manifest,
            paths.chromium_native_manifest,
            paths.nautilus_extension,
            paths.desktop_file,
            paths.setup_desktop_file,
            paths.uploader_desktop_file,
            paths.uploader_icon_file,
            paths.config_file,
            paths.install_manifest,
        ]
        for path in expected_files:
            self.assertTrue(path.is_file(), path)

        self.assertEqual(stat.S_IMODE(paths.launcher.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(paths.native_launcher.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(paths.config_file.stat().st_mode), 0o600)
        self.assertFalse(paths.service_file.exists())
        profile = (paths.config_dir / "onedrive" / "config").read_text()
        self.assertIn(f'sync_dir = "{paths.data_dir / "drive"}"\n', profile)

        launcher_text = paths.launcher.read_text(encoding="utf-8")
        native_text = paths.native_launcher.read_text(encoding="utf-8")
        subprocess_python = shutil.which(
            "python3", path=f"{self.fakebin}:/usr/bin:/bin"
        )
        python = str(Path(subprocess_python or PYTHON).resolve())
        backend = str(self.source / "office_online.py")
        self.assertIn(python, launcher_text)
        self.assertIn(backend, launcher_text)
        self.assertIn(install.MANAGED_MARKER, launcher_text)
        self.assertIn(python, native_text)
        self.assertIn(backend, native_text)
        self.assertIn("native-host", native_text)

        ran = subprocess.run(
            [str(paths.launcher), "status"],
            text=True,
            capture_output=True,
            check=False,
            env=self.env(),
        )
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(json.loads(ran.stdout), ["status"])
        ran_native = subprocess.run(
            [str(paths.native_launcher)],
            text=True,
            capture_output=True,
            check=False,
            env=self.env(),
        )
        self.assertEqual(ran_native.returncode, 0, ran_native.stderr)
        self.assertEqual(json.loads(ran_native.stdout), ["native-host"])

        config = self.installed_config()
        self.assertEqual(config, {"extension_id": KNOWN_ID})

        for native_manifest_path in paths.native_manifests:
            payload = json.loads(native_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["name"], install.NATIVE_HOST_NAME)
            self.assertEqual(payload["type"], "stdio")
            self.assertIn(install.MANAGED_MARKER, payload["description"])
            self.assertEqual(
                payload["path"], str(paths.native_launcher)
            )
            self.assertEqual(
                payload["allowed_origins"],
                [f"chrome-extension://{KNOWN_ID}/"],
            )
            self.assertNotIn("*", json.dumps(payload["allowed_origins"]))

        desktop = paths.desktop_file.read_text(encoding="utf-8")
        self.assertIn(f"Exec={paths.launcher} open --gui %F", desktop)
        self.assertIn("TryExec=", desktop)
        self.assertIn("Actions=Setup;", desktop)
        self.assertIn("[Desktop Action Setup]", desktop)
        self.assertIn("office-online-setup", desktop)
        self.assertIn("X-OfficeOnline-Managed=true", desktop)
        action_block = desktop.split("[Desktop Action Setup]", 1)[1]
        # Terminal is not a valid [Desktop Action] key; the hidden setup entry
        # carries Terminal=true and is launched through gio/gtk-launch instead.
        self.assertNotIn("Terminal=true", action_block)
        for mime in MIME_TYPES:
            self.assertIn(mime, desktop)

        setup_desktop = paths.setup_desktop_file.read_text(encoding="utf-8")
        self.assertIn(f"Exec={paths.launcher} setup", setup_desktop)
        self.assertIn("Terminal=true", setup_desktop)
        self.assertIn("NoDisplay=true", setup_desktop)
        self.assertIn("X-OfficeOnline-Managed=true", setup_desktop)

        uploader_desktop = paths.uploader_desktop_file.read_text(encoding="utf-8")
        self.assertEqual(
            paths.uploader_desktop_file.name,
            f"{install.upload_ui.APPLICATION_ID}.desktop",
        )
        self.assertIn(f"Exec={paths.launcher} open --gui %F", uploader_desktop)
        self.assertIn(f"Icon={paths.uploader_icon_file}", uploader_desktop)
        icon_value = [
            line for line in uploader_desktop.splitlines() if line.startswith("Icon=")
        ][0].split("=", 1)[1]
        self.assertTrue(os.path.isabs(icon_value), icon_value)
        self.assertIn(
            "StartupWMClass=" + install.upload_ui.APPLICATION_ID,
            uploader_desktop,
        )
        self.assertIn("StartupNotify=true", uploader_desktop)
        self.assertIn("NoDisplay=true", uploader_desktop)
        self.assertIn("X-OfficeOnline-Managed=true", uploader_desktop)

        icon_name = f"{install.upload_ui.ICON_NAME}.svg"
        self.assertEqual(paths.uploader_icon_file.name, icon_name)
        self.assertEqual(
            paths.uploader_icon_file.parent,
            paths.data_home / "icons" / "hicolor" / "scalable" / "apps",
        )
        icon_text = paths.uploader_icon_file.read_text(encoding="utf-8")
        self.assertIn(install.MANAGED_MARKER, icon_text)
        self.assertIn("<svg", icon_text)
        # The exact extension artwork is reused, not a generated placeholder.
        self.assertIn("M38 24h36l20 20v58", icon_text)
        self.assertIn("#2563eb", icon_text)

        validator = shutil.which("desktop-file-validate")
        if validator:
            for desktop_path in (
                paths.desktop_file,
                paths.setup_desktop_file,
                paths.uploader_desktop_file,
            ):
                checked = subprocess.run(
                    [validator, str(desktop_path)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(checked.returncode, 0, checked.stderr)

        self.assertEqual(
            paths.nautilus_extension.read_text(encoding="utf-8"),
            (ROOT / "nautilus_office_online.py").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            paths.nautilus_extension.parent,
            paths.data_home / "nautilus-python" / "extensions",
        )

        manifest = json.loads(paths.install_manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["extension_id"], KNOWN_ID)
        self.assertFalse(manifest["service"])
        self.assertIn(str(paths.launcher), manifest["files"])
        self.assertIn(str(paths.uploader_desktop_file), manifest["files"])
        self.assertIn(str(paths.uploader_icon_file), manifest["files"])

    def test_config_merge_preserves_other_values(self) -> None:
        paths = self.paths()
        paths.config_dir.mkdir(parents=True)
        paths.config_file.write_text(
            json.dumps(
                {
                    "browser": "chromium",
                    "onedrive_binary": "/opt/onedrive",
                    "custom": {"keep": True},
                    "extension_id": "old",
                }
            ),
            encoding="utf-8",
        )
        paths.config_file.chmod(0o640)

        result = self.run_install("--no-service")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.installed_config()
        self.assertEqual(config["browser"], "chromium")
        self.assertEqual(config["onedrive_binary"], "/opt/onedrive")
        self.assertEqual(config["custom"], {"keep": True})
        self.assertEqual(config["extension_id"], KNOWN_ID)
        self.assertEqual(stat.S_IMODE(paths.config_file.stat().st_mode), 0o640)

    def test_xdg_paths_without_prefix(self) -> None:
        xdg_home = self.base / "xdg-home"
        xdg_config = self.base / "xdg-config"
        xdg_data = self.base / "xdg-data"
        env = self.env(
            HOME=str(xdg_home),
            XDG_CONFIG_HOME=str(xdg_config),
            XDG_DATA_HOME=str(xdg_data),
        )
        result = self.run_install("--no-service", use_prefix=False, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertTrue((xdg_home / ".local" / "bin" / "office-online").is_file())
        self.assertTrue(
            (
                xdg_config
                / "google-chrome"
                / "NativeMessagingHosts"
                / f"{install.NATIVE_HOST_NAME}.json"
            ).is_file()
        )
        self.assertTrue(
            (
                xdg_config
                / "chromium"
                / "NativeMessagingHosts"
                / f"{install.NATIVE_HOST_NAME}.json"
            ).is_file()
        )
        self.assertTrue((xdg_config / "office-online" / "config.json").is_file())
        profile = (xdg_config / "office-online" / "onedrive" / "config").read_text()
        self.assertIn(f'sync_dir = "{xdg_data / "office-online" / "drive"}"\n', profile)
        self.assertTrue(
            (
                xdg_data
                / "nautilus-python"
                / "extensions"
                / "nautilus_office_online.py"
            ).is_file()
        )
        self.assertFalse((xdg_home / ".config").exists())

    def test_test_home_alias(self) -> None:
        command = [
            str(PYTHON),
            str(ROOT / "install.py"),
            "--source",
            str(self.source),
            "--test-home",
            str(self.prefix),
            "--no-service",
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=self.env(),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.paths().launcher.is_file())

    def test_collision_refused_without_force_and_force_replaces(self) -> None:
        paths = self.paths()
        paths.bin_dir.mkdir(parents=True)
        paths.launcher.write_text("#!/bin/sh\necho user file\n", encoding="utf-8")
        paths.launcher.chmod(0o755)

        result = self.run_install("--no-service")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual(
            paths.launcher.read_text(encoding="utf-8"),
            "#!/bin/sh\necho user file\n",
        )
        self.assertFalse(paths.config_file.exists())
        self.assertFalse(paths.chrome_native_manifest.exists())

        result = self.run_install("--no-service", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(install.MANAGED_MARKER, paths.launcher.read_text(encoding="utf-8"))

    def test_systemd_unit_and_enable_start_commands(self) -> None:
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = self.paths()
        unit = paths.service_file.read_text(encoding="utf-8")
        self.assertIn(f"ExecStart={paths.launcher} daemon", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn(install.MANAGED_MARKER, unit)
        self.assertNotIn("enable-linger", unit)
        log = self.systemctl_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            log,
            [
                "--user daemon-reload",
                f"--user enable --now {install.SERVICE_NAME}",
            ],
        )
        manifest = json.loads(paths.install_manifest.read_text(encoding="utf-8"))
        self.assertTrue(manifest["service"])

    def test_missing_manifest_key_fails_before_writing(self) -> None:
        self.manifest.pop("key")
        self.write_manifest()
        result = self.run_install("--no-service")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stable", result.stderr)
        self.assertFalse(self.prefix.exists())

    def test_missing_extension_icon_fails_before_writing(self) -> None:
        (self.extension / "icons" / "icon.svg").unlink()
        result = self.run_install("--no-service")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("extension artwork", result.stderr)
        self.assertFalse(self.prefix.exists())

    def test_prefix_with_spaces_quotes_wrappers(self) -> None:
        spaced_prefix = self.base / "home with spaces"
        command = [
            str(PYTHON),
            str(ROOT / "install.py"),
            "--source",
            str(self.source),
            "--prefix",
            str(spaced_prefix),
            "--no-service",
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=self.env(),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        launcher = spaced_prefix / ".local" / "bin" / "office-online"
        self.assertIn('"', launcher.read_text(encoding="utf-8"))
        ran = subprocess.run(
            [str(launcher), "doctor"],
            text=True,
            capture_output=True,
            check=False,
            env=self.env(),
        )
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(json.loads(ran.stdout), ["doctor"])

    def test_unowned_install_manifest_refused_before_writes(self) -> None:
        paths = self.paths()
        paths.data_dir.mkdir(parents=True)
        paths.install_manifest.write_text("{\"user\": true}\n", encoding="utf-8")
        result = self.run_install("--no-service")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertFalse(paths.launcher.exists())
        self.assertEqual(
            paths.install_manifest.read_text(encoding="utf-8"),
            "{\"user\": true}\n",
        )


class UninstallTests(InstallFixture):
    def install(self, *args: str) -> None:
        result = self.run_install("--no-service", *args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uninstall_removes_owned_files_but_keeps_data_auth_backups(self) -> None:
        self.install()
        paths = self.paths()
        # Simulate user data, dedicated auth, backups, and unrelated files.
        auth = paths.config_dir / "onedrive"
        auth.mkdir(exist_ok=True)
        (auth / "oauth.cache").write_text("token", encoding="utf-8")
        (paths.data_dir / "sessions.json").write_text("[]", encoding="utf-8")
        backups = paths.data_dir / "backups"
        backups.mkdir()
        (backups / "report.docx.bak").write_text("backup", encoding="utf-8")
        unrelated_bin = paths.bin_dir / "someone-else"
        unrelated_bin.write_text("keep", encoding="utf-8")
        unrelated_native = paths.chromium_native_dir / "other.host.json"
        unrelated_native.write_text("{}", encoding="utf-8")
        unrelated_desktop = paths.applications_dir / "other.desktop"
        unrelated_desktop.write_text("[Desktop Entry]\n", encoding="utf-8")
        unrelated_icon = (
            paths.data_home
            / "icons"
            / "hicolor"
            / "scalable"
            / "apps"
            / "someone-else.svg"
        )
        unrelated_icon.write_text("<svg/>\n", encoding="utf-8")

        result = self.run_uninstall("--no-service")
        self.assertEqual(result.returncode, 0, result.stderr)

        for path in (
            paths.launcher,
            paths.native_launcher,
            paths.chrome_native_manifest,
            paths.chromium_native_manifest,
            paths.nautilus_extension,
            paths.desktop_file,
            paths.setup_desktop_file,
            paths.uploader_desktop_file,
            paths.uploader_icon_file,
            paths.install_manifest,
        ):
            self.assertFalse(path.exists(), path)

        self.assertTrue(paths.config_file.exists())
        self.assertTrue((auth / "oauth.cache").is_file())
        self.assertTrue((paths.data_dir / "sessions.json").is_file())
        self.assertTrue((backups / "report.docx.bak").is_file())
        self.assertTrue(unrelated_bin.is_file())
        self.assertTrue(unrelated_native.is_file())
        self.assertTrue(unrelated_desktop.is_file())
        self.assertTrue(unrelated_icon.is_file())
        self.assertIn("Kept", result.stdout)

    def test_uninstall_with_service_stops_and_removes_unit(self) -> None:
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = self.paths()
        self.assertTrue(paths.service_file.is_file())
        self.systemctl_log.write_text("", encoding="utf-8")

        result = self.run_uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(paths.service_file.exists())
        self.assertTrue(paths.config_file.exists())
        log = self.systemctl_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            log,
            [
                f"--user disable --now {install.SERVICE_NAME}",
                "--user daemon-reload",
            ],
        )

    def test_uninstall_dry_run_changes_nothing(self) -> None:
        self.install()
        paths = self.paths()
        self.systemctl_log.unlink(missing_ok=True)
        result = self.run_uninstall("--dry-run", "--no-service")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Would remove", result.stdout)
        self.assertTrue(paths.launcher.is_file())
        self.assertTrue(paths.native_launcher.is_file())
        self.assertTrue(paths.nautilus_extension.is_file())
        self.assertTrue(paths.config_file.is_file())
        self.assertFalse(self.systemctl_log.exists())

    def test_uninstall_leaves_unowned_target(self) -> None:
        self.install()
        paths = self.paths()
        paths.launcher.write_text("#!/bin/sh\necho user\n", encoding="utf-8")
        result = self.run_uninstall("--no-service")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(paths.launcher.is_file())
        self.assertIn("Left untouched", result.stdout)
        self.assertFalse(paths.native_launcher.exists())

    def test_uninstall_leaves_unowned_uploader_icon(self) -> None:
        self.install()
        paths = self.paths()
        paths.uploader_icon_file.write_text(
            "<svg xmlns=\"http://www.w3.org/2000/svg\"/>\n", encoding="utf-8"
        )
        result = self.run_uninstall("--no-service")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(paths.uploader_icon_file.is_file())
        self.assertIn("Left untouched", result.stdout)
        self.assertFalse(paths.uploader_desktop_file.exists())
        self.assertFalse(paths.desktop_file.exists())

    def test_no_service_on_uninstall_removes_unit_without_systemctl(self) -> None:
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.systemctl_log.write_text("", encoding="utf-8")
        result = self.run_uninstall("--no-service")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.paths().service_file.exists())
        self.assertEqual(self.systemctl_log.read_text(encoding="utf-8"), "")

    def test_purge_removes_config_auth_data_and_backups(self) -> None:
        self.install()
        paths = self.paths()
        auth = paths.config_dir / "onedrive"
        auth.mkdir(exist_ok=True)
        (auth / "oauth.cache").write_text("token", encoding="utf-8")
        backup = paths.data_dir / "backups"
        backup.mkdir()
        (backup / "file.bak").write_text("backup", encoding="utf-8")

        result = self.run_uninstall("--no-service", "--purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(paths.config_dir.exists())
        self.assertFalse(paths.data_dir.exists())


if __name__ == "__main__":
    unittest.main()
