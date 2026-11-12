import tempfile
import unittest
from pathlib import Path
from office_online_backend.profile import ensure_quiet_profile


class QuietProfileTests(unittest.TestCase):
    def test_preserves_other_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'app-profile'
            directory.mkdir()
            path = directory / 'config'
            path.write_text('# keep this\nsync_dir = "/my/folder"\ndisable_notifications = "false"\ndisable_notifications = "false"\n')
            ensure_quiet_profile(directory)
            result = path.read_text()
            self.assertIn('# keep this\nsync_dir = "/my/folder"\n', result)
            self.assertEqual(result.count('disable_notifications ='), 1)
            self.assertIn('disable_notifications = "true"', result)
            self.assertIn('disable_version_check = "true"', result)
            self.assertIn('notify_file_actions = "false"', result)
            stamp = path.stat().st_mtime_ns
            ensure_quiet_profile(directory)
            self.assertEqual(path.stat().st_mtime_ns, stamp)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_creates_only_requested_profile(self):
        with tempfile.TemporaryDirectory() as root:
            normal = Path(root) / 'onedrive'
            normal.mkdir()
            (normal / 'config').write_text('unchanged')
            dedicated = Path(root) / 'office-online' / 'onedrive'
            ensure_quiet_profile(dedicated)
            self.assertEqual((normal / 'config').read_text(), 'unchanged')
            self.assertTrue((dedicated / 'config').is_file())
