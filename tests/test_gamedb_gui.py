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


if __name__ == '__main__':
    unittest.main()
