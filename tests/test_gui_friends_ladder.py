"""The Friends widget ladder (w-161): Now, Play together, Friends' picks,
quiet, at Small, Wide and Full, from nexus.social.friends_digest. The widget
only draws what the digest says; these check the texts, the one action on a
row, the setup, stale and quiet states, the `lead` option, growth and pad
focus. Offscreen, no network."""

import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

try:
    from test_gui_layout import nx
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx
from tests.patching import patch_gui  # noqa: E402,F401  (the sandbox's order)

if nx is not None:
    from PyQt6.QtWidgets import QApplication, QLabel, QPushButton
    from nexus.gui import dashboard as D
    from nexus.gui.friends import FRIENDS
    try:
        from test_gui_friends import answer, friend, iso, playing, sid
    except ImportError:
        from tests.test_gui_friends import answer, friend, iso, playing, sid

GAMES = [{'slug': 'steam-10', 'display_name': 'Ten', 'source': 'steam', 'appid': 10,
          'playtime_minutes': 120},
         {'slug': 'steam-20', 'display_name': 'Twenty', 'source': 'steam', 'appid': 20},
         {'slug': 'steam-30', 'display_name': 'Thirty', 'source': 'steam', 'appid': 30,
          'playtime_minutes': 60}]


def online(i, name, owned=None, visible=True):
    r = friend(i, name, owned, visible)
    r['evidence'] = {'personastate': 1}
    return r


def in_game(i, name, appid, mins, owned=None):
    r = playing(i, name, appid, mins, owned)
    r['evidence'] = {'personastate': 1}
    return r


class Ladder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if nx is None:
            raise unittest.SkipTest("PyQt6 not available")

    def setUp(self):
        FRIENDS.reset()
        self.addCleanup(FRIENDS.reset)
        self.opened = []
        p = mock.patch.object(D, 'open_url', self.opened.append)
        p.start()
        self.addCleanup(p.stop)
        self.hub = SimpleNamespace(
            config={'friends_enabled': True}, _games=[dict(g) for g in GAMES],
            _smart_facts=lambda: {}, dealspage=SimpleNamespace(_data=lambda: {'wishlist': []}),
            game=lambda slug: next((g for g in self.hub._games if g['slug'] == slug), None),
            _reveal_game=mock.Mock(), _play_lead=mock.Mock(), _play_together=mock.Mock(),
            _open_settings=mock.Mock(), _reload_friends=mock.Mock())

    def widget(self, size, rows, error=None, updated=None, show='auto', editing=False):
        FRIENDS.set_answer(answer(rows, error, updated=updated))
        w = D.FriendsWidget()
        self.addCleanup(w.deleteLater)
        w.size_, w.options = size, {'lead': show}
        w.set_editing(editing, size)
        w.fill(self.hub)
        return w

    @staticmethod
    def texts(w):
        return [lb.text() for lb in w.findChildren(QLabel) if lb.text()]

    def nowish(self):
        return [in_game(1, "Ana", 10, 42, {10: 600}), in_game(2, "Ben", 10, 42, {10: 300}),
                in_game(3, "Dee", 99, 95), online(7, "Gus", {30: 3600}),
                friend(8, "Hal", {20: 2400}, visible=False)]

    def together(self):
        return [online(1, "Ana", {20: 600}), online(2, "Ben", {20: 300, 30: 100}),
                friend(3, "Cy", {10: 60 * 64})]

    def picks(self):
        return [friend(1, "Ana", {10: 60 * 40}), friend(2, "Ben", {30: 60 * 64})]

    # ---- Now ----
    def test_now_small_says_who_where_and_for_how_long(self):
        w = self.widget('small', self.nowish())
        self.assertEqual(w.state, 'now')
        self.assertEqual(w.title_label.text(), "Friends")
        t = self.texts(w)
        self.assertIn("4 online", t)
        self.assertIn("Ana and Ben are in Ten", t)
        self.assertIn("42 min · in your library", t)
        self.assertIn("Dee is in Steam 99", t)
        self.assertIn("1 h 35 min · not in your library", t)
        self.assertEqual([r.button.text() for r in w.cards], ["Play", "Steam page"])
        self.assertIn("4 of 5 friends share their games", t)

    def test_now_actions_play_the_game_or_open_the_store(self):
        w = self.widget('wide', self.nowish())
        rows = [r for r in w.cards if r.key[0] == 'now']
        self.assertEqual([r.key for r in rows], [('now', 10), ('now', 99)])
        rows[0].button.click()
        self.hub._play_lead.assert_called_once_with(self.hub._games[0])
        rows[1].button.click()
        self.assertEqual(self.opened, ["https://store.steampowered.com/app/99/"])
        rows[0].clicked.emit()                       # the row itself opens the page
        self.hub._reveal_game.assert_called_once_with('steam-10')

    def test_wide_shows_the_lower_rungs_under_now(self):
        rows = self.nowish() + [friend(9, "Ivy", {20: 60 * 41})]
        w = self.widget('wide', rows)
        t = self.texts(w)
        self.assertIn("Right now", t)
        self.assertIn("Friends' picks", t)
        pick = next(r for r in w.cards if r.key == ('pick', 20))
        self.assertEqual(pick.button.text(), "Open")
        self.assertIn("Ivy 41 h · you haven't started",
                      [lb.text() for lb in pick.findChildren(QLabel)])

    # ---- Play together ----
    def test_together_small_and_its_actions(self):
        w = self.widget('small', self.together())
        self.assertEqual(w.state, 'together')
        t = self.texts(w)
        self.assertIn("2 online", t)
        self.assertIn("Ana and Ben are online", t)
        self.assertIn("Nobody is in a game · you all own 1 game", t)
        row = w.cards[0]
        self.assertEqual(row.key, ('together', 20))
        self.assertEqual(row.button.text(), "Play together")
        row.button.click()
        self.hub._play_together.assert_called_once_with(f"friends:{sid(1)},{sid(2)}:multi")

    def test_together_wide_has_play_on_the_other_games(self):
        w = self.widget('wide', [online(1, "Ana", {20: 600, 30: 10}),
                                 online(2, "Ben", {20: 300, 30: 10})])
        self.assertEqual([r.button.text() for r in w.cards if r.key[0] == 'together'],
                         ["Play together", "Play"])

    # ---- Friends' picks ----
    def test_picks_when_nobody_is_on(self):
        w = self.widget('small', self.picks())
        self.assertEqual(w.state, 'picks')
        self.assertIn("Nobody's on", self.texts(w))
        self.assertEqual([r.key for r in w.cards], [('pick', 30), ('pick', 10)])
        self.assertEqual(w.cards[0].button.text(), "Open")
        w.cards[0].button.click()
        self.hub._reveal_game.assert_called_once_with('steam-30')
        self.assertIn("Ben 64 h · you 1 h",
                      [lb.text() for lb in w.cards[0].findChildren(QLabel)])

    def test_wide_and_full_say_what_the_picks_are(self):
        w = self.widget('wide', self.picks())
        self.assertIn("Games in your library your friends put hours into", self.texts(w))
        f = self.widget('full', self.picks())
        self.assertIn("Games in your library your friends put hours into · nobody is on "
                      "right now", self.texts(f))

    def test_full_lists_online_friends_with_message_and_a_wishlist_line(self):
        self.hub.dealspage = SimpleNamespace(_data=lambda: {'wishlist': [
            {'appid': 777, 'title': "Silksong"}]})
        rows = self.nowish()
        rows[3]['owned']['777'] = 60
        w = self.widget('full', rows)
        self.assertIn("Gus owns Silksong, on your wishlist", self.texts(w))
        msg = [b for b in w.findChildren(QPushButton) if b.text() == "Message"]
        self.assertEqual(len(msg), 4)                # everyone online (Hal is private)
        msg[0].click()
        self.assertEqual(self.opened, [f"steam://friends/message/{sid(1)}"])
        store = next(b for b in w.findChildren(QPushButton) if b.text() == "Store page")
        store.click()
        self.assertEqual(self.opened[-1], "https://store.steampowered.com/app/777/")

    # ---- quiet, setup, stale ----
    def test_quiet_leaves_home_but_customise_still_shows_it(self):
        w = self.widget('wide', [friend(1, "Ana")])
        self.assertEqual(w.state, 'quiet')
        self.assertFalse(w.has_content(self.hub))
        self.assertIn("Nothing to show yet", self.texts(w))
        self.assertIn("Shows when a friend is on, or plays games you own.", self.texts(w))

    def test_setup_and_loading(self):
        self.hub.config['friends_enabled'] = False
        FRIENDS.reset()
        w = D.FriendsWidget()
        self.addCleanup(w.deleteLater)
        w.size_, w.options = 'small', {'lead': 'auto'}
        w.fill(self.hub)
        self.assertEqual(w.state, 'setup')
        self.assertFalse(w.has_content(self.hub))
        self.assertIn("See what your Steam friends play.", self.texts(w))
        next(b for b in w.findChildren(QPushButton) if b.text() == "Set up in Settings").click()
        self.hub._open_settings.assert_called_once_with('accounts', 'friends')
        self.hub.config['friends_enabled'] = True
        w.fill(self.hub)
        self.assertEqual(w.state, 'loading')

    def test_stale_presence_keeps_the_picks_and_says_since_when(self):
        ago = 3 * 3600
        rows = [in_game(1, "Ana", 10, 5, {10: 60 * 40})]
        w = self.widget('wide', rows, {'kind': 'offline', 'message': 'x'}, updated=iso(ago))
        self.assertEqual(w.state, 'picks')
        t = self.texts(w)
        self.assertIn("Offline", t)
        at = datetime.fromtimestamp(time.time() - ago).strftime('%H:%M')
        self.assertIn(f"Presence as of {at} · Steam is not answering", t)
        self.assertEqual([r.key for r in w.cards], [('pick', 10)])

    def test_bad_key_is_an_error_card_that_shows_on_home(self):
        w = self.widget('wide', self.picks(), {'kind': 'bad-key', 'message': 'x'})
        self.assertTrue(w.has_content(self.hub))
        self.assertTrue(any("Steam refused the key" in t for t in self.texts(w)))

    # ---- the lead option, titles ----
    def test_show_now_keeps_the_old_widget(self):
        w = self.widget('full', self.nowish(), show='now')
        self.assertEqual(w.title_label.text(), "Friends playing now")
        self.assertEqual(w.state, 'live')
        self.assertEqual([c.appid for c in w.cards], [10, 99])
        n = self.widget('wide', self.picks(), show='now')
        self.assertFalse(n.has_content(self.hub))        # nobody in a game: it leaves
        self.assertEqual(self.widget('wide', self.picks()).title_label.text(), "Friends")

    def test_the_spec_names_the_option_and_the_card(self):
        spec = D.SPEC['friends']
        self.assertEqual(spec.title, "Friends")
        self.assertEqual([v for v, _l in spec.options['lead']], ['auto', 'now'])
        self.assertTrue(spec.grows)

    # ---- grows, pad focus ----
    def test_more_picks_come_with_height_not_with_a_taller_card(self):
        rows = [friend(1, "Ana", {10 + i: 60 * (30 + i) for i in range(8)})]
        self.hub._games = [{'slug': f"steam-{10 + i}", 'display_name': f"G{i}", 'source': 'steam',
                            'appid': 10 + i} for i in range(8)]
        w = self.widget('wide', rows)
        self.assertEqual(len(w.cards), D.FriendsWidget.PICK_ROWS)
        w.resize(520, 10)
        w.show()
        QApplication.processEvents()
        nat = w.layout().totalHeightForWidth(520)
        w.resize(520, nat + 400)
        for _ in range(3):
            QApplication.processEvents()
        self.assertGreater(w.more.shown, 0)
        self.assertEqual(w.layout().totalHeightForWidth(520), nat)
        self.assertEqual(len(w.cards), D.FriendsWidget.PICK_ROWS)

    def test_a_presence_tick_keeps_rows_and_focus_and_moves_the_minutes(self):
        rows = self.nowish()
        w = self.widget('wide', rows)
        w.show()
        row = w.cards[0]
        row.button.setFocus()
        QApplication.processEvents()
        rows[0]['playing']['since'] -= 600
        FRIENDS.set_answer(answer(rows))
        w.fill(self.hub)
        self.assertIs(w.cards[0], row)                           # nothing rebuilt
        self.assertEqual(row.sub_label.text(), "52 min · in your library")
        rows.append(in_game(9, "Zed", 10, 1))                    # someone joins: rebuilt
        FRIENDS.set_answer(answer(rows))
        w.fill(self.hub)
        self.assertIsNot(w.cards[0], row)
        QApplication.processEvents()
        self.assertIs(QApplication.focusWidget(), w.cards[0].button)
        self.assertEqual(w.cards[0].key, ('now', 10))

    # ---- the gallery ----
    def test_preview_seed_shows_the_ladder_with_sample_data(self):
        from nexus.gui import widget_preview as wp
        from PyQt6.QtCore import QSize
        from PyQt6.QtWidgets import QWidget
        bench = QWidget()
        self.addCleanup(bench.deleteLater)
        hub = wp.PreviewHub(bench, datetime.now().date())
        for size in ('small', 'wide'):
            pm, real = wp.render('friends', size, QSize(278, 84), bench, hub)
            self.assertTrue(real, size)
        with wp.sample_world(hub):
            w = D.FriendsWidget(bench)
            self.addCleanup(w.deleteLater)
            w.size_, w.options = 'wide', {'lead': 'auto'}
            w.bind(hub)
            w.preview_seed(hub)
            w.fill(hub)
            self.assertEqual(w.state, 'now')
            self.assertTrue(w.cards)
            self.assertEqual(w.why(hub), "3 friends playing now")


if __name__ == '__main__':
    unittest.main()
