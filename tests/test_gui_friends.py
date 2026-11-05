"""Settings › Accounts and keys › Steam friends (w-80), offscreen: the key is
a password field, nothing asks the CLI without a key, Refresh now runs
`--friends --refresh --json` in the background, the key never reaches the
status line, edits save to [steam]/[friends], and the search finds it."""

import os
import unittest
from unittest import mock

try:
    from test_gui_layout import nx
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx
from tests.patching import patch_gui  # noqa: E402

if nx is not None:
    from nexus.gui import settings as gsettings
    from PyQt6.QtWidgets import QLineEdit

KEY = 'ABCDEF0123456789ABCDEF0123456789'          # made up
ME = '76561190000000001'

ANSWER = {'enabled': True, 'me': ME, 'account': {'persona': 'Tester', 'source': 'steam'},
          'friends': [], 'by_app': {},
          'counts': {'total': 10, 'public': 7, 'private': 3, 'playing': 1},
          'updated': '2026-11-05T10:00:00+00:00', 'evidence': {}, 'error': None}


class StatusText(unittest.TestCase):
    @unittest.skipIf(nx is None, "PyQt6 not available")
    def test_counts_age_and_errors(self):
        from datetime import datetime
        now = datetime.fromisoformat(ANSWER['updated']).timestamp() + 125
        st = gsettings.friends_status_text
        self.assertEqual(st(ANSWER, now=now),
                         "10 friends · 7 public · 3 private · updated 2 min ago")
        self.assertEqual(st({**ANSWER, 'counts': {'total': 0, 'public': 0, 'private': 0}},
                            now=now), "No friends on this Steam account · updated 2 min ago")
        bad = {**ANSWER, 'error': {'kind': 'bad-key', 'message': "Steam refused the key."}}
        self.assertEqual(st(bad, now=now), "10 friends · 7 public · 3 private · updated "
                         "2 min ago")
        self.assertEqual(gsettings.friends_problem(bad), ("Steam refused the key.", None))
        slow = {**ANSWER, 'error': {'kind': 'rate-limited', 'message': "Wait."}}
        self.assertEqual(gsettings.friends_problem(slow), (None, "Wait."))
        self.assertEqual(gsettings.friends_problem(ANSWER), (None, None))
        self.assertEqual(st({**bad, 'updated': None, 'counts': {'total': 0}}),
                         "Nothing found yet.")
        self.assertEqual(st(None, 'timed out'), "Couldn't ask: timed out")
        self.assertEqual(st({**ANSWER, 'updated': None,
                             'counts': {'total': 0, 'public': 0, 'private': 0}}),
                         "Not asked yet: Refresh now asks Steam.")


@unittest.skipIf(nx is None, "PyQt6 not available")
class FriendsGroup(unittest.TestCase):
    def setUp(self):
        self.saved, self.calls = [], []
        self.cfg = {}
        from nexus.gui.config import load_config
        base = load_config()
        for p in (mock.patch.object(gsettings, 'save_config', self.saved.append),
                  patch_gui('load_config', lambda: {**base, **self.cfg}),
                  patch_gui('cli_json', self.fake_cli),
                  mock.patch.dict(os.environ, {'STEAM_WEB_API_KEY': ''})):
            p.start()
            self.addCleanup(p.stop)

    def fake_cli(self, args, callback, *a, **k):
        self.calls.append((list(args), callback))

    def friends_calls(self):
        return [args for args, _cb in self.calls if '--friends' in args]

    def page(self):
        page = nx.SettingsPage()
        self.addCleanup(page.deleteLater)
        page.load()
        return page

    def test_group_rows_and_a_password_field(self):
        page = self.page()
        self.assertIn('accounts.friends', page.groups)
        for key in ('friends', 'steam_web_api_key', 'friends_whose', 'friends_status',
                    'friends_privacy', 'friends_steamid'):
            self.assertIn(f'accounts.{key}', page.rows, key)
        self.assertEqual(page.fr_key.echoMode(), QLineEdit.EchoMode.Password)
        self.assertIn(page.rows['accounts.friends_steamid'], page.categories['accounts']
                      .adv_body.rows)                                   # under Advanced

    def test_without_a_key_nothing_is_asked(self):
        page = self.page()
        self.assertEqual(self.friends_calls(), [])
        self.assertFalse(page.fr_enabled.isEnabled())
        self.assertFalse(page.fr_refresh.isEnabled())
        self.assertIn("Add a key", page.fr_status_row.desc.text())
        page.fr_key.setText(KEY)                                        # typing one enables
        self.assertTrue(page.fr_enabled.isEnabled())
        self.assertTrue(page.fr_refresh.isEnabled())

    def test_status_comes_from_the_cli_and_never_shows_the_key(self):
        self.cfg = {'steam_web_api_key': KEY, 'friends_enabled': True}
        page = self.page()
        self.assertEqual(self.friends_calls(), [['--friends', '--json']])
        self.assertEqual(page.fr_key.text(), KEY)
        self.assertTrue(page.fr_enabled.isChecked())
        self.assertFalse(page.fr_refresh.isEnabled())                   # while it asks
        cb = [c for args, c in self.calls if '--friends' in args][-1]
        cb({**ANSWER, 'error': {'kind': 'bad-key', 'message': 'Steam refused the key.'}}, '')
        text = page.fr_status_row.desc.text()
        self.assertIn("10 friends · 7 public · 3 private", text)
        self.assertIn("Steam refused the key", page.fr_status_row.error.text())
        self.assertFalse(page.fr_status_row.error.isHidden())
        self.assertNotIn(KEY, text + page.fr_status_row.error.text())
        self.assertNotIn(KEY, page.fr_whose_row.desc.text())
        self.assertIn("Tester", page.fr_whose_row.desc.text())
        self.assertTrue(page.fr_refresh.isEnabled())
        page.fr_refresh.click()                                         # Refresh now
        self.assertEqual(self.friends_calls()[-1], ['--friends', '--refresh', '--json'])
        self.assertIn("Asking Steam", page.fr_status_row.desc.text())

    def test_edits_save_to_steam_and_friends(self):
        page = self.page()
        page.fr_key.setText('not a key')
        page.fr_key.editingFinished.emit()
        self.assertFalse(any(('steam', 'web_api_key') in u for u in self.saved))
        self.assertIn('32', page.fr_key_row.error.text())
        page.fr_key.setText(KEY)
        page.fr_key.editingFinished.emit()
        self.assertEqual(self.saved[-1].get(('steam', 'web_api_key')), KEY)
        self.cfg['steam_web_api_key'] = KEY
        page.cfg = {**page.cfg, 'steam_web_api_key': KEY}
        page.fr_enabled.setChecked(True)                                # a switch saves too
        self.assertIs(self.saved[-1].get(('friends', 'enabled')), True)
        page.fr_steamid.setText('123')
        page.fr_steamid.editingFinished.emit()
        self.assertFalse(any(('friends', 'steamid') in u for u in self.saved))
        page.fr_steamid.setText(ME)
        page.fr_steamid.editingFinished.emit()
        self.assertEqual(self.saved[-1].get(('friends', 'steamid')), ME)

    def test_env_key_locks_the_field(self):
        with mock.patch.dict(os.environ, {'STEAM_WEB_API_KEY': KEY}):
            page = self.page()
            self.assertEqual(page.fr_key.text(), "")
            self.assertFalse(page.fr_key.isEnabled())
            self.assertIn('STEAM_WEB_API_KEY', page.fr_key.placeholderText())
            self.assertTrue(page.fr_enabled.isEnabled())

    def test_settings_search_finds_it(self):
        page = self.page()
        for q in ('friends', 'who owns', 'web api', 'steamid64'):
            page.search.setText(q)
            self.assertFalse(page.categories['accounts'].isHidden(), q)
            self.assertTrue(page.categories['accounts'].filter(q.lower()), q)
        page.search.setText('friends')
        self.assertFalse(page.groups['accounts.friends'].isHidden())
        page.search.setText('')
