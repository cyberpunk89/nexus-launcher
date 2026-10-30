"""Operational behaviour of the main window: what survives a failed reload,
what a game being busy or running allows, duplicate jobs, confirmations,
search. Offscreen, one window for the class, every CLI call stubbed."""

import time
import unittest
from unittest import mock

try:
    from test_gui_layout import nx, stop_leftovers
    from test_gui_window import fake_games
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, stop_leftovers
    from tests.test_gui_window import fake_games
from tests.patching import linux_features, patch_gui, set_gui  # noqa: E402

if nx is not None:
    from PyQt6.QtCore import Qt, QPoint, QPointF, QProcess
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QWidget


@unittest.skipIf(nx is None, "PyQt6 not available")
class Gates(unittest.TestCase):
    def keys(self, g):
        return {a.key: a for a in nx.game_actions(g)}

    @linux_features
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
        host_parent = QWidget()
        host = nx.ToastHost(host_parent)
        host.show_toast("boom", 'error')
        t = host.lay.itemAt(0).widget()
        self.assertIsNone(t._timer)
        host_parent.deleteLater()


@unittest.skipIf(nx is None, "PyQt6 not available")
class Regressions(unittest.TestCase):
    def test_list_view_keeps_every_selected_row(self):
        lv = nx.ListView()
        self.addCleanup(lv.deleteLater)
        lv.set_games(fake_games())
        lv.mark_selected({'alpha', 'gamma'})
        rows = {i.row() for i in lv.selectedIndexes()}
        got = {lv.item(r, 1).data(Qt.ItemDataRole.UserRole) for r in rows}
        self.assertEqual(got, {'alpha', 'gamma'})

    def test_hero_download_for_a_game_left_behind_is_dropped(self):
        import tempfile
        from pathlib import Path
        from PyQt6.QtGui import QImage
        pending = {}
        hb = nx.HeroBanner()
        self.addCleanup(hb.deleteLater)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(nx.ART, 'fetch', lambda url, cb: pending.__setitem__(url, cb)):
            img = Path(d, 'a.png')
            im = QImage(8, 8, QImage.Format.Format_RGB32)
            im.fill(0xff0000)
            im.save(str(img))
            hb.set_game({'slug': 'a', 'hero_url': 'https://x/a.png'})
            hb.set_game({'slug': 'b', 'hero_url': 'https://x/b.png'})
            pending['https://x/a.png'](str(img))           # A's art lands late
            self.assertIsNone(hb._pm)
            pending['https://x/b.png'](str(img))
            self.assertIsNotNone(hb._pm)

    def test_big_hero_art_decodes_off_the_gui_thread(self):
        import tempfile
        from pathlib import Path
        from PyQt6.QtCore import QThreadPool
        from PyQt6.QtGui import QImage
        hb = nx.HeroBanner()
        self.addCleanup(hb.deleteLater)
        hb.resize(600, 176)
        logos = []
        hb.logo_changed.connect(logos.append)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(nx.HeroBanner, 'SYNC_BYTES', 0), \
                mock.patch.object(nx.HeroBanner, '_RECENT', {}):
            def game(slug, w, rgb):
                art = Path(d, slug)
                art.mkdir()
                im = QImage(w, w // 3, QImage.Format.Format_RGB32)
                im.fill(rgb)
                im.save(str(art / 'hero.png'))
                QImage(300, 100, QImage.Format.Format_ARGB32).save(str(art / 'logo.png'))
                return {'slug': slug, 'artwork_dir': str(art)}
            a, b = game('a', 3000, 0xff0000), game('b', 2400, 0x0000ff)
            colour = lambda: hb._pm.toImage().pixel(5, 5) & 0xffffff
            hb.set_game(a)
            self.assertIsNone(hb._pm)                      # not decoded on this thread
            self.assertTrue(hb.has_logo())                 # ...but the name stays hidden
            hb.set_game(b)                                 # moved on before A finished
            QThreadPool.globalInstance().waitForDone()
            QApplication.processEvents()
            self.assertEqual(hb._pm.width(), nx.HeroBanner.DECODE_W)   # at banner size
            self.assertEqual(colour(), 0x0000ff)           # B's, not A's late one
            self.assertLessEqual(hb._logo.height(), 176)
            self.assertEqual(logos[-1], True)
            decodes = []
            real = nx.fit_image
            with patch_gui('fit_image',
                                   lambda *a, **k: decodes.append(a) or real(*a, **k)):
                hb.set_game(b)                             # same files: nothing to do
                hb.set_game(a)                             # A's late decode was kept
                QThreadPool.globalInstance().waitForDone()
                QApplication.processEvents()
                self.assertEqual(colour(), 0xff0000)
                hb.set_game(b)
                self.assertEqual(colour(), 0x0000ff)
            self.assertEqual(decodes, [])

    def test_tiles_follow_jobs_without_busy_work(self):
        lv = nx.LibraryView()
        self.addCleanup(lv.deleteLater)
        lv.resize(900, 600)
        lv.show()
        lv.set_games(fake_games())
        QApplication.processEvents()                           # tiles show on the next pass
        jm = nx.JobManager()
        job = nx.Job(['x'], 'Installing Alpha', 'alpha', 'install')
        tool = nx.Job(['y'], 'Tool', 'gamma', 'tool')
        job.state = tool.state = 'running'
        jm.jobs += [job, tool]
        self.assertEqual(jm.tile_jobs(), {'alpha': job})       # tools get no ring
        self.assertEqual(jm.busy_slugs(), {'alpha'})
        lv.set_jobs(jm)
        tile = lv.tiles['alpha'][0]
        self.assertIs(tile.job, job)
        self.assertTrue(tile._spinner.isActive())              # no percentage yet
        with mock.patch.object(tile, 'update') as upd:
            job.detail = 'another output line'
            lv.set_jobs(jm)
            upd.assert_not_called()                            # nothing it shows changed
            job.progress = (5, 10)
            lv.set_jobs(jm)
            upd.assert_called()
        self.assertFalse(tile._spinner.isActive())             # a percentage: no spin
        job.progress = None
        lv.set_jobs(jm)
        lv.hide()
        self.assertFalse(tile._spinner.isActive())             # off screen: no spin
        lv.show()
        self.assertTrue(tile._spinner.isActive())

    def test_elapsed_timers_tick_while_output_streams(self):
        import time
        jm = nx.JobManager()
        job = nx.Job(['x'], 'X')
        job.state = 'running'
        jm.active = lambda: [job]
        page, strip = nx.ActivityPage(jm), nx.ActivityStrip(jm)
        self.addCleanup(page.deleteLater)
        self.addCleanup(strip.deleteLater)
        jm.changed.emit()
        time.sleep(0.3)
        jm.changed.emit()                                  # output keeps arriving
        for t in (page.timer, strip.timer):
            self.assertTrue(t.isActive())
            self.assertLess(t.remainingTime(), 900)        # not restarted from 1000

    def test_a_deleted_page_isnt_called_by_its_jobs(self):
        # The JobManager outlives the window's pages; a job still running
        # then (a test's leftover) used to call a lambda into the dead page.
        from PyQt6.QtCore import QEvent
        jm = nx.JobManager()
        page = nx.ActivityPage(jm)
        page.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        jm.changed.emit()                                  # nothing listens now

    def test_a_widget_waiting_for_deletion_keeps_its_lambda_slots(self):
        # deleteLater() let sip drop the wrapper; a collection then cleared
        # the lambda's closure while the timer still ran, and its next tick
        # crashed natively (the Windows runner's access violation). Without
        # the keep-alive in common.py this test takes the whole run down.
        import gc
        from PyQt6.QtCore import QEvent, QTimer
        ticks = []

        class Ticking(QWidget):
            def __init__(self, parent):
                super().__init__(parent)
                self.timer = QTimer(self)
                self.timer.setInterval(1)
                self.timer.timeout.connect(lambda: ticks.append(self.isHidden()))
                self.timer.start()

        host = QWidget()
        self.addCleanup(host.deleteLater)
        w = Ticking(host)
        key = id(w)
        w.deleteLater()
        del w
        gc.collect()
        time.sleep(0.02)
        QApplication.processEvents()                       # the overdue tick, before the delete
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self.assertTrue(ticks)
        self.assertEqual(host.findChildren(Ticking), [])
        self.assertNotIn(key, nx.common._DOOMED)          # let go once Qt deleted it

    def test_achievements_error_clears_on_an_empty_success(self):
        w = nx.AchievementsWidget()
        self.addCleanup(w.deleteLater)
        hub = mock.Mock(_games=[{'slug': 's', 'source': 'steam', 'appid': 1,
                                 'achievements': (1, 2)}])
        with patch_gui('cli_json', lambda *a, **k: None):
            w._got_recent(hub, None, 'offline')
            def shown():                                   # clear() hides, then deletes later
                return [c.text() for c in w.findChildren(QLabel) if not c.isHidden()]
            self.assertTrue(any('unavailable' in t for t in shown()))
            w._got_recent(hub, {'recent': []})
            self.assertFalse(any('unavailable' in t for t in shown()))

    def test_seen_repacks_trim_drops_the_oldest(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(nx.LibraryPrefs, 'PATH', Path(d, 'prefs.json')):
            prefs = nx.LibraryPrefs()
            prefs.seen_repacks = [f'z{i:03}|f' for i in range(300)] + ['a-newest|f']
            prefs.save()
            saved = json.loads(Path(d, 'prefs.json').read_text())['seen_repacks']
            self.assertIn('a-newest|f', saved)
            self.assertNotIn('z000|f', saved)
            self.assertEqual(nx.LibraryPrefs().seen_repacks[-1], 'a-newest|f')


@unittest.skipIf(nx is None, "PyQt6 not available")
class Behaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (nx.cli_json, nx.LibraryPrefs.save, nx.CliQuery)
        set_gui('cli_json', lambda *a, **k: None)
        nx.LibraryPrefs.save = lambda self: None
        set_gui('CliQuery', lambda *a, **k: None)
        cls.win = nx.MainWindow()
        cls.win.resize(1300, 850)
        cls.win.show()

    @classmethod
    def tearDownClass(cls):
        stop_leftovers(cls.win.jobs)
        cls.win.close()
        cls.win.deleteLater()
        QApplication.processEvents()
        del cls.win
        set_gui('cli_json', cls._saved[0])
        nx.LibraryPrefs.save = cls._saved[1]
        set_gui('CliQuery', cls._saved[2])

    def setUp(self):
        self.win._lib_inflight = self.win._lib_again = False
        self.win._playing, self.win._launching = set(), {}
        self.win._on_library({'games': fake_games(), 'disks': []}, '')
        self.toasts = []
        self.win.toast = lambda text, kind='info', action=None, timeout=0: \
            self.toasts.append((text, kind, action))

    def tearDown(self):
        del self.win.toast
        stop_leftovers(self.win.jobs)
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
        with patch_gui('cli_json', fake):
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

    def test_storage_uninstall_runs_the_cli_and_skips_a_running_game(self):
        """w-48: the Storage page's answer becomes one --uninstall job per
        repack, with the same gates as the game menu."""
        self.win._playing = {'gamma'}
        self.win._mark_states()
        ran = []
        with mock.patch.object(self.win, 'run', lambda args, *a, **k: ran.append(args)):
            self.win.storage.uninstall.emit(['alpha', 'gamma', 'steam-1'], True, True)
            self.win.storage.uninstall.emit(['alpha'], False, False)
        self.assertEqual(ran, [['--uninstall', 'alpha', '--yes', '--save-backup',
                                '--remove-snapshots'],
                               ['--uninstall', 'alpha', '--yes']])
        self.assertIn("Gamma is running", self.toasts[0][0])
        with mock.patch.object(self.win, 'on_game_action') as act:
            self.win.storage.game_action.emit('steam-uninstall', 'steam-1')
        act.assert_called_once_with('steam-uninstall', self.win.game('steam-1'))

    def test_storage_refreshes_when_an_uninstall_ends(self):
        """The drive bars, the table and the cleanup list catch up with a
        finished uninstall while the Storage page is shown."""
        w = self.win
        asked = []
        with patch_gui('cli_json', lambda args, cb, *a, **k: asked.append((args, cb))):
            w.go(w.STORAGE)
            QApplication.processEvents()
            asked.clear()
            job = nx.Job(['x', '--uninstall', 'alpha', '--yes'], "Uninstalling Alpha",
                         'alpha', 'task')
            job.state = 'done'
            w._job_done(job)
            QApplication.processEvents()
            lib = next(cb for a, cb in asked if a[0] == '--library')
            self.assertTrue(any(a[0] == '--cleanup-scan' for a, _cb in asked))
            disk = {'path': '/g', 'free': 9 << 30, 'total': 20 << 30, 'used': 11 << 30}
            lib({'games': [g for g in fake_games() if g['slug'] != 'alpha'],
                 'disks': [disk]}, '')
            QApplication.processEvents()
        rows = {w.storage.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
                for r in range(w.storage.table.rowCount())}
        self.assertNotIn('alpha', rows)
        self.assertEqual(w.storage.disks, [disk])
        w.go(w.HOME)

    def test_restore_saves_asks_first(self):
        ran = []
        with mock.patch.object(self.win, 'run', lambda *a, **k: ran.append(a)):
            self.win.on_game_action('restore-saves', self.win.game('alpha'))
            self.assertEqual(ran, [])
            sheet = self.win.sheet
            self.assertTrue(sheet.isVisible())
            yes = next(b for b in sheet.findChildren(QPushButton)
                       if b.text() == "Restore saves")
            self.assertTrue(any(b.text() == "Cancel"
                                for b in sheet.findChildren(QPushButton)))
            yes.click()
        self.assertFalse(self.win.sheet.isVisible())
        self.assertEqual(ran[0][0], ['--restore-saves', 'alpha'])

    def test_launch_that_never_shows_up_is_cleared(self):
        # Long ago. Not 0.0: monotonic() counts from boot, and on a freshly
        # started CI runner that can be less than LAUNCH_WAIT.
        self.win._launching['alpha'] = time.monotonic() - self.win.LAUNCH_WAIT - 1
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

    def test_settings_save_as_you_go(self):
        from nexus.gui.common import CONFIG_PATH
        old = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else None
        self.addCleanup(lambda: CONFIG_PATH.write_text(old) if old is not None
                        else CONFIG_PATH.unlink(missing_ok=True))
        w = self.win
        st = w.settings
        w.go(w.SETTINGS)
        st.scan_more.setText("~/Elsewhere")
        self.assertTrue(st.dirty)                         # typed, not left yet
        w.go(w.HOME)                                      # leaving saves it
        self.assertFalse(st.dirty)
        self.assertEqual(nx.load_config()['scan_dirs'], ['~/Elsewhere'])
        w.go(w.SETTINGS)
        st.tor_ratio.setText("lots")                      # can't be saved
        st.tor_ratio.editingFinished.emit()
        self.assertIn("A number", st.row_of[st.tor_ratio].error.text())
        st.steam.setChecked(False)                        # the rest still saves
        self.assertFalse(nx.load_config()['steam'])
        w.go(w.HOME)
        self.assertIn("Not saved", self.toasts[-1][0])
        self.assertIsNone(nx.load_config().get('torrent_seed_ratio'))
        w.go(w.SETTINGS)
        self.assertEqual(st.tor_ratio.text(), "lots")     # still there to fix
        st.load()                                         # Discard
        self.assertFalse(st.dirty)
        self.assertEqual(st.tor_ratio.text(), "")
        w.go(w.HOME)

    def test_settings_search_and_sections(self):
        w = self.win
        st = w.settings
        w.go(w.SETTINGS)
        st.search.setText("seeding")
        shown = [k for k, sec in st.sections.items() if sec.isVisibleTo(st)]
        self.assertEqual(shown, ['torrent'])
        self.assertTrue(st.nav_btns['torrent'].isVisibleTo(st))
        self.assertFalse(st.nav_btns['appearance'].isVisibleTo(st))
        st.search.setText("zzz no such setting")
        self.assertTrue(st.no_match.isVisibleTo(st))
        st.search.clear()
        self.assertTrue(all(sec.isVisibleTo(st) for sec in st.sections.values()))
        w._open_settings('alerts')                        # Deals / Downloads link in
        self.assertEqual(st.current_section(), 'alerts')
        w.go(w.HOME)

    def test_torrent_fields_only_with_a_client(self):
        st = self.win.settings
        st.tor_client.setCurrentIndex(st.tor_client.findData(None))
        st._sync_torrent_rows()
        self.assertFalse(any(r.available for r in st.tor_rows))
        st.tor_client.setCurrentIndex(st.tor_client.findData('transmission'))
        st._sync_torrent_rows()
        self.assertTrue(all(r.available for r in st.tor_rows))

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
            next(b for b in self.win.sheet.findChildren(QPushButton)
                 if b.text() == "Stop install").click()
        self.assertEqual(cancelled, [1])
        card.deleteLater()

    def test_elevated_installer_greys_out_stop(self):
        job = nx.Job(['x'], "Installing T", 't', 'install', 'T')
        job.state = 'running'
        card = nx.ActivityCard(job)
        job._emit_text("[2/3] Running setup.exe as administrator...\n")
        self.assertTrue(job.elevated)
        card.refresh()
        self.assertFalse(card.btn_cancel.isEnabled())
        self.assertIn("close it from its own window", card.btn_cancel.toolTip())
        job.cancel()                                  # Quit's cancel waits instead
        self.assertEqual(job.state, 'running')
        job._emit_text("[3/3] Searching for the game executable...\n")
        card.refresh()
        self.assertTrue(card.btn_cancel.isEnabled())
        card.deleteLater()

    def test_cancel_sets_the_jobs_event_when_it_has_one(self):
        class Event:
            def __init__(self, ok):
                self.ok, self.sets, self.closed = ok, 0, False

            def set(self):
                self.sets += 1
                return self.ok

            def close(self):
                self.closed = True
        job = nx.Job(['x'], "Task", 't', 'task')
        ev = Event(True)
        job.state, job.proc, job._cancel_event = 'running', QProcess(), ev
        with mock.patch.object(QProcess, 'terminate') as term:
            job.cancel()                              # Windows: the CLI watches it
        self.assertEqual(ev.sets, 1)
        term.assert_not_called()
        self.assertEqual(job.state, 'cancelling')
        job._finished(130, None)
        self.assertEqual(job.state, 'cancelled')
        self.assertTrue(ev.closed)
        self.assertIsNone(job._cancel_event)
        job2 = nx.Job(['x'], "Task", 't', 'task')
        job2.state, job2.proc = 'running', QProcess()     # Linux: no event
        with mock.patch.object(QProcess, 'terminate') as term:
            job2.cancel()                             # SIGTERM as before
        term.assert_called_once()
        job3 = nx.Job(['x'], "Task", 't', 'task')
        job3.state, job3.proc, job3._cancel_event = 'running', QProcess(), Event(False)
        with mock.patch.object(QProcess, 'terminate') as term:
            job3.cancel()                             # couldn't set it: terminate()
        term.assert_called_once()

    def test_a_job_hands_its_event_to_the_cli(self):
        import sys
        from nexus.gui import jobs

        class Event:
            name, closed = 'Local\\nexus-cancel-test', False

            def close(self):
                self.closed = True
        ev = Event()
        code = "import os; print('event=' + str(os.environ.get('NEXUS_CANCEL_EVENT')))"
        job = nx.Job([sys.executable, '-c', code], "Task", None, 'tool')
        with mock.patch.object(jobs.platform, 'cancel_event', return_value=ev):
            job.start()
        self.assertTrue(job.proc.waitForFinished(30000))
        self.assertEqual(job.state, 'done', job.log)
        self.assertIn('event=Local\\nexus-cancel-test', ''.join(job.log))
        self.assertTrue(ev.closed)
        job2 = nx.Job([sys.executable, '-c', code], "Task", None, 'tool')
        with mock.patch.object(jobs.platform, 'cancel_event', return_value=None):
            job2.start()                              # Linux: the environment as before
        self.assertTrue(job2.proc.waitForFinished(30000))
        self.assertIn('event=None', ''.join(job2.log))

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

    # w-48: Uninstall from the "Games by size" table.
    GAMES = [{'slug': 'a', 'display_name': 'A', 'source': 'nexus', 'disk_bytes': 9 << 30,
              'owned_dir': True},
             {'slug': 'b', 'display_name': 'B', 'source': 'nexus', 'disk_bytes': 5 << 30},
             {'slug': 'run', 'display_name': 'Run', 'source': 'nexus', 'disk_bytes': 3 << 30,
              'playing': True},
             {'slug': 's', 'display_name': 'S', 'source': 'steam', 'appid': 7,
              'disk_bytes': 2 << 30},
             {'slug': 'h', 'display_name': 'H', 'source': 'heroic', 'disk_bytes': 1 << 30}]

    def page(self, snaps=None):
        asked = []

        def fake(args, cb, *a, **k):
            asked.append(args)
            cb({'snapshots': [{}] * (snaps or {}).get(args[1], 0)}, None)
        p = patch_gui('cli_json', fake)
        p.start()
        self.addCleanup(p.stop)
        page = nx.StoragePage()
        self.addCleanup(page.deleteLater)
        page.set_library([dict(g) for g in self.GAMES], [])
        page.resize(900, 900)
        page.show()
        self.sent, self.acts, self.asked = [], [], asked
        page.uninstall.connect(lambda *a: self.sent.append(a))
        page.game_action.connect(lambda *a: self.acts.append(a))
        page.request.connect(lambda *a: self.fail(f"ran a job without asking: {a}"))
        return page

    def select(self, page, *slugs):
        from PyQt6.QtCore import QItemSelectionModel
        page.table.clearSelection()
        for r in range(page.table.rowCount()):
            if page.table.item(r, 0).data(Qt.ItemDataRole.UserRole) in slugs:
                page.table.selectionModel().select(
                    page.table.model().index(r, 0),
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows)

    @linux_features
    def test_uninstall_asks_inline_with_the_game_pages_choices(self):
        page = self.page(snaps={'a': 2})
        tops = set(QApplication.topLevelWidgets())
        self.assertFalse(page.btn_uninstall.isEnabled())       # nothing selected
        self.select(page, 'a')
        self.assertTrue(page.btn_uninstall.isEnabled())
        self.assertEqual([t for t, _f, _on in page._menu_entries()],
                         ["Open game page", "Uninstall…"])
        page.btn_uninstall.click()
        self.assertTrue(page.un_bar.isVisible())               # inline, no window
        self.assertEqual(set(QApplication.topLevelWidgets()), tops)
        self.assertIn("Uninstall A?", page.un_text.text())
        self.assertTrue(page.un_backup.isVisible() and page.un_backup.isChecked())
        self.assertEqual(self.asked, [['--snapshots', 'a', '--json']])
        self.assertTrue(page.un_snaps.isVisible())
        self.assertFalse(page.un_snaps.isChecked())            # kept unless ticked
        self.assertEqual(self.sent, [])
        page.un_snaps.setChecked(True)
        page.un_go.click()
        self.assertEqual(self.sent, [(['a'], True, True)])
        self.assertFalse(page.un_bar.isVisible())

    @linux_features
    def test_uninstall_several_leaves_out_what_it_cant(self):
        page = self.page()
        self.select(page, 'a', 'b', 'run', 's', 'h')
        self.assertEqual(page.btn_uninstall.text(), "Uninstall 2 games…")
        page.ask_uninstall()
        self.assertFalse(page.un_snaps.isVisible())            # none to delete
        skip = page.un_skip.text()
        for why in ("Run is running", "S: one at a time", "uninstall it in Heroic"):
            self.assertIn(why, skip)
        page.un_backup.setChecked(False)
        page.un_go.click()
        self.assertEqual(self.sent, [(['a', 'b'], False, False)])

    @linux_features
    def test_running_game_alone_cant_be_asked(self):
        page = self.page()
        self.select(page, 'run')
        self.assertFalse(page.btn_uninstall.isEnabled())
        self.assertIn("running", page.btn_uninstall.toolTip())
        page.ask_uninstall()
        self.assertFalse(page.un_bar.isVisible())

    @linux_features
    def test_steam_game_goes_to_steam(self):
        page = self.page()
        self.select(page, 's')
        self.assertEqual(page.btn_uninstall.text(), "Uninstall in Steam…")
        page.btn_uninstall.click()
        self.assertEqual(self.acts, [('steam-uninstall', 's')])
        self.assertFalse(page.un_bar.isVisible())

    @linux_features
    def test_question_follows_the_selection_and_survives_a_reload(self):
        page = self.page()
        self.select(page, 'a', 'b')
        page.ask_uninstall()
        page.set_library([dict(g) for g in self.GAMES], [])    # a job finished elsewhere
        self.assertTrue(page.un_bar.isVisible())
        self.assertEqual(set(page.selected_slugs()), {'a', 'b'})
        self.select(page, 'a')                                 # asked about two
        self.assertFalse(page.un_bar.isVisible())
        page.ask_uninstall()
        page.set_library([dict(g) for g in self.GAMES if g['slug'] != 'a'], [])
        self.assertFalse(page.un_bar.isVisible())              # it's gone


if __name__ == '__main__':
    unittest.main()


@unittest.skipIf(nx is None, "PyQt6 not available")
class CliErrorBodies(unittest.TestCase):
    """A --json command that fails but prints {"error": ...} on stdout: the
    GUI gets that body and its message, not a bare 'exit 1' (w-13)."""

    def finish(self, code, out, err=b'', error_body=True):
        from PyQt6.QtCore import QObject
        from nexus.gui import jobs

        class Proc:
            def readAllStandardOutput(self):
                return out

            def readAllStandardError(self):
                return err
        got = []
        q = jobs.CliQuery.__new__(jobs.CliQuery)
        QObject.__init__(q)
        q._cb, q._done, q._error_body, q.proc = (lambda o, e: got.append((o, e)),
                                                  False, error_body, Proc())
        q._finished(code, None)
        return got[0]

    def test_error_body_and_its_message_reach_the_caller(self):
        body = b'{"error": {"kind": "torrent", "message": "Transmission is not running"}}'
        out, err = self.finish(1, body, b'Torrent client: Transmission is not running')
        self.assertEqual(err, "Transmission is not running")
        self.assertIn('"torrent"', out)
        out, err = self.finish(1, b'{"error": "site down"}')
        self.assertEqual(err, "site down")

    def test_crash_and_plain_failures_still_give_none(self):
        crash = b'{"error": {"kind": "internal", "message": "KeyError: x"}}'
        self.assertEqual(self.finish(1, crash), (None, "KeyError: x"))
        self.assertEqual(self.finish(2, b'', b'--x isn\'t available'),
                         (None, "--x isn't available"))
        self.assertEqual(self.finish(1, b'{"error": "x"}', error_body=False), (None, "exit 1"))

    def test_success_has_no_error_text(self):
        # It used to say "exit 0", so cli_json's ttl memo never kept an answer
        # and the news source switch toasted "Couldn't change the sources".
        self.assertEqual(self.finish(0, b'{"a": 1}'), ('{"a": 1}', ''))
        self.assertEqual(self.finish(0, b'{}', b'warning: x'), ('{}', ''))

    def test_ttl_memo_answers_the_second_call_at_once(self):
        from nexus.gui import jobs
        started, got = [], []

        def fake(args, cb, timeout_ms=0, error_body=False):
            started.append(args)
            cb('{"n": 1}', '')
        saved = jobs.CliQuery
        jobs.CliQuery = fake
        self.addCleanup(setattr, jobs, 'CliQuery', saved)
        self.addCleanup(jobs.cli_cache_clear)
        jobs.cli_cache_clear()
        for _ in range(2):
            jobs.cli_json(['--w47-memo', '--json'], lambda d, e: got.append((d, e)), ttl=60)
        self.assertEqual(len(started), 1)
        self.assertEqual(got, [({'n': 1}, '')] * 2)


INFO = {'proton': 'GE-Proton10-1', 'proton_pinned': None, 'protons': ['GE-Proton10-1', 'cachy'],
        'launch_options': 'X=1 %command%', 'owns_options': True, 'profile': {'source': 'nexus'},
        'perf': {'mangohud': True}, 'lsfg': None, 'launch_via': 'nexus', 'in_steam': True,
        'perf_tools': {'gamescope': False, 'mangohud': True, 'game-performance': True,
                       'gamemoderun': False}}


@unittest.skipIf(nx is None, "PyQt6 not available")
class GamePanel(unittest.TestCase):
    """The game panel's five sections, and Launch's one Apply bar (w-29)."""
    REPACK = {'slug': 'r', 'display_name': 'R', 'source': 'nexus', 'launcher': '/r',
              'installed': True, 'launch_options': 'X=1 %command%', 'steam': False}

    def setUp(self):
        # Sections remember being open (Disclosure.OPEN, for the whole app):
        # give it back as it was, or later tests start with Launch open.
        saved = dict(nx.Disclosure.OPEN)
        self.addCleanup(lambda: (nx.Disclosure.OPEN.clear(), nx.Disclosure.OPEN.update(saved)))
        self.asked = []
        p = patch_gui('cli_json', lambda args, cb, *a, **k: self.asked.append(args))
        p.start()
        self.addCleanup(p.stop)
        self.p = nx.DetailsPanel()
        self.p.resize(400, 900)
        self.p.show()
        self.addCleanup(self.p.deleteLater)
        self.sent = []
        self.p.request.connect(lambda args, title, *_: self.sent.append(args))

    def repack(self, **extra):
        self.p.show_games([dict(self.REPACK, **extra)])
        self.p._apply_info(dict(INFO), None)
        self.p.sec_launch.set_open(True, remember=False)

    def visible_sections(self):
        lay = self.p.sections
        return [lay.itemAt(i).widget().key for i in range(lay.count())
                if not lay.itemAt(i).widget().isHidden()]

    @linux_features
    def test_a_repack_has_five_sections_and_old_keys_still_open_them(self):
        self.repack(achievements=[1, 4])
        self.assertEqual(self.visible_sections(),
                         ['launch', 'files', 'troubleshoot', 'about', 'notes'])
        for old, new in (('log', self.p.sec_trouble), ('snapshots', self.p.sec_files),
                         ('achievements', self.p.sec_about), ('tune', self.p.sec_launch)):
            self.p.open_section(old)
            self.assertTrue(new.is_open, old)

    @linux_features
    def test_apply_bar_counts_changes_and_queues_jobs_in_order(self):
        self.repack()
        p = self.p
        self.assertTrue(p.apply_bar.isHidden())               # nothing changed yet
        p.perf.ck_wayland.setChecked(True)
        p.cb_proton.setCurrentIndex(p.cb_proton.findData('cachy'))
        p._proton_picked(0)
        p.ed_opts.setText('Y=2 %command%')
        p._opts_edited('')
        p.seg_via.set_current('steam', emit=True)
        self.assertFalse(p.apply_bar.isHidden())
        self.assertEqual(p.pending_text.text(), "4 changes to apply")
        p.btn_apply.click()
        self.assertEqual(self.sent, [['--proton', 'r', 'cachy'],
                                     ['--set-launch', 'r', 'Y=2 %command%'],
                                     ['--perf', 'r', 'wayland=on'],
                                     ['--launch-via', 'r', 'steam']])
        self.assertTrue(p.apply_bar.isHidden())                # sent; waiting for the jobs
        self.assertTrue(p.perf.ck_wayland.isChecked())        # the edits stay on screen
        # The jobs run (busy) and finish: the settings are read again.
        self.asked.clear()
        p.show_games([dict(self.REPACK, busy=True)])
        p.show_games([dict(self.REPACK)])
        self.assertIn(['--show-launch', 'r', '--json'], self.asked)
        self.assertFalse(p._dirty & p.LAUNCH_KEYS)

    @linux_features
    def test_discard_puts_everything_back(self):
        self.repack()
        p = self.p
        p.perf.ck_gs.setEnabled(True)
        p.perf.ck_gs.setChecked(True)
        self.assertFalse(p.perf.gs_box.isHidden())            # gamescope's settings show
        p.ed_opts.setText('')
        p._opts_edited('')
        self.assertEqual(p.pending_text.text(), "2 changes to apply")
        p._discard_launch()
        self.assertTrue(p.apply_bar.isHidden())
        self.assertFalse(p.perf.ck_gs.isChecked())
        self.assertTrue(p.perf.gs_box.isHidden())
        self.assertEqual(p.ed_opts.text(), 'X=1 %command%')
        self.assertEqual(self.sent, [])

    @linux_features
    def test_clearing_the_line_is_a_change(self):
        self.repack()
        self.p.ed_opts.setText('')
        self.p._opts_edited('')
        self.p._apply_launch()
        self.assertEqual(self.sent, [['--set-launch', 'r', '']])

    @linux_features
    def test_missing_tools_say_so(self):
        self.repack()
        perf = self.p.perf
        self.assertFalse(perf.missing.isHidden())
        self.assertIn("gamescope", perf.missing.text())
        self.assertIn("GameMode", perf.missing.text())
        self.assertNotIn("MangoHud", perf.missing.text())
        self.assertIn("isn't set up", perf.cb_fg.itemText(0))
        self.assertFalse(perf.ck_gs.isEnabled())

    @linux_features
    def test_steam_game_gets_advice_only(self):
        p = self.p
        p.show_games([{'slug': 'steam-1', 'display_name': 'S', 'source': 'steam',
                       'appid': 1, 'installed': True}])
        self.assertTrue(p.sec_launch.isHidden())             # nothing to say yet
        p._fill_tips({'recipes': [{'name': 'Fix it', 'launch_options': 'A=1 %command%'}],
                      'protondb': {'tier': 'gold', 'total': 3, 'url': 'https://protondb.com'}})
        self.assertFalse(p.sec_launch.isHidden())
        for w in (p.runtime_box, p.perf, p.adv_box):
            self.assertTrue(w.isHidden(), w)
        self.assertFalse(p.sugg_box.isHidden())
        self.assertIn("1 suggestion", p.sec_launch.summary.text())
        self.assertFalse(p.compat_box.isHidden())            # ProtonDB moved to About
        self.assertTrue(p.sec_files.isHidden() and p.sec_trouble.isHidden())


@unittest.skipIf(nx is None, "PyQt6 not available")
class MenuShape(unittest.TestCase):
    """The game menu: everyday actions at the top, the rest in a few named
    submenus, never a submenu of one (w-27)."""

    def top(self, g):
        self.host = QWidget()                               # the menu's parent lives on
        m = nx.build_game_menu(self.host, g, lambda *a: None)
        return [a.menu().title() + ' >' if a.menu() else a.text()
                for a in m.actions() if not a.isSeparator()]

    @linux_features
    def test_repack(self):
        g = {'slug': 'x', 'display_name': 'X', 'source': 'nexus', 'launcher': '/x',
             'installed': True, 'steam': True, 'owned_dir': '/g/x', 'collections': []}
        self.assertEqual(self.top(g), [
            "Play", "Open game folder", "Game settings…", "Favourite", "Hidden from library",
            "Collections >", "Add to >", "Maintenance >", "Wine tools >", "Uninstall…"])
        sub = {a.sub for a in nx.game_actions(g) if a.key in ('add-steam', 'remove-steam',
                                                               'check-integrity', 'artwork')}
        self.assertEqual(sub, {"Add to", "Maintenance"})

    def test_steam_game(self):
        g = {'slug': 's', 'display_name': 'S', 'source': 'steam', 'launcher': 'steam',
             'installed': True, 'appid': 10, 'collections': []}
        top = self.top(g)
        self.assertEqual(top[:3], ["Play", "Open game folder", "Open in Steam"])
        self.assertIn("Maintenance >", top)
        self.assertEqual(top[-1], "Uninstall in Steam…")

    def test_not_installed_has_no_one_item_submenu(self):
        g = {'slug': 'o', 'display_name': 'O', 'source': 'steam', 'launcher': 'steam',
             'installed': False, 'appid': 11, 'collections': []}
        top = self.top(g)
        self.assertIn("Change artwork…", top)
        self.assertNotIn("Maintenance >", top)

    def test_theatre_sheet_keeps_the_groups_as_headings(self):
        g = {'slug': 'x', 'display_name': 'X', 'source': 'nexus', 'launcher': '/x',
             'installed': True, 'collections': []}
        subs = [a.sub for a in nx.game_actions(g, theatre=True)]
        self.assertNotIn("Wine tools", subs)               # desktop-only, not from the couch
        self.assertIn("Maintenance", subs)
        first = [s for i, s in enumerate(subs) if s and s not in subs[:i]]
        self.assertEqual(len(first), len(set(first)))      # each group in one block
