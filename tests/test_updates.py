"""Tests for nexus/updates.py: the daily "newer release" check (HTTP mocked)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import features, net, updates  # noqa: E402
from tests.patching import patch_gui  # noqa: E402

PAGE = 'https://github.com/cyberpunk89/nexus-launcher/releases/tag/v9.1.0'
SETUP = 'https://github.com/cyberpunk89/nexus-launcher/releases/download/v9.1.0/nexus-launcher-Setup-9.1.0.exe'
RELEASE = json.dumps({'tag_name': 'v9.1.0', 'html_url': PAGE}).encode()
WITH_ASSETS = json.dumps({'tag_name': 'v9.1.0', 'html_url': PAGE, 'assets': [
    {'name': 'nexus-launcher-9.1.0-portable.zip',
     'browser_download_url': SETUP.replace('Setup-9.1.0.exe', '9.1.0-portable.zip')},
    {'name': 'nexus-launcher-Setup-9.1.0.exe', 'browser_download_url': SETUP}]}).encode()


class Updates(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='nexus-upd-'))
        self.addCleanup(__import__('shutil').rmtree, self.tmp, True)
        self.conf = self.tmp / 'config.toml'
        for name, val in (('CONFIG_PATH', self.conf), ('CACHE', self.tmp / 'update-check.json')):
            p = mock.patch.object(updates, name, val)
            p.start()
            self.addCleanup(p.stop)

    def token(self, t='ghp_secret'):
        self.conf.write_text(f'[updates]\ngithub_token = "{t}"\n')

    def test_parse(self):
        self.assertEqual(updates.parse('v1.2.3'), (1, 2, 3))
        self.assertEqual(updates.parse('0.10.0-rc1'), (0, 10, 0))
        self.assertEqual(updates.parse('nightly'), ())
        self.assertGreater(updates.parse('0.10.0'), updates.parse('0.9.9'))

    def test_no_token_asks_anonymously(self):
        """The repo is public: no token needed, and none is sent."""
        with mock.patch.object(net, 'get', return_value=RELEASE) as get:
            self.assertEqual(updates.available('0.3.0')['version'], '9.1.0')
        self.assertEqual(get.call_args.kwargs['headers'], {})

    def test_turned_off_no_request(self):
        with mock.patch.object(net, 'get') as get:
            self.conf.write_text('[updates]\ncheck = false\ngithub_token = "ghp_secret"\n')
            self.assertIsNone(updates.available('0.3.0'))
        get.assert_not_called()
        self.assertFalse(updates.CACHE.exists())

    def test_newer_release_found_with_token_sent(self):
        self.token()
        with mock.patch.object(net, 'get', return_value=RELEASE) as get:
            rel = updates.available('0.3.0')
        self.assertEqual(rel['version'], '9.1.0')
        self.assertTrue(rel['url'].endswith('/v9.1.0'))
        self.assertEqual(get.call_args.kwargs['headers'], {'Authorization': 'Bearer ghp_secret'})
        self.assertNotIn('ghp_secret', updates.CACHE.read_text())

    def test_same_or_older_shows_nothing(self):
        self.token()
        with mock.patch.object(net, 'get', return_value=RELEASE):
            self.assertIsNone(updates.available('9.1.0'))
            self.assertIsNone(updates.available('10.0.0'))

    def test_checked_once_a_day(self):
        self.token()
        with mock.patch.object(net, 'get', return_value=RELEASE) as get:
            updates.available('0.3.0')
            again = updates.available('0.3.0')
        self.assertEqual(get.call_count, 1)             # second answer came from the cache
        self.assertEqual(again['version'], '9.1.0')

    def test_failure_is_quiet_and_not_retried_at_once(self):
        self.token()
        with mock.patch.object(net, 'get', side_effect=net.NetError('offline', 401)) as get:
            self.assertIsNone(updates.available('0.3.0'))
            self.assertIsNone(updates.available('0.3.0'))
        self.assertEqual(get.call_count, 1)

    def test_garbage_response(self):
        self.token()
        with mock.patch.object(net, 'get', return_value=b'{"message": "Not Found"}'):
            self.assertIsNone(updates.available('0.3.0'))

    def test_non_https_link_refused(self):
        self.token()
        bad = json.dumps({'tag_name': 'v9.0.0', 'html_url': 'javascript:alert(1)'}).encode()
        with mock.patch.object(net, 'get', return_value=bad):
            self.assertIsNone(updates.available('0.3.0'))

    def test_force_skips_the_daily_cache(self):
        with mock.patch.object(net, 'get', return_value=RELEASE) as get:
            updates.available('0.3.0')
            updates.available('0.3.0')
            self.assertEqual(get.call_count, 1)
            self.assertEqual(updates.available('0.3.0', force=True)['version'], '9.1.0')
        self.assertEqual(get.call_count, 2)

    def test_forced_check_that_fails_says_so(self):
        """An old cached answer must not pass for a fresh one."""
        with mock.patch.object(net, 'get', return_value=RELEASE):
            self.assertEqual(updates.check('0.3.0')['state'], 'available')
        with mock.patch.object(net, 'get', side_effect=net.NetError('offline')):
            self.assertEqual(updates.check('0.3.0', force=True), {'state': 'failed'})
            self.assertEqual(updates.check('0.3.0')['state'], 'available')   # cache still serves

    def test_states_and_last_checked(self):
        self.assertIsNone(updates.last_checked())
        with mock.patch.object(net, 'get', return_value=RELEASE):
            self.assertEqual(updates.check('9.1.0'), {'state': 'current', 'version': '9.1.0'})
        self.assertIsNotNone(updates.last_checked())
        self.conf.write_text('[updates]\ncheck = false\n')
        self.assertEqual(updates.check('0.3.0', force=True), {'state': 'off'})

    def test_windows_links_the_installer_linux_the_release_page(self):
        with mock.patch.object(net, 'get', return_value=WITH_ASSETS):
            with mock.patch.object(features, 'FEATURES', features.WINDOWS):
                self.assertEqual(updates.available('0.3.0')['url'], SETUP)
            with mock.patch.object(features, 'FEATURES', features.ALL):
                self.assertEqual(updates.available('0.3.0')['url'], PAGE)

    def test_windows_without_an_installer_asset_falls_back_to_the_page(self):
        with mock.patch.object(net, 'get', return_value=RELEASE), \
                mock.patch.object(features, 'FEATURES', features.WINDOWS):
            self.assertEqual(updates.available('0.3.0')['url'], PAGE)

    def test_non_https_installer_ignored(self):
        bad = json.dumps({'tag_name': 'v9.1.0', 'html_url': PAGE, 'assets': [
            {'name': 'x-Setup-9.1.0.exe', 'browser_download_url': 'http://x.test/a.exe'}]}).encode()
        with mock.patch.object(net, 'get', return_value=bad), \
                mock.patch.object(features, 'FEATURES', features.WINDOWS):
            self.assertEqual(updates.available('0.3.0')['url'], PAGE)


@unittest.skipUnless(os.environ.get('QT_QPA_PLATFORM') == 'offscreen',
                     "GUI test: needs QT_QPA_PLATFORM=offscreen")
class Window(unittest.TestCase):
    def test_toast_links_the_release(self):
        from nexus.gui import window
        win = mock.Mock()
        opened = []
        with mock.patch.object(window.QDesktopServices, 'openUrl', opened.append):
            window.MainWindow._update_checked(win, {'version': '9.1.0', 'url': 'https://x.test/r'}, None)
            text, _kind, actions, _t = win.toast.call_args.args
            self.assertEqual(text, "Version 9.1.0 is available.")
            actions[0][1]()
        self.assertEqual(opened[0].toString(), 'https://x.test/r')
        win.toast.reset_mock()
        window.MainWindow._update_checked(win, None, None)
        win.toast.assert_not_called()

    def page(self, result, calls=None):
        """A SettingsPage whose background work runs at once with `result`
        (or is only recorded in `calls`)."""
        import tests.test_gui_layout  # noqa: F401  (makes the QApplication)
        from nexus.gui import settings
        p = patch_gui('in_thread', (lambda fn, cb: calls.append((fn, cb))) if calls is not None
                      else (lambda fn, cb: cb(result, None)))
        p.start()
        self.addCleanup(p.stop)
        page = settings.SettingsPage()
        self.addCleanup(page.deleteLater)
        return page

    def test_settings_row_shows_version_and_result(self):
        from nexus import __version__
        page = self.page({'state': 'available', 'version': '9.1.0', 'url': SETUP})
        self.assertIn(f"Nexus {__version__}", page.version_row.desc.text())
        self.assertIn("last checked never", page.version_row.desc.text())
        with mock.patch.object(updates, 'check', return_value={
                'state': 'available', 'version': '9.1.0', 'url': SETUP}) as check:
            page.upd_btn.click()
        check.assert_not_called()                       # runs inside the (faked) thread
        self.assertTrue(page.upd_btn.isEnabled())
        text = page.upd_result.text()
        self.assertIn("Nexus 9.1.0 is available", text)
        self.assertIn(f'href="{SETUP}"', text)

    def test_settings_row_other_results(self):
        for state, want in (('current', "up to date"), ('failed', "Couldn't check"),
                            ('off', "check = false")):
            page = self.page({'state': state, 'version': '0.3.1'})
            page.upd_btn.click()
            self.assertIn(want, page.upd_result.text())

    def test_settings_check_forces_and_shows_checking(self):
        from nexus.gui import settings
        calls = []
        page = self.page(None, calls)
        page.upd_btn.click()
        self.assertEqual(page.upd_result.text(), "Checking…")
        self.assertFalse(page.upd_btn.isEnabled())
        with mock.patch.object(settings.updates, 'check', return_value={'state': 'off'}) as check:
            calls[0][0]()
        check.assert_called_once_with(force=True)


if __name__ == '__main__':
    unittest.main()
