"""Steam friends in the window, offscreen.

w-80: Settings › Accounts and keys › Steam friends: the key is a password
field, nothing asks the CLI without a key, Refresh now runs
`--friends --refresh --json` in the background, the key never reaches the
status line, edits save to [steam]/[friends], and the search finds it.

w-81: the FRIENDS store (what is shown, and when nothing is), the game
page's Friends block and hero pill, and the Library's Friends own chip,
tile badge, tooltip and List column.

w-82: who plays what grouped by game (playing_now), Home's Friends playing
now widget and its states, the presence timer (only while Home shows and
the window is active), and Theatre's Home row, game drawer section, fact
line and Sort & filter's Friends own."""

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

try:
    from test_gui_layout import nx, stop_leftovers
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, stop_leftovers
from tests.patching import patch_gui, set_gui  # noqa: E402

if nx is not None:
    from nexus.gui import dashboard as gdash, settings as gsettings
    from nexus.gui.friends import FRIENDS
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtWidgets import (QApplication, QFrame, QLabel, QLineEdit, QPushButton,
        QWidget)

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
    def test_gamedb_only_wording(self):
        """w-84: no Steam key, Game-db live: no Steam wording, Game-db's time."""
        st = gsettings.friends_status_text
        no_key = {'kind': 'no-key', 'message': 'Add a key'}
        g = {'updated': '2026-11-05T09:00:00+00:00', 'count': 2, 'error': None}
        now = datetime.fromisoformat(g['updated']).timestamp() + 125
        data = {**ANSWER, 'error': no_key, 'gamedb': g}
        self.assertEqual(st(data, now=now), "2 on Game-db \u00b7 updated 2 min ago")
        self.assertEqual(st({**data, 'gamedb': {**g, 'count': 0}}, now=now),
                         "Nobody on Game-db shares with you yet")
        self.assertEqual(st({**ANSWER, 'counts': {'total': 2, 'public': 2, 'private': 0},
                             'gamedb': g}, now=now + 3 * 3600),
                         "No friends on this Steam account \u00b7 2 on Game-db \u00b7 "
                         "updated 2 h ago")


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

    def test_gamedb_row_says_you_share_nothing_only_when_live(self):
        from nexus import gamedb
        page = self.page()                                     # not live: hidden, no ask
        row = page.rows['accounts.friends_gamedb']
        self.assertFalse(row.available)
        self.assertTrue(row.isHidden())
        self.assertEqual(self.friends_calls(), [])
        with mock.patch.dict(os.environ, {gamedb.FRIENDS_STUB_ENV: '/nonexistent.json'}):
            page = self.page()                                 # live, still no Steam key
        row = page.rows['accounts.friends_gamedb']
        self.assertEqual(self.friends_calls(), [['--friends', '--json']])
        self.assertTrue(page.fr_enabled.isEnabled())
        self.assertIn("Steam friends need a Steam Web API key", page.fr_enabled_row.desc.text())
        cb = [c for args, c in self.calls if '--friends' in args][-1]
        ans = merged([], [gdb_row('ana', "Ana"), gdb_row('ben', "Ben")],
                     {'kind': 'no-key', 'message': 'Add a key'})
        ans['gamedb']['error'] = {'kind': 'rate', 'message': "Game-db is rate-limiting"}
        cb(ans, '')
        self.assertTrue(row.available)
        self.assertEqual(row.desc.text(),
                         "2 people share their library with you \u00b7 You share: nothing")
        self.assertIn("rate-limiting", row.note.text())
        self.assertTrue(page.fr_status_row.desc.text().startswith("2 on Game-db"))
        self.assertTrue(page.fr_status_row.error.isHidden())  # no Steam key is fine here
        self.assertFalse(row.findChildren(QPushButton))        # no share control, ever
        self.assertFalse(row.findChildren(gsettings.Switch))
        cb(answer([]), '')                                     # Game-db off again
        self.assertFalse(row.available)

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


def gdb_row(fid, name, owned=None, owned_store=None, steamid=None):
    """A made-up Game-db friend row as `--friends --json` gives it."""
    return {'id': f'gdb:{fid}', 'source': 'gamedb', 'steamid': steamid, 'name': name,
            'avatar': None, 'visible': True, 'private_reason': None, 'evidence': {},
            'owned': {str(a): m for a, m in (owned or {}).items()},
            'owned_store': dict(owned_store or {}), 'playing': None}


def merged(rows, gdb_rows, error=None):
    """answer() with Game-db rows merged in, as the CLI does when it is live."""
    ans = answer([{**r, 'id': r['steamid'], 'source': 'steam'} for r in rows], error)
    ans['friends'] = ans['friends'] + gdb_rows
    for r in gdb_rows:
        for a, m in r['owned'].items():
            ans['by_app'].setdefault(a, []).append(r['id'])
    by_store = {}
    for r in gdb_rows:
        for k in r['owned_store']:
            by_store.setdefault(k, []).append(r['id'])
    ans.update(by_store=by_store, sources=sorted({r['source'] for r in ans['friends']}),
               gamedb={'updated': iso(), 'error': None, 'count': len(gdb_rows)})
    return ans


HEROIC = {'slug': 'heroic-legendary-example-epic-1', 'display_name': 'Example Epic Game',
          'source': 'heroic', 'launcher': 'heroic://launch/legendary/example-epic-1'}


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

    def test_gamedb_rows_join_by_store_and_survive_no_key(self):
        ana = gdb_row('ana', "Ana GDB", {10: 5}, {'epic:example-epic-1': 45})
        FRIENDS.set_answer(merged(crowd(), [ana]))
        self.assertEqual(FRIENDS.owner_ids_of(HEROIC), ['gdb:ana'])
        self.assertEqual(FRIENDS.owner_ids(10)[-1], 'gdb:ana')
        self.assertEqual(FRIENDS.record('gdb:ana')['name'], "Ana GDB")
        self.assertEqual(FRIENDS.minutes(FRIENDS.record('gdb:ana'), HEROIC), 45)
        self.assertEqual(FRIENDS.minutes(FRIENDS.record('gdb:ana'),
                                         {'source': 'steam', 'appid': 10}), 5)
        self.assertTrue(FRIENDS.two_sources())
        # No Steam key: the Steam rows go, the Game-db ones stay
        FRIENDS.set_answer(merged(crowd(), [ana], {'kind': 'no-key', 'message': 'x'}))
        self.assertTrue(FRIENDS.usable())
        self.assertEqual([r['id'] for r in FRIENDS.friends()], ['gdb:ana'])
        self.assertEqual(FRIENDS.owner_ids(10), ['gdb:ana'])
        self.assertEqual(FRIENDS.private(), [])
        self.assertFalse(FRIENDS.two_sources())
        self.assertTrue(FRIENDS.has_owners())

    def test_gamedb_counts_only_with_people(self):
        no_key = {'kind': 'no-key', 'message': 'x'}
        FRIENDS.set_answer(merged(crowd(), [], no_key))            # live, nobody shares
        self.assertFalse(FRIENDS.usable())
        FRIENDS.set_answer(merged(crowd(), [gdb_row('ana', "Ana")], no_key))
        self.assertTrue(FRIENDS.usable())
        self.assertEqual(FRIENDS.steam(), [])

    def test_public_ids_steam_only(self):
        FRIENDS.set_answer(merged(crowd(), [gdb_row('ana', "Ana GDB", {10: 5},
                                                    steamid=sid(1))]))
        ids = FRIENDS.public_ids()
        self.assertNotIn('gdb:ana', ids)
        self.assertIn(sid(1), ids)
        self.assertTrue(all(i.isdigit() for i in ids))
        self.assertEqual(FRIENDS.record('gdb:ana')['steam_link'], sid(1))

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

    def test_online_needs_fresh_presence_and_a_status(self):
        """w-91: online = public friends with personastate 1-6 or in a game;
        nothing once stale; a status change alone is no `changed`."""
        rows = [friend(1, "Ana"), friend(2, "Ben", playing=10), friend(3, "Cy"),
                friend(4, "Dee", visible=False), friend(5, "Eve"), friend(6, "Fox")]
        for r, st in zip(rows, (1, 0, 0, 1, 7, 3)):
            r['evidence'] = {'personastate': st}
        rows[2]['evidence'] = {}
        FRIENDS.set_answer(answer(rows))
        self.assertEqual([r['name'] for r in FRIENDS.online()], ["Ana", "Ben", "Fox"])
        self.assertEqual(FRIENDS.online(now=time.time() + 11 * 60), [])
        seen = []
        FRIENDS.changed.connect(self._seen)
        self.addCleanup(FRIENDS.changed.disconnect, self._seen)
        self.seen = seen
        rows[2]['evidence'] = {'personastate': 1}
        FRIENDS.set_answer(answer(rows))
        self.assertEqual(seen, [])                       # the Library doesn't rebuild
        self.assertIn("Cy", [r['name'] for r in FRIENDS.online()])
        FRIENDS.set_answer(answer(rows, updated=iso(11 * 60)))
        self.assertEqual(FRIENDS.online(), [])
        FRIENDS.set_answer(answer(rows, {'kind': 'no-key', 'message': 'x'}))
        self.assertEqual(FRIENDS.online(), [])


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

    def test_heroic_game_shows_gamedb_owner(self):
        ana = gdb_row('ana', "Ana GDB", owned_store={'epic:example-epic-1': 90})
        FRIENDS.set_answer(merged(crowd(), [ana]))
        box = self.show(HEROIC)
        self.assertFalse(box.isHidden())
        self.assertEqual(box.count.text(), "1 own it")
        self.assertEqual([c.rec['name'] for c in box.chips], ["Ana GDB"])
        self.assertIn("1.5 h played", box.chips[0].toolTip())
        # A repack with neither an app id nor a store key: still hidden
        self.assertTrue(self.show({'slug': 'rp', 'display_name': 'Repack', 'source': 'nexus',
                                   'launcher': '/rp'}).isHidden())

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


# ---- w-82 ----
def playing(i, name, appid, mins, owned=None, visible=True):
    """A friend in appid for `mins` minutes (since as epoch seconds, as the CLI)."""
    r = friend(i, name, owned, visible)
    r['playing'] = {'appid': appid, 'gameid': str(appid), 'name': f"Steam {appid}",
                    'since': int(time.time() - mins * 60)}
    return r


def live_crowd():
    """Ana (80 min), Ben (50) and Cy (10) in app 10; Dee in app 99 for 3 h;
    Eve in a mod (no appid); Fay private and 'in' app 10; Gus idle."""
    rows = [playing(1, "Ana", 10, 80, {10: 600}), playing(2, "Ben", 10, 50, {10: 300}),
            playing(3, "Cy", 10, 10), playing(4, "Dee", 99, 180), friend(5, "Eve"),
            playing(6, "Fay", 10, 5, visible=False), friend(7, "Gus", {10: 50})]
    rows[4]['playing'] = {'appid': None, 'gameid': '9' * 19, 'name': 'A mod', 'since': iso()}
    return rows


GAMES = [{'slug': 'steam-10', 'display_name': 'Ten', 'source': 'steam', 'appid': 10,
          'launcher': 'steam://rungameid/10'},
         {'slug': 'steam-20', 'display_name': 'Twenty', 'source': 'steam', 'appid': 20,
          'launcher': 'steam://rungameid/20', 'installed': False},
         {'slug': 'steam-30', 'display_name': 'Thirty', 'source': 'steam', 'appid': 30,
          'launcher': 'steam://rungameid/30'}]


@unittest.skipIf(nx is None, "PyQt6 not available")
class SourceMark(unittest.TestCase):
    """w-84: a Steam or Game-db mark on people, only with friends from both."""

    def setUp(self):
        FRIENDS.reset()
        self.addCleanup(FRIENDS.reset)

    def test_mark_only_with_two_sources(self):
        from nexus.gui.friends import PersonChip, source_mark
        from nexus.gui.theme import G
        ana = gdb_row('ana', "Ana GDB", {10: 5})
        FRIENDS.set_answer(answer(crowd()))                       # Steam only
        steam = FRIENDS.record(sid(0))
        self.assertIsNone(source_mark(steam, 32))
        self.assertNotIn("on Steam", PersonChip(steam).toolTip())
        FRIENDS.set_answer(merged(crowd(), [ana]))                # both
        steam, gdb = FRIENDS.record(sid(0)), FRIENDS.record('gdb:ana')
        self.assertEqual(source_mark(steam, 32), G['steam'])
        self.assertEqual(source_mark(gdb, 32), G['gamedb'])
        self.assertIsNone(source_mark(gdb, 32, ring='bg'))        # never in a stack
        self.assertIsNone(source_mark(gdb, 16))                   # too small to read
        self.assertIn("on Game-db", PersonChip(gdb).toolTip())
        self.assertIn("on Steam", PersonChip(steam).toolTip())
        chip = PersonChip(gdb)
        chip.resize(chip.sizeHint())
        self.assertFalse(chip.grab().isNull())                    # paints the mark
        FRIENDS.set_answer(merged(crowd(), [ana], {'kind': 'no-key', 'message': 'x'}))
        self.assertIsNone(source_mark(FRIENDS.record('gdb:ana'), 32))   # one source left


@unittest.skipIf(nx is None, "PyQt6 not available")
class PlayingNow(unittest.TestCase):
    def setUp(self):
        FRIENDS.reset()
        self.addCleanup(FRIENDS.reset)

    def test_playing_now_groups_by_game_most_friends_first(self):
        FRIENDS.set_answer(answer(live_crowd()))
        out = nx.playing_now(GAMES)
        self.assertEqual([e['appid'] for e in out], [10, 99])     # three friends beat 3 h
        ten = out[0]
        self.assertEqual([r['name'] for r in ten['people']], ["Ana", "Ben", "Cy"])  # longest first
        self.assertEqual(ten['minutes'], 80)
        self.assertEqual((ten['slug'], ten['title']), ('steam-10', 'Ten'))       # your copy's name
        self.assertIn('/apps/10/header.jpg', ten['headerImage'])
        self.assertEqual((out[1]['slug'], out[1]['title']), (None, "Steam 99"))  # Steam's name
        # A hidden game counts as not in your library
        hidden = [dict(g, hidden=True) if g['appid'] == 10 else g for g in GAMES]
        self.assertIsNone(nx.playing_now(hidden)[0]['slug'])
        # ISO times work as well as epoch seconds
        rows = live_crowd()
        rows[0]['playing']['since'] = iso(80 * 60 + 1)   # Windows' clock may not tick before playing_now
        FRIENDS.set_answer(answer(rows))
        self.assertEqual(nx.playing_now(GAMES)[0]['minutes'], 80)
        # Stale presence, friends off: nothing
        self.assertEqual(nx.playing_now(GAMES, now=time.time() + 11 * 60), [])
        FRIENDS.set_answer(answer(live_crowd(), {'kind': 'no-key', 'message': 'x'}))
        self.assertEqual(nx.playing_now(GAMES), [])
        # At most eight games, unless asked for all
        FRIENDS.set_answer(answer([playing(i, f"P{i}", 100 + i, i) for i in range(12)]))
        self.assertEqual(len(nx.playing_now(GAMES)), 8)
        self.assertEqual(len(nx.playing_now(GAMES, limit=None)), 12)
        self.assertEqual(nx.playing_now(GAMES)[0]['appid'], 111)              # the longest

    def test_who_text(self):
        FRIENDS.set_answer(answer(live_crowd()))
        out = nx.playing_now(GAMES)
        self.assertEqual(nx.who_text(out[0]), "Ana, Ben +1 · 1 h 20 min")
        self.assertEqual(nx.who_text(out[1]), "Dee · 3 h")
        self.assertEqual(nx.who_text({'people': [], 'minutes': 5}), "5 min")

    def test_age(self):
        self.assertIsNone(FRIENDS.age())
        FRIENDS.set_answer(answer(live_crowd(), updated=iso(90)))
        self.assertAlmostEqual(FRIENDS.age(), 90, delta=2)


@unittest.skipIf(nx is None, "PyQt6 not available")
class _FriendsWindow(unittest.TestCase):
    """A MainWindow with friends on, whose CLI answers `--friends` with
    self.reply (and records every call)."""

    def setUp(self):
        FRIENDS.reset()
        self.calls, self.reply, self.active = [], answer(live_crowd()), True
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.opened = []
        for p in (patch_gui('cli_json', self.fake_cli), patch_gui('CliQuery', lambda *a, **k: None),
                  mock.patch.object(nx.LibraryPrefs, 'save', lambda self: None),
                  mock.patch.object(nx.LibraryPrefs, 'PATH', tmp / 'library.json'),
                  mock.patch.object(nx.MainWindow, '_poll_running', lambda self: None),
                  mock.patch.object(gdash, 'open_url', self.opened.append)):
            p.start()
            self.addCleanup(p.stop)
        for name, ret in (('power_caps', set()), ('power', True), ('bluetooth_pads', []),
                          ('bluetooth_disconnect', True)):
            p = mock.patch(f'nexus.platform.{name}', return_value=ret)
            p.start()
            self.addCleanup(p.stop)
        w = self.win = nx.MainWindow()
        w.isActiveWindow = lambda: self.active
        w.prefs.dashboard = nx.dash_kind_defaults()          # Friends at full size
        w.prefs.library_state = {}
        w.config['friends_enabled'] = True
        w.resize(1400, 900)
        w.show()
        w._on_library({'games': [dict(g) for g in GAMES], 'disks': []}, '')
        QApplication.processEvents()

    def tearDown(self):
        stop_leftovers(self.win.jobs)
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        FRIENDS.reset()

    def fake_cli(self, args, cb, *a, **k):
        self.calls.append(list(args))
        if '--friends' in args and self.reply is not None:
            cb(self.reply, '')

    def presence_calls(self):
        return [c for c in self.calls if '--presence' in c]

    def widget(self):
        self.win.dashboard.refresh(['friends'])
        QApplication.processEvents()
        return self.win.dashboard.widgets['friends']

    def placed(self):
        return 'friends' in {k for k, *_ in nx.dash_pack(
            self.win.dashboard.items, 3, self.win.dashboard._skip())}

    def set_size(self, size):
        for it in self.win.prefs.dashboard:
            if it['id'] == 'friends':
                it['size'] = size


class HomeFriends(_FriendsWindow):
    def test_widget_only_while_a_friend_plays_after_jump(self):
        ids = [i['id'] for i in self.win.prefs.dashboard]
        self.assertEqual(ids[ids.index('stage') + 1:][:3], ['save', 'foryou', 'friends'])
        fw = self.widget()
        self.assertTrue(self.placed())
        self.assertEqual(fw.state, 'live')
        self.assertEqual([c.appid for c in fw.cards], [10, 99])
        card = fw.cards[0]
        self.assertEqual(card.title.text(), "Ten")
        self.assertIsNotNone(card.pill)                     # In your library
        self.assertIsNone(fw.cards[1].pill)
        self.assertEqual(card.sub_label.text(), "Ana, Ben +1 · 1 h 20 min")
        self.assertEqual(card.stack.parentWidget(), card.art)
        self.assertIn("1 friend is private on Steam · updated", fw.footer.text())
        # Nobody in a game: out of the layout, but Customise still shows it
        rows = [dict(r, playing=None) for r in live_crowd()]
        FRIENDS.set_answer(answer(rows))
        fw = self.widget()
        self.assertFalse(self.placed())
        self.assertIn("No friends are in a game", fw.body.itemAt(0).widget().text())
        self.win.dashboard.set_editing(True)
        self.assertTrue(self.placed())
        self.win.dashboard.set_editing(False)

    def test_wide_rows_and_footer(self):
        FRIENDS.set_answer(answer([playing(i, f"P{i}", 100 + i, 60 - i) for i in range(6)],
                                  {'kind': 'offline', 'message': 'x'}))
        self.set_size('wide')
        fw = self.widget()
        self.assertEqual(len(fw.cards), 4)
        self.assertEqual(fw.cards[0].sub_label.text(), "P0 · 1 h")
        texts = [fw.body.itemAt(i).widget().text() for i in range(fw.body.count())
                 if isinstance(fw.body.itemAt(i).widget(), QLabel)]
        self.assertIn("+2 more", texts)
        self.assertTrue(fw.footer.text().startswith("As of "))           # offline
        self.assertIn("Friends only", fw.footer.toolTip())

    def test_click_owned_reveals_unowned_opens_store(self):
        fw = self.widget()
        fw.cards[1].clicked.emit()
        self.assertEqual(self.opened, ["https://store.steampowered.com/app/99/"])
        fw.cards[0].clicked.emit()
        QApplication.processEvents()
        self.assertEqual(self.win.current(), self.win.LIBRARY)
        self.assertEqual(self.win.selected, ['steam-10'])

    def test_states_setup_loading_bad_key_offline(self):
        w = self.win
        w.config['friends_enabled'] = False
        w._load_friends()
        fw = self.widget()
        self.assertEqual(fw.state, 'setup')
        self.assertFalse(self.placed())
        btn = next(b for b in fw.findChildren(QPushButton) if b.text() == "Set up in Settings")
        with mock.patch.object(w, '_open_settings') as op:
            btn.click()
        op.assert_called_once_with('accounts', 'friends')
        w.config['friends_enabled'] = True
        self.reply = None                                   # the CLI hasn't answered yet
        w._reload_friends()
        self.assertEqual(self.widget().state, 'loading')
        self.reply = answer(live_crowd(), {'kind': 'bad-key', 'message': 'refused'})
        w._reload_friends()
        fw = self.widget()
        self.assertEqual(fw.state, 'bad-key')
        self.assertTrue(self.placed())                     # an error shows outside Customise
        retry = next(b for b in fw.findChildren(QPushButton) if b.text() == "Retry")
        n = len(self.calls)
        retry.click()
        self.assertEqual(self.calls[n:], [['--friends', '--json']])
        self.reply = answer([friend(1, "Ana", visible=False)])
        w._reload_friends()
        self.assertEqual(self.widget().state, 'private')
        self.reply = answer([])
        w._reload_friends()
        self.assertEqual(self.widget().state, 'none')

    def test_refill_keeps_focus_updates_minutes_in_place(self):
        fw = self.widget()
        card = fw.cards[0]
        card.setFocus()
        QApplication.processEvents()
        rows = live_crowd()
        rows[0]['playing']['since'] -= 600                      # ten more minutes
        FRIENDS.set_answer(answer(rows))
        fw = self.widget()
        self.assertIs(fw.cards[0], card)                        # nothing rebuilt
        self.assertEqual(card.sub_label.text(), "Ana, Ben +1 · 1 h 30 min")
        rows.append(playing(8, "Hal", 99, 1))                   # someone joins Dee: rebuilt
        FRIENDS.set_answer(answer(rows))
        fw = self.widget()
        self.assertIsNot(fw.cards[0], card)
        QApplication.processEvents()
        self.assertIs(QApplication.focusWidget(), fw.cards[0])  # the same game keeps focus
        self.assertEqual(fw.cards[0].appid, 10)


class HomeFriendsSmall(_FriendsWindow):
    """w-91: Friends at Small: who's online, the first two in a game."""

    def setUp(self):
        super().setUp()
        self.set_size('small')
        self.rows = live_crowd()
        for r in self.rows:
            r['evidence'] = {'personastate': 0}
        self.rows[6]['evidence'] = {'personastate': 1}          # Gus: online, not in a game
        FRIENDS.set_answer(answer(self.rows))

    def texts(self, fw):
        return [lb.text() for lb in fw.findChildren(QLabel) if lb.text() and lb.isVisibleTo(fw)]

    def test_small_live(self):
        self.assertEqual(nx.DASH_SIZES['friends'], ('small', 'wide', 'full'))
        self.win.dashboard.relayout(force=True)
        fw = self.widget()
        self.assertTrue(self.placed())
        self.assertEqual(fw.title_label.text(), "Friends")
        self.assertEqual(fw.edit_name.text(), "Friends")
        t = self.texts(fw)
        # Ana, Ben, Cy and Dee in a game, Eve in a mod, Gus online: 6 online, 4 in a game
        self.assertIn("6 online · 4 in a game", t)
        self.assertEqual([c.toolTip() for c in fw.cards],
                         ["Open its page", "Open its page"])
        lines = [c.findChild(nx.ElideLabel).text() for c in fw.cards]
        self.assertEqual(lines, ["Ana in Ten", "Ben in Ten"])
        self.assertIn("+2 more in a game", t)
        fw.cards[0].clicked.emit()
        QApplication.processEvents()
        self.assertEqual(self.win.selected, ['steam-10'])
        # Back at Full width the title is the old one again.
        self.set_size('full')
        self.win.dashboard.refresh(['friends'])
        self.win.dashboard.relayout(force=True)
        self.assertEqual(fw.title_label.text(), "Friends playing now")

    def test_small_shows_with_nobody_online_and_says_so(self):
        rows = [dict(r, playing=None, evidence={'personastate': 0}) for r in self.rows]
        FRIENDS.set_answer(answer(rows))
        fw = self.widget()
        self.assertTrue(self.placed())                         # any public friend: shown
        self.assertIn("No friends online", self.texts(fw))
        self.assertEqual(fw.cards, [])
        stack = fw.findChild(nx.AvatarStack)
        self.assertTrue(stack.dim)
        # Stale, no error: checking
        FRIENDS.set_answer(answer(self.rows, updated=iso(11 * 60)))
        self.assertIn("Checking who's online…", self.texts(self.widget()))
        # Steam not answering: the last time, dimmed
        FRIENDS.reset()
        FRIENDS.set_answer(answer(self.rows, {'kind': 'offline', 'message': 'x'},
                                  updated=iso(3 * 3600)))
        fw = self.widget()
        t = self.texts(fw)
        self.assertIn("Steam isn't answering", t)
        self.assertTrue(any(x.startswith("As of ") for x in t))
        self.assertTrue(fw.findChild(nx.AvatarStack).dim)

    def test_small_states(self):
        w = self.win
        FRIENDS.reset()
        FRIENDS.set_answer(answer([friend(1, "Ana", visible=False),
                                   friend(2, "Ben", visible=False)]))
        fw = self.widget()
        self.assertEqual(fw.state, 'private')
        self.assertFalse(self.placed())                        # Customise only
        self.assertIn("All 2 friends keep their game details private.", self.texts(fw))
        FRIENDS.set_answer(answer([]))
        self.assertIn("No friends on this Steam account.", self.texts(self.widget()))
        w.config['friends_enabled'] = False
        w._load_friends()
        fw = self.widget()
        self.assertEqual(fw.state, 'setup')
        self.assertIn("See what your Steam friends play.", self.texts(fw))
        self.assertFalse(self.placed())
        w.dashboard.set_editing(True)
        self.assertTrue(self.placed())
        w.dashboard.set_editing(False)

    def test_small_gamedb_people_are_never_presence(self):
        ana = gdb_row('ana', "Ana GDB")
        FRIENDS.reset()
        FRIENDS.set_answer(merged([], [ana], {'kind': 'no-key', 'message': 'x'}))
        self.assertEqual(self.widget().state, 'setup')         # Steam isn't set up
        FRIENDS.set_answer(merged([], [ana]))
        self.assertEqual(self.widget().state, 'none')          # no Steam friends
        FRIENDS.set_answer(merged(self.rows, [ana]))
        fw = self.widget()
        self.assertEqual(fw.state, 'live')
        self.assertNotIn('gdb:ana', [r['steamid'] for r in FRIENDS.online()])
        self.assertNotIn("Ana GDB", self.texts(fw))

    def test_small_refill_keeps_rows_and_focus(self):
        fw = self.widget()
        row = fw.cards[0]
        row.setFocus()
        QApplication.processEvents()
        rows = [dict(r) for r in self.rows]
        rows[0]['playing'] = dict(rows[0]['playing'], since=rows[0]['playing']['since'] - 60)
        FRIENDS.set_answer(answer(rows))                       # a minute later: no rebuild
        self.assertIs(self.widget().cards[0], row)
        rows[6] = dict(rows[6], evidence={'personastate': 0})  # Gus went offline: rebuilt
        FRIENDS.set_answer(answer(rows))
        fw = self.widget()
        self.assertIsNot(fw.cards[0], row)
        QApplication.processEvents()
        self.assertIs(QApplication.focusWidget(), fw.cards[0])
        self.assertIn("5 online · 4 in a game", self.texts(fw))


class Presence(_FriendsWindow):
    def test_timer_only_on_home_while_active(self):
        w = self.win
        t = w._presence_timer
        w.go(w.LIBRARY)
        self.assertFalse(t.isActive())
        self.reply = answer(live_crowd(), updated=iso(90))       # over a minute old
        FRIENDS.reset()                                          # (an older answer is kept out)
        w._reload_friends()
        self.calls.clear()
        w.go(w.HOME)
        self.assertTrue(t.isActive())
        self.assertEqual(t.interval(), 120_000)
        self.assertEqual(self.presence_calls(), [['--friends', '--presence', '--json']])
        self.calls.clear()
        self.reply = answer(live_crowd())                        # fresh now
        w._presence_tick()
        self.assertEqual(len(self.presence_calls()), 1)
        self.calls.clear()
        w._sync_presence()                                       # fresh: no new ask
        self.assertEqual(self.presence_calls(), [])
        self.active = False                                      # another window in front
        w._sync_presence()
        self.assertFalse(t.isActive())
        self.active = True
        w._sync_presence()
        self.assertTrue(t.isActive())
        # Not while the plain call is on its way (it brings presence too)
        w._friends_loading = True
        w._presence_tick()
        self.assertEqual(self.presence_calls(), [])
        w._friends_loading = False
        # A refused key or a private list never polls
        self.reply = answer(live_crowd(), {'kind': 'bad-key', 'message': 'x'})
        w._reload_friends()
        self.assertFalse(t.isActive())
        self.reply = answer(live_crowd())
        w._reload_friends()
        self.assertTrue(t.isActive())
        # Theatre: its Home tab, a game page over it too; not Library
        w.set_theatre(True)
        QApplication.processEvents()
        self.assertTrue(t.isActive())
        w.theatre.open_game(w.game('steam-10'))
        w._sync_presence()
        self.assertTrue(t.isActive())
        w.theatre.close_game()
        w.theatre.tabs.set_current('library', emit=True)
        self.assertFalse(t.isActive())
        w.theatre.tabs.set_current('home', emit=True)
        self.assertTrue(t.isActive())
        w.set_theatre(False)
        QApplication.processEvents()

    def test_an_older_memo_never_replaces_presence(self):
        w = self.win
        newer = answer(live_crowd(), updated=iso(5))
        FRIENDS.set_answer(newer)
        w._friends_done(answer([], updated=iso(200)), '')
        self.assertIs(FRIENDS.data, newer)


class TheatreFriends(_FriendsWindow):
    def theatre(self):
        w = self.win
        w.set_theatre(True)
        QApplication.processEvents()
        return w.theatre

    def done(self):
        self.win.set_theatre(False)
        QApplication.processEvents()

    def row_index(self, th, w):
        return [th.rows.itemAt(i).widget() for i in range(th.rows.count())].index(w)

    def test_row_after_your_week_kept_across_set_games(self):
        th = self.theatre()
        self.assertTrue(th.friends_row.isVisibleTo(th))
        self.assertEqual(self.row_index(th, th.friends_row), self.row_index(th, th.week_row) + 1)
        self.assertEqual([c.item['appid'] for c in th.friend_cards], [10, 99])
        card = th.friend_cards[0]
        self.assertEqual(card.kind, 'friends')
        self.assertEqual(card.facts.text(), "Ana, Ben +1 · 1 h 20 min")
        self.assertIn("In your library", card.art.badges())
        self.assertNotIn("In your library", th.friend_cards[1].art.badges())
        th.set_games([g for g in self.win._games if not g.get('hidden')])
        self.assertTrue(th.friends_row.isVisibleTo(th))
        self.assertEqual(self.row_index(th, th.friends_row), self.row_index(th, th.week_row) + 1)
        self.assertIs(th.friend_cards[0], card)                   # kept, not rebuilt
        card.setFocus()
        QApplication.processEvents()
        self.assertEqual(th._game_at(card)['slug'], 'steam-10')    # the tint follows
        self.win._update_hints()
        self.assertEqual(self.win.hints._shown[1][0], ('a', 'Open'))
        # Nobody plays: focus leaves first, then the row hides
        FRIENDS.set_answer(answer([dict(r, playing=None) for r in live_crowd()]))
        QApplication.processEvents()
        self.assertFalse(th.friends_row.isVisibleTo(th))
        f = QApplication.focusWidget()
        self.assertTrue(f is not None and th.isAncestorOf(f) and not th.friends_row.isAncestorOf(f))
        self.done()

    def test_a_owned_opens_page_unowned_opens_sheet(self):
        th = self.theatre()
        owned, other = th.friend_cards
        seen = []
        th.deal_asked.connect(lambda item, kind: seen.append((item['appid'], kind)))
        th._friend_activated(other)
        self.assertEqual(seen[-1], (99, 'friends'))
        self.assertTrue(self.win.theatre_sheet.isVisible())       # Open in Steam
        self.win.theatre_sheet.close_sheet(restore=False)
        other.setFocus()
        QApplication.processEvents()
        self.win._update_hints()
        self.assertEqual(self.win.hints._shown[1][0], ('a', 'Options'))
        th._friend_activated(owned)
        self.assertTrue(th.details.isVisible())
        self.assertEqual(th.details.game['slug'], 'steam-10')
        th.close_game()
        self.done()

    def test_drawer_section_after_overview_grid_private(self):
        rows = live_crowd() + [friend(20 + i, f"Q{i}", {10: 5}) for i in range(8)]
        FRIENDS.set_answer(answer(rows))
        th = self.theatre()
        from nexus.gui import theatre_game as tg
        self.assertEqual([k for k, _t in tg.SECTIONS][:2], ['overview', 'friends'])
        th.open_game(self.win.game('steam-10'))
        QApplication.processEvents()
        det = th.details
        dr = det.drawer
        self.assertIn('friends', dr.visible_keys())
        det.cycle_section(1)                                      # opens on Overview
        det.cycle_section(1)                                      # RB: Friends
        self.assertEqual(dr.tabs.current(), 'friends')
        sec = dr.sections['friends']
        texts = [lab.text() for lab in sec.findChildren(QLabel) if not lab.isHidden()]
        self.assertIn("11 FRIENDS OWN THIS", texts)               # Ana, Ben, Gus + 8
        self.assertIn("and 3 more", texts)
        self.assertIn("Playing now · 1 h 20 min", texts)
        self.assertIn("PRIVATE ON STEAM", texts)
        self.assertIn("Fay", texts)
        cells = [c for c in sec.findChildren(QFrame) if c.objectName() == "TheatreRow"]
        self.assertEqual(len(cells), 8)
        for wdg in sec.findChildren(QWidget):
            self.assertEqual(wdg.focusPolicy(), Qt.FocusPolicy.NoFocus, wdg)
        # A game nobody has: hidden; friends off: hidden
        th.open_game(self.win.game('steam-30'))
        QApplication.processEvents()
        self.assertIn('friends', dr.visible_keys())               # Fay's private line still
        FRIENDS.set_answer(answer([friend(1, "Ana", {10: 5})]))
        QApplication.processEvents()
        self.assertNotIn('friends', dr.visible_keys())
        th.close_game()
        self.done()

    def test_stage_fact_line(self):
        th = self.theatre()
        th.open_game(self.win.game('steam-10'))
        QApplication.processEvents()
        self.assertIn("Ana, Ben +1 playing", th.details.facts.text())
        self.assertIn("Ana, Ben +1 playing", th.details.band_facts.text())
        FRIENDS.set_answer(answer(live_crowd(), updated=iso(11 * 60)))   # stale
        th._tick()
        self.assertNotIn("playing", th.details.facts.text())
        self.assertNotIn("playing", nx.fact_text(self.win.game('steam-10')))
        th.close_game()
        self.done()

    def test_sort_filter_friends_own(self):
        th = self.theatre()
        th.tabs.set_current('library', emit=True)
        QApplication.processEvents()
        sheet = self.win.theatre_sheet
        th.set_library_state({'sort': 'name', 'show': 'friends'})
        self.assertEqual([g['slug'] for g in th._lib_games()], ['steam-10'])
        self.assertIn("Friends own", th.lib_chip.text())
        sheet.close_sheet(restore=False)
        # Without friends data it falls back to Installed and isn't offered
        FRIENDS.set_answer(answer(live_crowd(), {'kind': 'no-key', 'message': 'x'}))
        QApplication.processEvents()
        self.assertIn("Installed", th.lib_chip.text())
        th.lib_state = {'sort': 'name', 'show': 'installed'}
        chosen = []
        with mock.patch.object(sheet, 'choose', lambda t, items, **k: chosen.extend(items)):
            th.open_library_sheet(sheet)
        self.assertNotIn("Friends own", [i[0] for i in chosen if isinstance(i, tuple)])
        self.done()
        # Prefs keep it; an unknown value reads as Installed
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        f = tmp / 'library.json'
        f.write_text(json.dumps({'theatre_library': {'sort': 'most', 'show': 'friends'}}))
        with mock.patch.object(nx.LibraryPrefs, 'PATH', f):
            self.assertEqual(nx.LibraryPrefs().theatre_library, {'sort': 'most',
                                                                 'show': 'friends',
                                                                 'view': 'browse'})
        f.write_text(json.dumps({'theatre_library': {'show': 'bogus'}}))
        with mock.patch.object(nx.LibraryPrefs, 'PATH', f):
            self.assertEqual(nx.LibraryPrefs().theatre_library['show'], 'installed')


# ---- w-83 ----
def together_crowd():
    """Ana in app 10 now, owns 10, 20, 30; Ben owns 10 and 20; Cy owns 30;
    Dee owns nothing of yours; Fay is private."""
    return [playing(1, "Ana", 10, 80, {10: 600, 20: 100, 30: 5}),
            friend(2, "Ben", {10: 300, 20: 50}), friend(3, "Cy", {30: 10}), friend(4, "Dee"),
            friend(5, "Fay", visible=False)]


class _Together(_FriendsWindow):
    """Play together (w-83) on the friends window: steam-30 is single-player
    only, steam-10 has co-op, steam-20 (not installed) has no modes known."""

    def setUp(self):
        super().setUp()
        self.win.prefs.library_state = {}
        self.win._facts_idx = {'games': {'steam-10': {'modes': ['single', 'coop']},
                                         'steam-30': {'modes': ['single']}}}
        self.reply = answer(together_crowd())
        self.win._reload_friends()
        QApplication.processEvents()

    def form(self, modal=None):
        from nexus.gui.friends import PlayTogetherForm
        m = modal or self.win.modal
        self.assertTrue(m.isVisible())
        return next(f for f in m.findChildren(PlayTogetherForm) if not f.isHidden())

    def row(self, form, name):
        return next(r for r in form.rows if r.name == name)

    def pick_index(self):
        from nexus.gui.win_library import PICK_FRIENDS
        return self.win.filter.findData(PICK_FRIENDS)


class PlayTogether(_Together):
    def test_form_order_private_last_summary_count(self):
        w = self.win
        w.go(w.LIBRARY)
        w._play_together()
        m, f = w.modal, self.form()
        self.assertEqual(m.title.text(), "Play together")
        self.assertLessEqual(m.card.width(), 600)
        self.assertEqual([r.name for r in f.rows], ["Ana", "Ben", "Cy", "Dee", "Fay"])
        self.assertEqual(self.row(f, "Ana").sub.text(), "Playing Steam 10")
        self.assertEqual(self.row(f, "Ben").sub.text(), "1 game in common with you")
        self.assertEqual(self.row(f, "Dee").sub.text(), "0 games in common with you")
        fay = self.row(f, "Fay")
        self.assertFalse(fay.isEnabled())
        self.assertIn("private", [lab.text() for lab in fay.findChildren(QLabel)])
        self.assertIsNone(f.find)                                    # five friends
        self.assertIs(QApplication.focusWidget(), f.rows[0])         # the first friend
        self.assertIs(m.default_focus(), f.rows[0])
        self.assertEqual(f.mode.current(), 'multi')                  # the default
        self.assertEqual(m.primary.text(), "Show games")
        self.assertFalse(m.primary.isEnabled())
        self.assertFalse(m.btn_extra.isEnabled())
        self.assertEqual(m.btn_extra.text(), "Save as shelf")
        self.row(f, "Ana").click()
        # Installed only (the pool is what the grid shows): 10 and 30, and
        # 30 is single-player only
        self.assertEqual(f.count.text(), "1 game you and Ana both own")
        self.assertEqual(m.primary.text(), "Show 1 game")
        self.assertTrue(m.primary.isEnabled())
        self.assertTrue(m.btn_extra.isEnabled())
        f.mode.set_current('any', emit=True)
        self.assertEqual(f.count.text(), "2 games you and Ana both own")
        self.row(f, "Ben").click()
        self.assertEqual(f.count.text(), "1 game you, Ana and Ben all own")
        self.assertEqual(f.key(), f"friends:{sid(1)},{sid(2)}")
        self.assertEqual(len([c for c in f.strip_host.findChildren(QLabel)
                              if c.isVisibleTo(f)]), 1)              # one cover
        self.row(f, "Cy").click()
        self.assertEqual(f.count.text(), "No games you, Ana, Ben and Cy all own")
        self.assertFalse(m.primary.isEnabled())
        m.cancel()

    def test_find_field_only_above_twelve(self):
        w = self.win
        self.reply = answer([friend(i, f"P{i:02d}", {10: i + 1}) for i in range(13)])
        w._reload_friends()
        w.go(w.LIBRARY)
        w._play_together()
        f = self.form()
        self.assertIsNotNone(f.find)
        self.assertIs(QApplication.focusWidget(), f.rows[0])        # a friend, not the field
        f.find.setText("p1")
        self.assertEqual([r.name for r in f.rows if not r.isHidden()],
                         ["P10", "P11", "P12"])
        f.find.setText("zz")
        self.assertFalse(f.empty.isHidden())
        f.find.setText("")
        self.assertEqual(len([r for r in f.rows if not r.isHidden()]), 13)
        w.modal.cancel()

    def test_entry_opens_picker_and_combo_reverts(self):
        w = self.win
        w.go(w.LIBRARY)
        QApplication.processEvents()
        i = self.pick_index()
        self.assertGreater(i, 0)
        self.assertEqual(w.filter.itemText(i).strip(), "Play together…")
        w.filter.setCurrentIndex(i)
        self.assertEqual(w.filter.currentData(), 'all')              # back at once
        self.assertNotEqual(w.prefs.library_state.get('status'), w.filter.itemData(i))
        self.assertFalse(w.modal.isVisible())
        QApplication.processEvents()                                 # opens after the popup
        self.assertTrue(w.modal.isVisible())
        self.form()
        w.modal.cancel()
        # Nobody public: no entry
        self.reply = answer([friend(5, "Fay", visible=False)])
        w._reload_friends()
        w.apply_filter()
        self.assertEqual(self.pick_index(), -1)

    def test_gamedb_only_people_offer_no_picker(self):
        w = self.win
        w.go(w.LIBRARY)
        self.reply = merged([friend(5, "Fay", visible=False)], [gdb_row('ana', "Ana")])
        w._reload_friends()
        w.apply_filter()
        QApplication.processEvents()
        self.assertEqual(self.pick_index(), -1)
        w._play_together()
        self.assertFalse(w.modal.isVisible())

    def test_show_sets_filter_badge_pencil_reopens(self):
        w = self.win
        w.go(w.LIBRARY)
        w.set_view('grid', save=False)
        w._set_chip('steam')
        w._play_together()
        f = self.form()
        for name in ("Ana", "Ben"):
            self.row(f, name).click()
        key = f.key()
        self.assertEqual(key, f"friends:{sid(1)},{sid(2)}:multi")
        w.modal.primary.click()
        QApplication.processEvents()
        self.assertFalse(w.modal.isVisible())
        self.assertEqual(w.filter.currentData(), key)
        self.assertEqual(w.filter.currentText().strip(), "Play together: Ana and Ben")
        self.assertEqual(w._chip, 'all')                             # the whole library
        self.assertEqual(sorted(g['slug'] for g in w._shown_games), ['steam-10'])
        self.assertTrue(w.library.friends_badge)
        self.assertEqual(w.prefs.library_state['status'], key)
        self.assertFalse(w.btn_edit_shelf.isHidden())
        self.assertEqual(w.btn_edit_shelf.toolTip(), "Change who you play with")
        w.btn_edit_shelf.click()
        f = self.form()
        self.assertEqual([r.name for r in f.rows if r.isChecked()], ["Ana", "Ben"])
        self.assertEqual(f.mode.current(), 'multi')
        self.assertEqual(w.modal.primary.text(), "Show 1 game")
        w.modal.cancel()
        w._clear_filter()
        self.assertFalse(w.library.friends_badge)
        self.assertTrue(w.btn_edit_shelf.isHidden())

    def test_save_as_shelf_adds_rule(self):
        w = self.win
        w.go(w.LIBRARY)
        w._play_together()
        f = self.form()
        self.row(f, "Ana").click()
        w.modal.btn_extra.click()
        self.assertFalse(w.modal.isVisible())
        shelf = w.prefs.user_shelves()[-1]
        self.assertEqual(shelf['name'], "Play with Ana")
        self.assertEqual(shelf['rules'], [{'field': 'friends', 'op': 'all_multi',
                                          'value': [sid(1)]}])
        key = 'user:' + shelf['id']
        self.assertEqual([g['slug'] for g in w._smart_games(key, w._smart_pool())],
                         ['steam-10'])
        self.assertIn("Ana", w._smart_tip(key))
        # Ana unfriended: the rule has nobody left and is skipped, nothing raises
        self.reply = answer([r for r in together_crowd() if r['name'] != "Ana"])
        w._reload_friends()
        w.apply_filter()
        self.assertEqual(w._smart_games(key, w._shelf_pool()), [])
        self.assertEqual(w._smart_tip(key),
                         "Friends own (all of) 1 friend, more than one player")

    def test_filter_falls_back_when_friends_off(self):
        w = self.win
        w.go(w.LIBRARY)
        key = f"friends:{sid(2)}"
        w._show_filter(key)
        self.assertEqual(w.filter.currentData(), key)
        self.assertEqual(sorted(g['slug'] for g in w._shown_games), ['steam-10'])
        w.config['friends_enabled'] = False
        w._load_friends()
        self.assertEqual(w.filter.currentData(), 'all')
        self.assertEqual(self.pick_index(), -1)
        w.config['friends_enabled'] = True
        w._reload_friends()
        w._show_filter(key)
        self.assertEqual(w.filter.currentData(), key)
        w._friends_done(answer(together_crowd(), {'kind': 'no-key', 'message': 'x'}), '')
        self.assertEqual(w.filter.currentData(), 'all')
        self.assertNotEqual(w.prefs.library_state.get('status'), key)


class TheatreTogether(_Together):
    def test_theatre_modal_focus_on_first_row_a_and_b(self):
        from PyQt6.QtTest import QTest
        w = self.win
        w.set_theatre(True)
        QApplication.processEvents()
        th = w.theatre
        th.tabs.set_current('library', emit=True)
        QApplication.processEvents()
        chosen = []
        with mock.patch.object(w.theatre_sheet, 'choose',
                               lambda t, items, **k: chosen.extend(items)):
            th.open_library_sheet(w.theatre_sheet)
        row = next(i for i in chosen if isinstance(i, tuple) and i[0] == "Play together…")
        row[2]()                                             # A on it
        QApplication.processEvents()
        m = w.theatre_modal
        f = self.form(m)
        self.assertEqual(f.scale, nx.THEATRE_SCALE)
        self.assertTrue(m.btn_extra.isHidden())              # no Save as shelf on the TV
        first = f.rows[0]
        self.assertIs(QApplication.focusWidget(), first)
        w._update_hints()
        self.assertEqual(w.hints._shown[1][0], ('a', 'Add'))
        QTest.keyClick(first, Qt.Key.Key_Space)              # the pad's A
        self.assertTrue(first.isChecked())
        self.assertEqual(w.hints._shown[1][0], ('a', 'Remove'))
        # Everything counts in Theatre's Library: 10 and 20 (unknown modes stay)
        self.assertEqual(f.count.text(), "2 games you and Ana both own")
        m.primary.click()
        QApplication.processEvents()
        self.assertEqual(th.lib_state['show'], f"friends:{sid(1)}:multi")
        self.assertEqual(w.prefs.theatre_library['show'], f"friends:{sid(1)}:multi")
        self.assertEqual(sorted(g['slug'] for g in th._lib_games()), ['steam-10', 'steam-20'])
        self.assertIn("Play together", th.lib_chip.text())
        self.assertEqual(w.filter.currentData(), 'all')      # the desktop filter is untouched
        # Again, then B cancels
        th._ask_together()
        QApplication.processEvents()
        f = self.form(m)
        self.assertEqual([r.name for r in f.rows if r.isChecked()], ["Ana"])
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Escape)
        self.assertFalse(m.isVisible())
        # Without friends data the show falls back to Installed
        FRIENDS.set_answer(answer(together_crowd(), {'kind': 'no-key', 'message': 'x'}))
        QApplication.processEvents()
        self.assertIn("Installed", th.lib_chip.text())
        w.set_theatre(False)
        QApplication.processEvents()
