"""The shell entry point forwards safely without installing anything real."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ShellInstallerTests(unittest.TestCase):
    def test_forwards_arguments_from_a_directory_with_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'checkout with spaces'
            root.mkdir()
            shutil.copyfile(ROOT / 'install.sh', root / 'install.sh')
            (root / 'install.py').write_text('fixture')
            fake_bin = Path(directory) / 'bin'
            fake_bin.mkdir()
            python = fake_bin / 'python3'
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            python.chmod(0o755)
            env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ.get('PATH', ''))
            result = subprocess.run(['/bin/sh', str(root / 'install.sh'), '--dry-run', '--prefix', 'home with spaces'], cwd='/', env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.splitlines(), [str(root / 'install.py'), '--dry-run', '--prefix', 'home with spaces'])

    def test_missing_checkout_fails_without_installing(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'install.sh'
            shutil.copyfile(ROOT / 'install.sh', script)
            result = subprocess.run(['/bin/sh', str(script)], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('complete project checkout', result.stderr)
