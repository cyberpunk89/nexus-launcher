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
        with mock.patch.object(updates, 'last_checked', return_value=None):   # no check yet
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


class WhatsNew(unittest.TestCase):
    """The short notes shown once after an update (w-28)."""

    def notes(self, data):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        f = Path(d.name) / 'whats_new.json'
        f.write_text(data if isinstance(data, str) else json.dumps(data))
        p = mock.patch.object(updates, 'NOTES', f)
        p.start()
        self.addCleanup(p.stop)

    def test_bundled_file_is_valid(self):
        data = json.loads(updates.NOTES.read_text(encoding='utf-8'))
        self.assertIsInstance(data, dict)
        from nexus.icons import ICONS
        for ver, notes in data.items():
            self.assertTrue(updates.parse(ver), ver)
            self.assertTrue(notes, ver)
            for n in notes:
                if isinstance(n, str):
                    continue
                self.assertTrue(n.get('text'), (ver, n))
                self.assertLessEqual(len(n['text']), updates.NOTE_LEN, (ver, n))
                self.assertLessEqual(len(n.get('title', '')), updates.TITLE_LEN, (ver, n))
                self.assertIn(n.get('icon', 'news'), ICONS, (ver, n))
                self.assertIn(n.get('kind', 'new'), updates.KINDS, (ver, n))
                self.assertIn(n.get('platform', 'linux'), ('linux', 'windows'), (ver, n))
            for here in ('linux', 'windows'):                # nothing cut off on either
                shown = [n for n in notes if isinstance(n, str) or n.get('platform', here) == here]
                self.assertLessEqual(len(shown), updates.NOTES_MAX, (ver, here))

    def test_lines_are_short_plain_and_capped(self):
        self.notes({'1.2.0': ["  Faster   start ", "", 7, "x" * 300, *[f"n{i}" for i in range(9)]]})
        got = updates.whats_new('1.2.0')
        self.assertEqual(got[0], {'text': "Faster start", 'title': '', 'icon': None, 'kind': None})
        self.assertEqual(len(got[1]['text']), updates.NOTE_LEN)
        self.assertTrue(got[1]['text'].endswith('…'))
        self.assertEqual(len(got), updates.NOTES_MAX)
        self.assertEqual(updates.whats_new('9.9.9'), [])

    def test_cards_keep_known_icons_and_kinds_only(self):
        self.notes({'1.2.0': [
            {'kind': 'new', 'icon': 'news', 'title': "  Pick   sites ", 'text': "Ten of them."},
            {'kind': 'shiny', 'icon': 'no-such-icon', 'title': "t" * 99, 'text': "Odd one."},
            {'kind': 'fixed', 'title': "No text"},
            "A plain line"]})
        got = updates.whats_new('1.2.0')
        self.assertEqual(got[0], {'text': "Ten of them.", 'title': "Pick sites",
                                  'icon': 'news', 'kind': 'new'})
        self.assertEqual((got[1]['icon'], got[1]['kind']), (None, None))
        self.assertEqual(len(got[1]['title']), updates.TITLE_LEN)
        self.assertEqual(got[2], {'text': "A plain line", 'title': '', 'icon': None, 'kind': None})
        self.assertEqual(len(got), 3)                         # the one without text is dropped

    def test_platform_entries_show_on_their_platform_only(self):
        self.notes({'1.2.0': [{'text': "Both"}, {'text': "Linux", 'platform': 'linux'},
                              {'text': "Windows", 'platform': 'windows'},
                              {'text': "Mac", 'platform': 'mac'}]})
        texts = lambda: [n['text'] for n in updates.whats_new('1.2.0')]   # noqa: E731
        with mock.patch.object(features, 'FEATURES', features.ALL):
            self.assertEqual(texts(), ["Both", "Linux"])
        with mock.patch.object(features, 'FEATURES', features.WINDOWS):
            self.assertEqual(texts(), ["Both", "Windows"])

    def test_platform_filter_runs_before_the_cap(self):
        self.notes({'1.2.0': [*({'text': f"w{i}", 'platform': 'windows'} for i in range(8)),
                              {'text': "linux one", 'platform': 'linux'}]})
        with mock.patch.object(features, 'FEATURES', features.ALL):
            self.assertEqual([n['text'] for n in updates.whats_new('1.2.0')], ["linux one"])

    def test_missing_or_broken_file_means_no_notes(self):
        for bad in ('not json', '[1, 2]'):
            self.notes(bad)
            self.assertEqual(updates.whats_new('1.2.0'), [])

    def test_shown_on_the_first_start_of_a_newer_version_only(self):
        S = updates.show_whats_new
        self.assertTrue(S('0.4.0', '0.4.1', True))
        self.assertFalse(S('0.4.1', '0.4.1', True))
        self.assertFalse(S('0.5.0', '0.4.1', True))          # went back a version
        self.assertTrue(S(None, '0.4.1', True))              # updated from before this
        self.assertFalse(S(None, '0.4.1', False))            # a fresh install
        self.assertFalse(S('junk', '0.4.1', True))

    def test_window_remembers_the_version_and_shows_the_sheet_once(self):
        from nexus.gui import window
        win = mock.Mock(_first_run=False)
        win.prefs.last_seen_version = '0.0.1'
        shots = []
        with mock.patch.object(window.updates, 'whats_new', return_value=[{'text': "A"}]), \
                mock.patch.dict(os.environ, {'NEXUS_NO_SETUP': ''}), \
                mock.patch.object(window.QTimer, 'singleShot', lambda _ms, fn: shots.append(fn)):
            window.MainWindow._note_version(win, True)
            self.assertEqual(win.prefs.last_seen_version, window.__version__)
            win.prefs.save.assert_called_once()
            self.assertEqual(len(shots), 1)
            window.MainWindow._note_version(win, True)        # same version again: nothing
            self.assertEqual(len(shots), 1)
            win.prefs.last_seen_version = None                # fresh install
            window.MainWindow._note_version(win, False)
            self.assertEqual(len(shots), 1)

    def test_sheet_lists_the_notes_and_links_the_release(self):
        from nexus.gui import window
        win = mock.Mock()
        opened = []
        notes = [{'text': "One"}, {'text': "Two"}]
        with mock.patch.object(window.updates, 'whats_new', return_value=notes), \
                mock.patch.object(window.QDesktopServices, 'openUrl', opened.append):
            window.MainWindow.show_whats_new(win)
            title, entries, items = win.sheet.notes.call_args.args
            self.assertIn(window.__version__, title)
            self.assertEqual(entries, notes)
            self.assertTrue(win.sheet.notes.call_args.kwargs['empty'])
            self.assertEqual([i[0] for i in items], ["Got it", "Full release notes"])
            items[1][2]()
        self.assertTrue(opened[0].toString().endswith(f"/releases/tag/v{window.__version__}"))


if __name__ == '__main__':
    unittest.main()
