#!/usr/bin/env python3
"""Tempdir-only tests for the Nautilus 4 provider, using a fake gi/Nautilus."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import nautilus_office_online as provider  # noqa: E402


class FakeInfo:
    def __init__(self, uri: str, directory: bool = False) -> None:
        self.uri = uri
        self.directory = directory

    def get_uri(self) -> str:
        return self.uri

    def is_directory(self) -> bool:
        return self.directory


@contextlib.contextmanager
def fake_nautilus_modules():
    """Install fake gi.repository modules long enough to load the provider."""

    class FakeGObject:
        pass

    class FakeMenuProvider:
        pass

    class FakeMenuItem:
        def __init__(self, name: str, label: str, tip: str | None = None) -> None:
            self.name = name
            self.label = label
            self.tip = tip
            self.handlers: dict[str, object] = {}

        def connect(self, signal: str, callback) -> None:
            self.handlers[signal] = callback

    fake_gi = types.ModuleType("gi")
    fake_gi.require_version = lambda _namespace, _version: None  # type: ignore[attr-defined]
    fake_repository = types.ModuleType("gi.repository")
    fake_repository.GObject = types.SimpleNamespace(GObject=FakeGObject)  # type: ignore[attr-defined]
    fake_repository.Nautilus = types.SimpleNamespace(  # type: ignore[attr-defined]
        MenuProvider=FakeMenuProvider,
        MenuItem=FakeMenuItem,
    )
    fake_gi.repository = fake_repository  # type: ignore[attr-defined]

    saved_gi = sys.modules.get("gi")
    saved_repository = sys.modules.get("gi.repository")
    sys.modules["gi"] = fake_gi
    sys.modules["gi.repository"] = fake_repository

    spec = importlib.util.spec_from_file_location(
        "_office_online_nautilus_fake_gi",
        ROOT / "nautilus_office_online.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        yield module, FakeMenuItem
    finally:
        if saved_gi is None:
            sys.modules.pop("gi", None)
        else:
            sys.modules["gi"] = saved_gi
        if saved_repository is None:
            sys.modules.pop("gi.repository", None)
        else:
            sys.modules["gi.repository"] = saved_repository


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="office-online-nautilus-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make_file(self, name: str) -> Path:
        path = self.root / name
        path.write_text("content", encoding="utf-8")
        return path

    def test_accepts_only_local_supported_regular_files(self) -> None:
        docx = self.make_file("report.docx")
        pptx = self.make_file("slides.PPTX")
        xlsx = self.make_file("budget.xlsx")
        paths = provider.selection_paths(
            [FakeInfo(docx.as_uri()), FakeInfo(pptx.as_uri()), FakeInfo(xlsx.as_uri())]
        )
        self.assertEqual(paths, [str(docx), str(pptx), str(xlsx)])

    def test_mixed_or_unsupported_selection_yields_no_menu(self) -> None:
        docx = self.make_file("report.docx")
        text = self.make_file("notes.txt")
        self.assertEqual(
            provider.selection_paths([FakeInfo(docx.as_uri()), FakeInfo(text.as_uri())]),
            [],
        )
        self.assertEqual(provider.selection_paths([]), [])

    def test_remote_and_non_file_uris_are_rejected(self) -> None:
        for uri in (
            "sftp://example.test/home/me/report.docx",
            "file://example.test/home/me/report.docx",
            "https://example.test/report.docx",
            "file:///home/me/report.txt",
        ):
            self.assertEqual(provider.selection_paths([FakeInfo(uri)]), [], uri)

    def test_directory_missing_file_and_percent_encoding(self) -> None:
        directory = self.root / "folder.docx"
        directory.mkdir()
        self.assertEqual(
            provider.selection_paths([FakeInfo(directory.as_uri(), directory=True)]),
            [],
        )
        missing = self.root / "missing.xlsx"
        self.assertEqual(provider.selection_paths([FakeInfo(missing.as_uri())]), [])

        spaced = self.make_file("quarterly report.xlsx")
        self.assertIn("%20", spaced.as_uri())
        self.assertEqual(
            provider.selection_paths([FakeInfo(spaced.as_uri())]),
            [str(spaced)],
        )


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="office-online-launch-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.file = self.root / "report.docx"
        self.file.write_text("content", encoding="utf-8")
        self.binary = self.root / "office-online"
        self.binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.binary.chmod(0o755)

    def test_launch_uses_argv_list_and_no_shell(self) -> None:
        with mock.patch.object(provider.subprocess, "Popen") as popen:
            opened = provider.launch_paths([str(self.file)], binary=self.binary)
        self.assertTrue(opened)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [str(self.binary), "open", "--gui", str(self.file)])
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["stdin"], provider.subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], provider.subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], provider.subprocess.DEVNULL)

    def test_launch_without_binary_or_paths_returns_false(self) -> None:
        self.assertFalse(provider.launch_paths([], binary=self.binary))
        self.assertFalse(provider.launch_paths([str(self.file)], binary=self.root / "nope"))

    def test_env_binary_is_used(self) -> None:
        with mock.patch.dict(
            os.environ, {"OFFICE_ONLINE_BIN": str(self.binary)}, clear=False
        ):
            self.assertEqual(provider.find_office_online(), self.binary)


class MenuProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="office-online-menu-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.binary = self.root / "office-online"
        self.binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.binary.chmod(0o755)
        self.file = self.root / "report.docx"
        self.file.write_text("content", encoding="utf-8")

    def test_menu_item_and_activation(self) -> None:
        with fake_nautilus_modules() as (module, _menu_item_class):
            with mock.patch.dict(
                os.environ,
                {"HOME": str(self.home), "OFFICE_ONLINE_BIN": str(self.binary)},
                clear=False,
            ):
                menu_provider = module.OfficeOnlineMenuProvider()
                items = menu_provider.get_file_items([FakeInfo(self.file.as_uri())])
                self.assertEqual(len(items), 1)
                item = items[0]
                self.assertEqual(item.name, "OfficeOnline::Open")
                self.assertEqual(item.label, "Open in Office Online")
                self.assertIn("activate", item.handlers)

                with mock.patch.object(module.subprocess, "Popen") as popen:
                    item.handlers["activate"](item)
                args, kwargs = popen.call_args
                self.assertEqual(
                    args[0], [str(self.binary), "open", "--gui", str(self.file)]
                )
                self.assertFalse(kwargs["shell"])

    def test_menu_hidden_for_mixed_selection_or_missing_binary(self) -> None:
        text = self.root / "notes.txt"
        text.write_text("content", encoding="utf-8")
        with fake_nautilus_modules() as (module, _menu_item_class):
            with mock.patch.dict(
                os.environ,
                {"HOME": str(self.home), "OFFICE_ONLINE_BIN": str(self.binary)},
                clear=False,
            ):
                menu_provider = module.OfficeOnlineMenuProvider()
                self.assertEqual(
                    menu_provider.get_file_items(
                        [
                            FakeInfo(self.file.as_uri()),
                            FakeInfo(text.as_uri()),
                        ]
                    ),
                    [],
                )

            with mock.patch.dict(
                os.environ,
                {"HOME": str(self.home), "PATH": ""},
                clear=False,
            ):
                os.environ.pop("OFFICE_ONLINE_BIN", None)
                menu_provider = module.OfficeOnlineMenuProvider()
                self.assertEqual(
                    menu_provider.get_file_items([FakeInfo(self.file.as_uri())]),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
