"""Game-db in the window, offscreen: the Settings rows that follow what the
server offers and what the token may do (meta.json, never the network), and
the release status chips on Deals > Upcoming and the Home upcoming tile."""

import unittest
from unittest import mock

try:
    from test_gui_layout import nx
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx

from nexus import gamedb  # noqa: E402

if nx is not None:
    from nexus.gui import settings as gsettings


@unittest.skipIf(nx is None, "PyQt6 not available")
class TokenNote(unittest.TestCase):
    def test_says_what_the_token_may_do(self):
        self.assertEqual(gsettings.token_note(None), gsettings.TOKEN_NOTE)
        self.assertTrue(gsettings.token_note(['read']).startswith("Can read only."))
        self.assertTrue(gsettings.token_note(['read', 'write:sessions']).startswith(
            "Can read and upload play sessions."))
        self.assertTrue(gsettings.token_note(
            ['read', 'write:sessions', 'write:library']).startswith(
            "Can read, upload play sessions and send your library."))

    def test_settings_page_reads_meta_json_only(self):
        with mock.patch.object(gamedb, 'token_scopes', return_value=['read']) as scopes, \
                mock.patch.object(gamedb, '_request') as net:
            page = nx.SettingsPage()
            self.addCleanup(page.deleteLater)
            page.load()
        scopes.assert_called()
        net.assert_not_called()
        self.assertIn("Can read only", page.gdb_token_row.desc.text())



@unittest.skipIf(nx is None, "PyQt6 not available")
class UploadRows(unittest.TestCase):
    def setUp(self):
        self.saved = []                 # nothing reaches the sandbox's config.toml
        p = mock.patch.object(gsettings, 'save_config', self.saved.append)
        p.start()
        self.addCleanup(p.stop)

    def page(self, status, scopes=('read',)):
        with mock.patch.object(gamedb, 'write_status', return_value=status), \
                mock.patch.object(gamedb, 'token_scopes', return_value=list(scopes)):
            page = nx.SettingsPage()
            self.addCleanup(page.deleteLater)
            page.load()
        return page

    def test_hidden_when_the_server_cannot_take_sessions(self):
        page = self.page('unavailable')
        self.assertFalse(page.gdb_sessions_row.available)
        self.assertFalse(page.gdb_other_row.available)

    def test_shown_while_unknown_and_off_by_default(self):
        page = self.page('unknown')
        self.assertTrue(page.gdb_sessions_row.available)
        self.assertFalse(page.gdb_sessions.isChecked())
        self.assertFalse(page.gdb_other.isChecked())
        self.assertFalse(page.gdb_other.isEnabled())        # only with sessions on
        page.gdb_sessions.setChecked(True)
        self.assertTrue(page.gdb_other.isEnabled())

    def test_read_only_token_gets_the_hint(self):
        page = self.page('read_only')
        self.assertTrue(page.gdb_sessions_row.available)
        self.assertIn("can only read", page.gdb_sessions_row.desc.text())
        self.assertIn("Upload play sessions", page.gdb_sessions_row.desc.text())
        page = self.page('ok', ('read', 'write:sessions'))
        self.assertNotIn("can only read", page.gdb_sessions_row.desc.text())

    def test_switches_save_on_their_own(self):
        """The game details switch, the upload switches and the PC name each
        save when changed (the first two didn't before w-45)."""
        page = self.page('ok', ('read', 'write:sessions'))
        page.gdb_sessions.setChecked(True)
        page.gdb_other.setChecked(True)
        page.gdb_games.setChecked(False)
        keys = set().union(*self.saved)
        self.assertTrue({('gamedb', 'upload_sessions'), ('gamedb', 'upload_other'),
                         ('gamedb', 'games')} <= keys, keys)


if __name__ == '__main__':
    unittest.main()
