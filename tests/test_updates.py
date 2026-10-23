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
from nexus import net, updates  # noqa: E402

RELEASE = json.dumps({'tag_name': 'v9.1.0',
                      'html_url': 'https://github.com/cyberpunk89/nexus-launcher/releases/tag/v9.1.0'
                      }).encode()


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

    def test_no_token_no_request(self):
        with mock.patch.object(net, 'get') as get:
            self.assertIsNone(updates.available('0.3.0'))
            self.conf.write_text('[updates]\ngithub_token = "  "\n')
            self.assertIsNone(updates.available('0.3.0'))
        get.assert_not_called()

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


if __name__ == '__main__':
    unittest.main()
