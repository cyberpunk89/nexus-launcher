"""Operational behaviour of the main window: what survives a failed reload,
what a game being busy or running allows, duplicate jobs, confirmations,
search. Offscreen, one window for the class, every CLI call stubbed."""

import unittest
from unittest import mock

try:
    from test_gui_layout import nx
    from test_gui_window import fake_games
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx
    from tests.test_gui_window import fake_games

if nx is not None:
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QApplication


@unittest.skipIf(nx is None, "PyQt6 not available")
class Gates(unittest.TestCase):
    def keys(self, g):
        return {a.key: a for a in nx.game_actions(g)}

    def test_running_game_cant_be_uninstalled_or_restored(self):
        g = {'slug': 'a', 'source': 'nexus', 'launcher': '/a', 'playing': True}
        acts = self.keys(g)
        for k in ('uninstall', 'restore-saves'):
            self.assertFalse(acts[k].enabled, k)
            self.assertIn("running", acts[k].label)
        self.assertTrue(acts['tool:kill'].enabled)          # the way out stays open
        self.assertFalse(nx.play_enabled(g))

    def test_busy_game_offers_no_second_job(self):
        g = {'slug': 'a', 'source': 'nexus', 'launcher': '/a', 'busy': True}
        acts = self.keys(g)
        self.assertFalse(acts['uninstall'].enabled)
        self.assertFalse(acts['add-steam'].enabled)
        self.assertTrue(acts['favorite'].enabled)
        self.assertFalse(nx.play_enabled(g))
        self.assertEqual(nx.play_label(g), "Working…")

    def test_idle_game_is_untouched(self):
        g = {'slug': 'a', 'source': 'nexus', 'launcher': '/a'}
        self.assertTrue(all(a.enabled for a in nx.game_actions(g)))


@unittest.skipIf(nx is None, "PyQt6 not available")
class Widgets(unittest.TestCase):
    def test_wheel_passes_over_an_unfocused_combo(self):
        cb = nx.NoWheelCombo()
        cb.addItems(["a", "b", "c"])
        fired = []
        cb.activated.connect(fired.append)
        ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120),
                         Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(cb, ev)
        self.assertEqual(cb.currentIndex(), 0)
        self.assertEqual(fired, [])
        self.assertFalse(ev.isAccepted())                 # on to the scroll area
        cb.deleteLater()

    def test_toast_action_runs_once(self):
        calls = []
        t = nx.Toast("x", 'info', ("Retry", lambda: calls.append(1)))
        b = t._buttons[0]
        b.click()
        b.click()
        self.assertEqual(calls, [1])
        t.deleteLater()

    def test_error_toasts_stay(self):
        host_parent = nx.QWidget()
        host = nx.ToastHost(host_parent)
        host.show_toast("boom", 'error')
        t = host.lay.itemAt(0).widget()
        self.assertIsNone(t._timer)
        host_parent.deleteLater()


@unittest.skipIf(nx is None, "PyQt6 not available")
class Behaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (nx.cli_json, nx.LibraryPrefs.save, nx.CliQuery)
        nx.cli_json = lambda *a, **k: None
        nx.LibraryPrefs.save = lambda self: None
        nx.CliQuery = lambda *a, **k: None
        cls.win = nx.MainWindow()
        cls.win.resize(1300, 850)
        cls.win.show()

    @classmethod
    def tearDownClass(cls):
        cls.win.jobs.jobs.clear()
        cls.win.close()
        cls.win.deleteLater()
        QApplication.processEvents()
        del cls.win
        nx.cli_json, nx.LibraryPrefs.save, nx.CliQuery = cls._saved

    def setUp(self):
        self.win._lib_inflight = self.win._lib_again = False
        self.win._playing, self.win._launching = set(), {}
        self.win._on_library({'games': fake_games(), 'disks': []}, '')
        self.toasts = []
        self.win.toast = lambda text, kind='info', action=None, timeout=0: \
            self.toasts.append((text, kind, action))

    def tearDown(self):
        del self.win.toast
        self.win.jobs.jobs.clear()
        self.win._busy = frozenset()

    def test_failed_reload_keeps_the_library(self):
        self.win._playing = {'alpha'}
        self.win._on_library(None, "timed out")
        self.assertEqual(len(self.win._games), 3)
        self.assertEqual(self.toasts[-1][1], 'error')
        self.assertEqual(self.toasts[-1][2][0], "Retry")
        stopped = []
        with mock.patch.object(self.win, '_game_stopped', stopped.append):
            # The running poll still sees alpha's entry: nothing "stops".
            self.win._apply_running({'alpha'})
        self.assertEqual(stopped, [])

    def test_reloads_coalesce(self):
        calls = []
        def fake(args, cb, *a, **k):
            if args[0] == '--library':
                calls.append(cb)
        with mock.patch.object(nx, 'cli_json', fake):
            self.win.reload_library()
            self.win.reload_library()
            self.win.reload_library()
            self.assertEqual(len(calls), 1)               # one scan out at a time
            calls[0]({'games': fake_games()}, '')
            QApplication.processEvents()                  # the queued follow-up
            self.assertEqual(len(calls), 2)
            calls[1]({'games': fake_games()}, '')
            QApplication.processEvents()
            self.assertEqual(len(calls), 2)

    def test_same_job_twice_is_refused(self):
        with mock.patch.object(nx.Job, 'start', lambda self: None):
            self.win.run(['--refetch-art', 'alpha'], "Fetching artwork", 'alpha')
            self.win.run(['--refetch-art', 'alpha'], "Fetching artwork", 'alpha')
        self.assertEqual(len(self.win.jobs.jobs), 1)
        self.assertIn("already queued", self.toasts[-1][0])

    def test_busy_game_is_marked_and_guarded(self):
        with mock.patch.object(nx.Job, 'start', lambda self: None):
            self.win.run(['--check-integrity', 'alpha'], "Checking", 'alpha')
        self.assertTrue(self.win.game('alpha')['busy'])
        self.win.on_game_action('uninstall', self.win.game('alpha'))
        self.assertIn("busy", self.toasts[-1][0])

    def test_restore_saves_asks_first(self):
        ran = []
        with mock.patch.object(self.win, 'run', lambda *a, **k: ran.append(a)):
            self.win.on_game_action('restore-saves', self.win.game('alpha'))
            self.assertEqual(ran, [])
            sheet = self.win.sheet
            self.assertTrue(sheet.isVisible())
            yes = next(b for b in sheet.findChildren(nx.QPushButton)
                       if b.text() == "Restore saves")
            self.assertTrue(any(b.text() == "Cancel"
                                for b in sheet.findChildren(nx.QPushButton)))
            yes.click()
        self.assertFalse(self.win.sheet.isVisible())
        self.assertEqual(ran[0][0], ['--restore-saves', 'alpha'])

    def test_launch_that_never_shows_up_is_cleared(self):
        self.win._launching['alpha'] = 0.0                # long ago
        self.win._mark_states()
        self.assertEqual(nx.play_label(self.win.game('alpha')), "Starting…")
        self.win._poll_running()
        self.assertNotIn('alpha', self.win._launching)
        self.assertEqual(nx.play_label(self.win.game('alpha')), "Play")
        self.assertIn("doesn't seem to have started", self.toasts[-1][0])

    def test_open_jobs_dont_claim_a_launch(self):
        job = nx.Job(['x'], "Opening Beta in Steam", 'steam-1', 'open')
        job.state = 'done'
        self.win._job_done(job)
        self.assertEqual(self.toasts, [])

    def test_rebuilt_shelves_keep_the_focused_game(self):
        w = self.win
        w.go(w.HOME)
        w.activateWindow()
        QApplication.processEvents()
        tiles = [t for t in w.homeview.findChildren(nx.GameTile)
                 if not t.isHidden() and not t.pending]
        self.assertTrue(tiles)
        tile = tiles[-1]
        slug = tile.game['slug']
        tile.setFocus()
        QApplication.processEvents()
        w._home_sig = None                                # force a full rebuild
        w._refresh_home()
        for _ in range(3):
            QApplication.processEvents()
        f = QApplication.focusWidget()
        self.assertIsInstance(f, nx.GameTile)
        self.assertIsNot(f, tile)                         # a new tile…
        self.assertEqual(f.game['slug'], slug)            # …for the same game

    def test_library_waits_while_off_screen(self):
        w = self.win
        w.go(w.HOME)
        w.apply_filter()
        self.assertTrue(w._filter_dirty)
        w.go(w.LIBRARY)
        self.assertFalse(w._filter_dirty)
        w.go(w.HOME)

    def test_back_and_forward(self):
        w = self.win
        w.go(w.HOME)
        w.go(w.LIBRARY)
        w.go(w.SETTINGS)
        self.assertTrue(w.go_back())
        self.assertEqual(w.current(), w.LIBRARY)
        self.assertTrue(w.go_forward())
        self.assertEqual(w.current(), w.SETTINGS)
        w._escape()                                       # a side page: back, not Home
        self.assertEqual(w.current(), w.LIBRARY)
        w.go(w.HOME)

    def test_settings_edits_survive_leaving(self):
        w = self.win
        w.go(w.SETTINGS)
        w.settings.scan_more.setText("~/Elsewhere")
        self.assertTrue(w.settings.dirty)
        w.go(w.HOME)
        self.assertIn("aren't saved", self.toasts[-1][0])
        w.go(w.SETTINGS)
        self.assertEqual(w.settings.scan_more.text(), "~/Elsewhere")
        w.settings.load()                                 # Discard
        self.assertFalse(w.settings.dirty)
        w.go(w.HOME)

    def test_controls_reach_theatre_and_hints(self):
        w = self.win
        w._apply_controls({'pad_a': 'play', 'double_click': 'details'})
        self.assertTrue(w.theatre.a_plays)
        self.assertFalse(w.theatre.double_plays)
        acts = []
        with mock.patch.object(w, 'on_game_action', lambda a, g: acts.append(a)):
            w._tile_activated(w.game('alpha'))
            w._tile_double(w.game('alpha'))
        self.assertEqual(acts, ['launch'])                # A plays, double click opens
        w._apply_controls({'pad_a': 'details', 'double_click': 'play'})

    def test_install_cancel_asks_first(self):
        job = nx.Job(['x'], "Installing T", 't', 'install', 'T')
        job.state = 'running'
        card = nx.ActivityCard(job)
        self.win.activity.list.insertWidget(0, card)      # inside the window
        cancelled = []
        with mock.patch.object(job, 'cancel', lambda: cancelled.append(1)):
            card.btn_cancel.click()
            self.assertEqual(cancelled, [])
            self.assertTrue(self.win.sheet.isVisible())
            next(b for b in self.win.sheet.findChildren(nx.QPushButton)
                 if b.text() == "Stop install").click()
        self.assertEqual(cancelled, [1])
        card.deleteLater()

    def test_search_matches_words_not_punctuation(self):
        games = fake_games()
        games[0]['display_name'] = "The Witcher® 3: Wild Hunt"
        self.win._on_library({'games': games, 'disks': []}, '')
        self.win.go(self.win.LIBRARY)
        self.win.search.setText("witcher 3")
        self.win.apply_filter()
        shown = self.win._shown_view()
        self.assertEqual(set(shown.tiles), {'alpha'})
        self.win.search.clear()
        self.win.apply_filter()


@unittest.skipIf(nx is None, "PyQt6 not available")
class Storage(unittest.TestCase):
    def test_delete_forever_deletes_what_was_confirmed(self):
        page = nx.StoragePage()
        sent = []
        page.request.connect(lambda args, *_: sent.append(args))
        a = {'id': 'a', 'size': 10}
        b = {'id': 'b', 'size': 20}
        sel = [a]
        with mock.patch.object(page, '_selected', lambda: list(sel)):
            page._ask_forever()
            sel.append(b)                                 # ticked after the question
            page._clean(True)
        self.assertEqual(sent[0][:2], ['--cleanup', 'a'])
        self.assertNotIn('b', sent[0])
        page.deleteLater()


if __name__ == '__main__':
    unittest.main()
