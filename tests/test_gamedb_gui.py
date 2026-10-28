"""Game-db in the window, offscreen: the Settings rows that follow what the
server offers and what the token may do (meta.json, never the network), and
the release status chips on Deals > Upcoming and the Home upcoming tile."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

try:
    from test_gui_layout import nx
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx

from nexus import gamedb  # noqa: E402

if nx is not None:
    from nexus.gui import dashboard, deals, settings as gsettings
    from PyQt6.QtWidgets import QLabel

NOW = datetime.now(timezone.utc)


def day(offset: int) -> str:
    return (NOW + timedelta(days=offset)).strftime('%Y-%m-%dT00:00:00.000Z')


def text_of(d: str) -> str:
    """Steam's text for a concrete day ('5 Mar, 2027')."""
    t = datetime.fromisoformat(d.replace('Z', '+00:00'))
    return f"{t.day} {t.strftime('%b')}, {t.year}"


def upcoming_row(appid, offset, **kw):
    rel = day(offset)
    return {'appid': appid, 'title': f'Game {appid}', 'headerImage': None,
            'releaseDate': rel, 'releaseDateText': text_of(rel), 'countdown': None,
            'owned': False, 'protonTier': None,
            'steamUrl': f'https://store.steampowered.com/app/{appid}', **kw}


# The same rows from a Game-db with the additive status fields, and from an
# older one without them.
NEW_SERVER = {'schema': 1, 'wishlist': [], 'upcoming': [
    upcoming_row(1, 30, status='early_access', previousDate=None, pcDate=None),
    upcoming_row(2, 60, status=None, previousDate=day(10), pcDate=day(90)),
    upcoming_row(3, 20, status='cancelled', previousDate=None, pcDate=None),
    upcoming_row(4, -3, status=None, previousDate=None, pcDate=None),
    upcoming_row(5, 40, status=None, previousDate=None, pcDate=None)]}
OLD_SERVER = {'schema': 1, 'wishlist': [], 'upcoming': [
    {k: v for k, v in r.items() if k not in gamedb.STATUS_FIELDS}
    for r in NEW_SERVER['upcoming']]}


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



@unittest.skipIf(nx is None, "PyQt6 not available")
class ReleaseChips(unittest.TestCase):
    """w-44: release status chips on Deals > Upcoming and the Home tile."""

    def chips(self, data):
        return {g['appid']: [t for t, _r in gamedb.release_chips(g)]
                for g in gamedb.upcoming(data)}

    def test_each_status_and_nothing_from_an_old_server(self):
        got = self.chips(NEW_SERVER)
        pc = datetime.fromisoformat(day(90).replace('Z', '+00:00'))
        self.assertEqual(got[1], ["Early access"])
        self.assertEqual(got[2][0], "Delayed")
        self.assertTrue(got[2][1].startswith(f"PC {pc.day} {pc.strftime('%b')}"))
        self.assertEqual(got[3], ["Cancelled"])
        self.assertEqual(got[4], ["Out now"])
        self.assertEqual(got[5], [])
        self.assertEqual(self.chips(OLD_SERVER), {1: [], 2: [], 3: [], 4: [], 5: []})

    def test_out_now_only_for_14_days_and_not_before_the_pc_date(self):
        old = {'schema': 1, 'upcoming': [upcoming_row(9, -20, status=None)]}
        self.assertEqual(self.chips(old)[9], [])
        later = {'schema': 1, 'upcoming': [upcoming_row(9, -2, pcDate=day(30))]}
        self.assertNotIn("Out now", self.chips(later)[9])
        vague = upcoming_row(9, -2, status=None)
        vague['releaseDateText'] = 'Q4 2026'
        self.assertNotIn("Out now", self.chips({'upcoming': [vague]})[9])

    def cards(self, data):
        out = {}
        for g in gamedb.upcoming(data):
            card = deals.DealCard(g, 'upcoming', None, set())
            self.addCleanup(card.deleteLater)
            out[g['appid']] = card
        return out

    def texts(self, w):
        return {lab.text() for lab in w.findChildren(QLabel) if not lab.isHidden()}

    def test_deal_cards(self):
        cards = self.cards(NEW_SERVER)
        self.assertIn("Early access", self.texts(cards[1]))
        self.assertTrue({"Delayed"} <= self.texts(cards[2]))
        self.assertIn("Cancelled", self.texts(cards[3]))
        self.assertTrue(cards[3].property('dimmed'))
        self.assertFalse(cards[1].property('dimmed'))
        self.assertIn("Out now", self.texts(cards[4]))
        for card in self.cards(OLD_SERVER).values():
            t = self.texts(card)
            self.assertFalse(t & {"Early access", "Delayed", "Cancelled", "Out now"}, t)
            self.assertFalse(card.property('dimmed'))

    def home_tile(self, data, size='full'):
        page = mock.Mock(_loaded=True, error=None)
        page._data.return_value = data
        page._base.return_value = None
        hub = mock.Mock(config={'gamedb_enabled': True}, dealspage=page)
        w = dashboard.UpcomingWidget()
        self.addCleanup(w.deleteLater)
        w.size_ = size
        w.fill(hub)
        return w

    def test_home_upcoming_tile(self):
        w = self.home_tile(NEW_SERVER)
        t = self.texts(w)
        self.assertTrue({"Early access", "Delayed", "Cancelled"} <= t, t)
        dimmed = [r for r in w.findChildren(dashboard.ClickRow) if r.property('dimmed')]
        self.assertEqual(len(dimmed), 1)
        self.assertIn("Game 3", self.texts(dimmed[0]))
        self.assertNotIn("out now", t)                  # the Out now chip replaces it
        old = self.texts(self.home_tile(OLD_SERVER))
        self.assertFalse(old & {"Early access", "Delayed", "Cancelled", "Out now"}, old)


if __name__ == '__main__':
    unittest.main()
