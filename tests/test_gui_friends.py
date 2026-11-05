"""Steam friends in the window, offscreen.

w-80: Settings › Accounts and keys › Steam friends: the key is a password
field, nothing asks the CLI without a key, Refresh now runs
`--friends --refresh --json` in the background, the key never reaches the
status line, edits save to [steam]/[friends], and the search finds it.

w-81: the FRIENDS store (what is shown, and when nothing is), the game
page's Friends block and hero pill, and the Library's Friends own chip,
tile badge, tooltip and List column."""

import os
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

try:
    from test_gui_layout import nx, stop_leftovers
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, stop_leftovers
from tests.patching import patch_gui, set_gui  # noqa: E402

if nx is not None:
    from nexus.gui import settings as gsettings
    from nexus.gui.friends import FRIENDS
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit

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
        FRIENDS.reset()
        self.addCleanup(FRIENDS.reset)
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


# ---- w-81 ----
def iso(ago: float = 0) -> str:
    return datetime.fromtimestamp(time.time() - ago, timezone.utc).isoformat()


def friend(i, name, owned=None, visible=True, playing=None):
    """A made-up friend: owned {appid: minutes}, playing an appid."""
    return {'steamid': sid(i), 'name': name, 'avatar': None, 'visible': visible,
            'private_reason': None if visible else 'game-details',
            'owned': {str(a): m for a, m in (owned or {}).items()},
            'playing': {'appid': playing, 'gameid': str(playing), 'name': 'A game',
                        'since': iso()} if playing else None}


def sid(i: int) -> str:
    return str(76561190000000100 + i)


def answer(rows, error=None, enabled=True, updated=None):
    """A `--friends --json` answer from rows, by_app most minutes first."""
    by_app = {}
    for r in rows:
        if r['visible'] is not False:
            for a, m in r['owned'].items():
                by_app.setdefault(a, []).append((m, r['steamid']))
    return {'enabled': enabled, 'me': ME, 'account': {'persona': 'Tester'}, 'friends': rows,
            'by_app': {a: [s for _m, s in sorted(v, reverse=True)] for a, v in by_app.items()},
            'counts': {}, 'updated': iso() if updated is None else updated, 'evidence': {},
            'error': error}


def crowd():
    """Ten owners of app 10 (P05 and P07 in it now), one private friend;
    app 20 owned by P00."""
    rows = [friend(i, f"P{i:02d}", {10: 100 * (20 - i)}) for i in range(10)]
    for i in (5, 7):
        rows[i]['playing'] = friend(i, '', playing=10)['playing']
    rows[0]['owned']['20'] = 50
    rows.append(friend(10, "Hidden", visible=False))
    return rows


@unittest.skipIf(nx is None, "PyQt6 not available")
class FriendsStore(unittest.TestCase):
    def setUp(self):
        FRIENDS.reset()
        self.addCleanup(FRIENDS.reset)

    def test_owners_by_string_appid_most_played_first(self):
        FRIENDS.set_answer(answer(crowd()))
        self.assertTrue(FRIENDS.usable())
        self.assertEqual(FRIENDS.owner_ids(10)[:3], [sid(0), sid(1), sid(2)])
        self.assertEqual(FRIENDS.owner_ids('20'), [sid(0)])
        self.assertEqual(FRIENDS.owner_ids_of({'source': 'steam', 'appid': 20}), [sid(0)])
        self.assertEqual(FRIENDS.owner_ids_of({'source': 'nexus', 'store_appid': '20'}),
                         [sid(0)])
        self.assertEqual(FRIENDS.owner_ids_of({'source': 'nexus', 'slug': 'x'}), [])
        self.assertEqual([r['name'] for r in FRIENDS.private()], ["Hidden"])
        self.assertEqual(FRIENDS.owned_by_text([sid(0)]), "Owned by P00")
        self.assertEqual(FRIENDS.owned_by_text([sid(0), sid(1)]), "Owned by P00 and P01")
        self.assertEqual(FRIENDS.owned_by_text([sid(i) for i in range(4)]),
                         "Owned by P00, P01 +2")
        self.assertTrue(FRIENDS.has_owners())

    def test_no_key_no_account_or_off_show_nothing(self):
        for ans in (answer(crowd(), {'kind': 'no-key', 'message': 'Add a key'}),
                    answer(crowd(), {'kind': 'no-account', 'message': 'No account'}),
                    answer(crowd(), enabled=False)):
            FRIENDS.set_answer(ans)
            self.assertFalse(FRIENDS.usable())
            self.assertEqual(FRIENDS.owner_ids(10), [])
            self.assertEqual(FRIENDS.private(), [])
            self.assertEqual(FRIENDS.playing_in(10), [])
            self.assertFalse(FRIENDS.has_owners())

    def test_a_refused_key_or_no_network_keep_the_cached_owners(self):
        for kind in ('bad-key', 'offline', 'rate-limited', 'private-list'):
            FRIENDS.set_answer(answer(crowd(), {'kind': kind, 'message': 'x'}))
            self.assertTrue(FRIENDS.usable(), kind)
            self.assertEqual(len(FRIENDS.owner_ids(10)), 10, kind)

    def test_playing_now_goes_once_stale(self):
        FRIENDS.set_answer(answer(crowd()))
        self.assertEqual({r['steamid'] for r in FRIENDS.playing_in(10)}, {sid(5), sid(7)})
        self.assertEqual(FRIENDS.playing_in('10'), FRIENDS.playing_in(10))
        self.assertEqual(FRIENDS.playing_in(10, now=time.time() + 11 * 60), [])
        FRIENDS.set_answer(answer(crowd(), updated=iso(11 * 60)))
        self.assertEqual(FRIENDS.playing_in(10), [])

    def test_changed_fires_only_for_a_real_change(self):
        seen = []
        FRIENDS.changed.connect(self._seen)
        self.addCleanup(FRIENDS.changed.disconnect, self._seen)
        self.seen = seen
        FRIENDS.set_answer(answer(crowd()))
        FRIENDS.set_answer(answer(crowd(), updated=iso(5)))      # same people, later
        self.assertEqual(len(seen), 1)
        FRIENDS.set_answer(None)                                  # the CLI failed: kept
        self.assertTrue(FRIENDS.usable())
        self.assertEqual(len(seen), 1)
        rows = crowd()
        rows[3]['name'] = "Renamed"
        FRIENDS.set_answer(answer(rows))
        self.assertEqual(len(seen), 2)
        FRIENDS.reset()
        self.assertEqual(len(seen), 3)

    def _seen(self):
        self.seen.append(1)


@unittest.skipIf(nx is None, "PyQt6 not available")
class GamePageFriends(unittest.TestCase):
    STEAM = {'slug': 'steam-10', 'display_name': 'Ten', 'source': 'steam', 'appid': 10,
             'launcher': 'steam://rungameid/10'}

    def setUp(self):
        FRIENDS.reset()
        self.addCleanup(FRIENDS.reset)
        p = patch_gui('cli_json', lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self.panel = nx.DetailsPanel()
        self.panel.resize(480, 900)
        self.panel.show()
        self.addCleanup(self.panel.deleteLater)

    def show(self, game=None):
        self.panel.show_games([dict(game or self.STEAM)])
        QApplication.processEvents()
        return self.panel.friends_box

    def pills(self):
        lay = self.panel.chips
        return [lay.itemAt(i).widget().text() for i in range(lay.count())
                if isinstance(lay.itemAt(i).widget(), QLabel)]

    def test_owners_playing_first_then_eight_and_more(self):
        FRIENDS.set_answer(answer(crowd()))
        box = self.show()
        self.assertFalse(box.isHidden())
        self.assertEqual(box.count.text(), "10 own it")
        names = [c.rec['name'] for c in box.chips]
        self.assertEqual(names[:2], ["P05", "P07"])             # in it now
        self.assertEqual(names[2:], ["P00", "P01", "P02", "P03", "P04", "P06"])
        self.assertEqual(box.chips[0].sub.text(), "playing now")
        self.assertIsNone(box.chips[2].sub)
        self.assertIn("P00", box.chips[2].toolTip())
        self.assertIn("h played", box.chips[2].toolTip())
        for c in box.chips:
            self.assertEqual(c.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertEqual(box.more.text(), "+2 more")
        self.assertEqual(box.more.focusPolicy(), Qt.FocusPolicy.NoFocus)
        box.more.click()
        self.assertEqual(len(box.chips), 10)
        self.assertIsNone(box.more)
        self.assertFalse(box.private_row.isHidden())
        self.assertIn("1 friend is private", box.private_text.text())
        self.assertTrue(box.none.isHidden())

    def test_hero_pill_only_while_a_friend_plays(self):
        FRIENDS.set_answer(answer(crowd()))
        self.show()
        self.assertIn("P05 and P07 are playing", self.pills())
        rows = crowd()
        rows[5]['playing'] = rows[7]['playing'] = None
        FRIENDS.set_answer(answer(rows))                         # live: the page follows
        self.assertFalse(any("playing" in t for t in self.pills()))
        self.assertTrue(all(c.sub is None for c in self.panel.friends_box.chips))

    def test_none_own_it_and_all_private(self):
        FRIENDS.set_answer(answer([friend(1, "Ana", {99: 10}),
                                   friend(2, "Ben", visible=False)]))
        box = self.show()
        self.assertFalse(box.isHidden())
        self.assertTrue(box.host.isHidden())
        self.assertFalse(box.none.isHidden())
        self.assertIn("None of your friends", box.none.text())
        self.assertFalse(box.private_row.isHidden())
        FRIENDS.set_answer(answer([friend(1, "Ana", visible=False),
                                   friend(2, "Ben", visible=False)]))
        box = self.show()
        self.assertTrue(box.host.isHidden())
        self.assertTrue(box.none.isHidden())
        self.assertIn("2 friends are private", box.private_text.text())

    def test_hidden_without_a_key_off_or_for_a_game_steam_cant_match(self):
        FRIENDS.set_answer(answer(crowd(), {'kind': 'no-key', 'message': 'x'}))
        self.assertTrue(self.show().isHidden())
        self.assertFalse(any("playing" in t for t in self.pills()))
        FRIENDS.set_answer(answer(crowd()))
        self.assertFalse(self.panel.friends_box.isHidden())
        box = self.show({'slug': 'rp', 'display_name': 'Repack', 'source': 'nexus',
                         'launcher': '/rp'})
        self.assertTrue(box.isHidden())
        self.show()
        FRIENDS.reset()
        self.assertTrue(self.panel.friends_box.isHidden())


@unittest.skipIf(nx is None, "PyQt6 not available")
class LibraryFriends(unittest.TestCase):
    """The Library's Friends own chip, the tile badge under it, the tooltip
    and the List column (MainWindow, as test_gui_collections.Window)."""

    @classmethod
    def setUpClass(cls):
        cls._saved = (nx.cli_json, nx.LibraryPrefs.save, nx.CliQuery)
        set_gui('cli_json', lambda *a, **k: None)
        nx.LibraryPrefs.save = lambda self: None
        set_gui('CliQuery', lambda *a, **k: None)

    @classmethod
    def tearDownClass(cls):
        set_gui('cli_json', cls._saved[0])
        nx.LibraryPrefs.save = cls._saved[1]
        set_gui('CliQuery', cls._saved[2])

    def setUp(self):
        FRIENDS.reset()
        self.win = nx.MainWindow()
        self.win.prefs.library_state = {}
        self.win.prefs.show_owned = True
        self.win.prefs.owned_view = 'installed'
        self.win.resize(1400, 850)
        self.win.show()
        self.win.go(self.win.LIBRARY)
        self.win.set_view('grid', save=False)
        self.win._on_library({'games': self.games(), 'disks': []}, '')
        QApplication.processEvents()

    def tearDown(self):
        stop_leftovers(self.win.jobs)
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        FRIENDS.reset()

    @staticmethod
    def games():
        return [{'slug': 'steam-10', 'display_name': 'Ten', 'source': 'steam', 'appid': 10,
                 'launcher': 'steam://rungameid/10'},
                {'slug': 'steam-20', 'display_name': 'Twenty', 'source': 'steam', 'appid': 20,
                 'launcher': 'steam://rungameid/20', 'installed': False},
                {'slug': 'steam-30', 'display_name': 'Thirty', 'source': 'steam', 'appid': 30,
                 'launcher': 'steam://rungameid/30'},
                {'slug': 'rp', 'display_name': 'Repack', 'source': 'nexus', 'launcher': '/rp'}]

    def shown(self):
        return sorted(g['slug'] for g in self.win._shown_games)

    def test_chip_hidden_until_friends_own_something_then_filters(self):
        w = self.win
        chip = w.chips['friends']
        self.assertTrue(chip.isHidden())
        FRIENDS.set_answer(answer([friend(1, "Ana", visible=False)]))   # nothing to filter
        self.assertTrue(chip.isHidden())
        FRIENDS.set_answer(answer(crowd()))
        self.assertFalse(chip.isHidden())
        self.assertEqual(w.game('steam-10')['friends'][0], sid(0))
        self.assertNotIn('friends', w.game('steam-30'))
        chip.click()
        self.assertEqual(w._chip, 'friends')
        self.assertEqual(self.shown(), ['steam-10'])              # Installed
        w._set_owned_view('owned')
        self.assertEqual(self.shown(), ['steam-20'])              # Not installed
        w._set_owned_view('all')
        self.assertEqual(self.shown(), ['steam-10', 'steam-20'])  # Everything
        FRIENDS.set_answer(answer(crowd(), {'kind': 'no-key', 'message': 'x'}))
        self.assertEqual(self.shown(), [])
        self.assertFalse(chip.isHidden())                          # in use: stays reachable
        w._friends_done(answer(crowd(), {'kind': 'no-key', 'message': 'x'}), '')
        self.assertEqual(w._chip, 'all')                           # unusable: back to All

    def test_badge_only_under_the_filter_and_a_tooltip_always(self):
        w = self.win
        FRIENDS.set_answer(answer(crowd()))
        QApplication.processEvents()
        tile = w.library.tile_for('steam-10')
        self.assertFalse(tile.friends_badge)
        self.assertIn("Owned by P00, P01 +8", tile.toolTip())
        self.assertNotIn("Owned by", w.library.tile_for('steam-30').toolTip())
        x, y = 8 + 3 + tile.FRIEND_D / 2, tile.height() - 8 - 3 - tile.FRIEND_D / 2

        def px(t):
            t._anim.stop()                     # at rest: focus would fade the badge out
            t._hover = 0.0
            return t.grab().toImage().pixelColor(int(x), int(y)).name()
        bare = px(tile)
        w._set_chip('friends')
        QApplication.processEvents()
        tile = w.library.tile_for('steam-10')
        self.assertTrue(tile.friends_badge)
        self.assertNotEqual(px(tile), bare)                        # an avatar there now
        w._set_chip('all')
        QApplication.processEvents()
        tile = w.library.tile_for('steam-10')
        self.assertFalse(tile.friends_badge)
        self.assertEqual(px(tile), bare)

    def test_restored_chip_falls_back_when_friends_are_off(self):
        w = self.win
        w._set_chip('friends')
        self.assertFalse(w.chips['friends'].isHidden())            # in use
        w.config['friends_enabled'] = False
        w._load_friends()
        self.assertEqual(w._chip, 'all')
        self.assertTrue(w.chips['friends'].isHidden())

    def test_list_column_hidden_without_data(self):
        w = self.win
        w.set_view('list', save=False)
        lv = w.listview
        col = lv.COLS.index("Friends")
        self.assertTrue(lv.isColumnHidden(col))
        FRIENDS.set_answer(answer(crowd()))
        self.assertFalse(lv.isColumnHidden(col))
        rows = {lv.item(r, 1).data(Qt.ItemDataRole.UserRole): lv.item(r, col).text()
                for r in range(lv.rowCount())}
        self.assertEqual(rows['steam-10'], '10')
        self.assertEqual(rows['rp'], '—')
