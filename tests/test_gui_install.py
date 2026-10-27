"""The GUI side of installs: the CLI's @nexus events, multi-action toasts,
unfinished installs (Resume / Discard), the download watcher, repack updates
and the install command line. Offscreen, with every CLI call stubbed."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_gui_layout import nx
    from test_gui_window import fake_games
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx
    from tests.test_gui_window import fake_games
from tests.patching import set_gui  # noqa: E402

from nexus import features, platform  # noqa: E402

if nx is not None:
    from PyQt6.QtWidgets import QApplication, QLabel, QPushButton


def feed(job, *lines):
    job._emit_text(''.join(f"{line}\n" for line in lines))


@unittest.skipIf(nx is None, "PyQt6 not available")
class Events(unittest.TestCase):
    def test_silent_install_step_and_progress(self):
        job = nx.Job(['nexus-cli', '/r', '--yes'], "Installing T", None, 'install', 'T')
        feed(job, "[3/4] Running setup.exe via umu-run + GE...",
             '@nexus progress {"written": 1500000000}')
        self.assertEqual(job.step, nx._SILENT_STEP)
        self.assertTrue(job.detail.startswith("1.5 GB written"), job.detail)
        self.assertEqual(job.events['progress'], {'written': 1500000000})
        feed(job, '@nexus exe-candidates {"slug": "t", "chosen": "/a.exe", "candidates": []}')
        self.assertEqual(job.events['exe-candidates']['chosen'], '/a.exe')
        self.assertNotIn('@nexus', job.detail)

    def test_move_shows_how_much_is_copied(self):
        job = nx.Job(['nexus-cli', '--move', 't', '/mnt/b'], "Moving T", 't', 'task')
        feed(job, '@nexus move {"done": 500000000, "total": 2000000000}')
        self.assertEqual(job.detail, "500.0 MB of 2.0 GB copied")

    def test_setup_window_keeps_its_step(self):
        job = nx.Job(['nexus-cli', '/r', '--interactive'], "Installing T", None, 'install')
        feed(job, "[3/4] Running setup.exe via umu-run + GE...")
        self.assertEqual(job.step, nx._INSTALL_STEPS[3])

    def test_windows_install_steps(self):
        # [2/3] runs setup.exe as administrator: the installer's step, no
        # "prefix"; [3/3] is the CLI's own words.
        job = nx.Job(['nexus-cli', '/r', '--yes'], "Installing T", None, 'install', 'T')
        feed(job, "[1/3] Skipping file verification (use --verify to enable).")
        self.assertEqual(job.step, nx._INSTALL_STEPS[1])
        feed(job, "[2/3] Running setup.exe as administrator...")
        self.assertEqual((job.step, job.progress, job.elevated), (nx._SILENT_STEP, None, True))
        feed(job, "[3/3] Searching for the game executable...")
        self.assertEqual((job.step, job.progress), ("Searching for the game executable", (3, 3)))
        job = nx.Job(['nexus-cli', '--import', '/d/x.exe'], "Adding X", None, 'install', 'X')
        feed(job, "[1/2] Running x.exe as administrator — install it into C:\\Games\\x so ...")
        self.assertEqual(job.step, nx._INSTALL_STEPS[3])       # its own window

    def test_output_bursts_coalesce(self):
        from PyQt6.QtTest import QTest
        job = nx.Job(['nexus-cli'], "X", None, 'task')
        hits = []
        job.changed.connect(lambda: hits.append(job.detail))
        for i in range(200):
            feed(job, f"line {i}")
        self.assertEqual(hits, [])                        # nothing per line…
        QTest.qWait(nx.Job.CHANGE_MS + 80)
        self.assertEqual(hits, ["line 199"])              # …one update, the latest
        job.state = 'running'
        job._end('done', 0)
        self.assertEqual(len(hits), 2)                    # the end is never delayed

    def test_bad_event_is_ignored(self):
        job = nx.Job(['nexus-cli'], "X", None, 'task')
        feed(job, '@nexus progress {not json}')
        self.assertEqual(job.events, {})


@unittest.skipIf(nx is None, "PyQt6 not available")
class Toasts(unittest.TestCase):
    def test_several_actions(self):
        hits = []
        t = nx.Toast("Done", 'success', [("One", lambda: hits.append(1)),
                                         ("Two", lambda: hits.append(2))], 0)
        buttons = {b.text(): b for b in t.findChildren(QPushButton) if b.text()}
        self.assertIn("One", buttons)
        self.assertIn("Two", buttons)
        buttons["Two"].click()
        self.assertEqual(hits, [2])
        # Clicking starts the fade-out; a parentless toast left animating
        # would call back into a dead wrapper later (in the app they live
        # in ToastHost). Stop it and delete it now.
        t._anim.stop()
        t.deleteLater()
        QApplication.processEvents()


@unittest.skipIf(nx is None, "PyQt6 not available")
class Unfinished(unittest.TestCase):
    GAME = {'slug': 'half', 'display_name': 'Half', 'source': 'nexus', 'installed': True,
            'incomplete': {'argv': ['/r/Half', '--yes'], 'source': '/r/Half'}}

    def test_resume_label_and_actions(self):
        g = dict(self.GAME)
        self.assertEqual(nx.play_label(g), "Resume install")
        self.assertTrue(nx.play_enabled(g))
        self.assertEqual([a.key for a in nx.game_actions(g)],
                         ['launch', 'open-folder', 'discard-incomplete'])
        self.assertEqual([a.key for a in nx.game_actions(g, theatre=True)],
                         ['launch', 'discard-incomplete'])
        g['resuming'] = True
        self.assertFalse(nx.play_enabled(g))

    def test_update_action(self):
        g = {'slug': 'a', 'source': 'nexus', 'launcher': '/a', 'repack_update': '/r/A2'}
        self.assertEqual(nx.game_actions(g)[1].key, 'install-update')


@unittest.skipIf(nx is None, "PyQt6 not available")
class Background(unittest.TestCase):
    def test_match_running(self):
        games = [{'slug': 'rep', 'source': 'nexus', 'exe': 'C:\\G\\Game.EXE'},
                 {'slug': 'sc', 'source': 'nexus', 'steam_appid': 3000000000},
                 {'slug': 'trk', 'source': 'nexus'},
                 {'slug': 'steam-10', 'source': 'steam', 'appid': 10},
                 {'slug': 'heroic-x', 'source': 'heroic', 'install_dir': '/g/Epic X'},
                 {'slug': 'epic-Fable', 'source': 'epic', 'install_dir': 'C:\\Games\\Fable'},
                 {'slug': 'epic-Idle', 'source': 'epic', 'install_dir': 'C:\\Games\\Fab'},
                 {'slug': 'idle', 'source': 'steam', 'appid': 11}]
        r = {'appids': [10, 3000000000], 'slugs': ['trk'], 'exes': ['game.exe'],
             'paths': ['wine /g/Epic X/bin/x.exe', 'C:\\Games\\Fable\\Fable.exe']}
        self.assertEqual(nx.match_running(games, r),
                         {'rep', 'sc', 'trk', 'steam-10', 'heroic-x', 'epic-Fable'})

    def test_in_thread_delivers_on_the_gui_thread(self):
        from PyQt6.QtCore import QThread
        from PyQt6.QtTest import QTest
        main = QThread.currentThread()
        got = []
        nx.in_thread(lambda: QThread.currentThread() is not main,
                     lambda res, err: got.append((res, err, QThread.currentThread() is main)))
        nx.in_thread(lambda: 1 / 0, lambda res, err: got.append((res, type(err), True)))
        for _ in range(100):
            if len(got) == 2:
                break
            QTest.qWait(10)
        self.assertIn((True, None, True), got)            # ran off-thread, delivered on it
        self.assertIn((None, ZeroDivisionError, True), got)


@unittest.skipIf(nx is None, "PyQt6 not available")
class AchievementsUI(unittest.TestCase):
    GAME = {'slug': 'steam-7', 'display_name': 'Seven', 'source': 'steam', 'appid': 7,
            'launcher': 'steam://rungameid/7', 'achievements': [3, 12]}
    LIST = {'appid': 7, 'total': 3, 'unlocked': 1, 'items': [
        {'title': 'First', 'desc': 'Do it', 'unlocked': True, 'time': 1700000000,
         'hidden': False, 'icon_url': None},
        {'title': 'Spoiler', 'desc': 'The twist', 'unlocked': False, 'time': None,
         'hidden': True, 'icon_url': None},
        {'title': 'Later', 'desc': 'Some day', 'unlocked': False, 'time': None,
         'hidden': False, 'icon_url': None}]}

    def setUp(self):
        self._cli = nx.cli_json
        self.asked = []
        set_gui('cli_json', lambda args, cb, *a, **k: (self.asked.append(args), cb(self.LIST, '')))

    def tearDown(self):
        set_gui('cli_json', self._cli)

    def texts(self, w):
        return [lab.text() for lab in w.findChildren(QLabel)]

    def test_details_panel(self):
        nx.Disclosure.OPEN.pop('about', None)              # closed, whatever ran before
        panel = nx.DetailsPanel()
        panel.show_games([dict(self.GAME)])
        self.assertFalse(panel.st_ach.isHidden())
        self.assertEqual(panel.st_ach.value.text(), "3 of 12")
        self.assertFalse(panel.sec_about.isHidden())          # achievements live in About
        self.assertFalse(panel.ach_box.isHidden())
        self.assertNotIn(['--achievements', '7', '--json'], self.asked)   # not until opened
        panel.sec_about.set_open(True)
        self.assertIn(['--achievements', '7', '--json'], self.asked)
        self.assertIn("3 / 12", panel.sec_about.summary.text())
        texts = self.texts(panel.ach_body)
        self.assertIn("First", texts)
        self.assertIn("Hidden achievement", texts)          # a secret stays secret
        self.assertNotIn("Spoiler", texts)
        panel.sec_about.set_open(False)
        panel.show_games([{'slug': 'r', 'source': 'nexus', 'launcher': '/r'}])
        self.assertTrue(panel.st_ach.isHidden())             # repacks: nothing
        panel.deleteLater()

    def test_dashboard_widget(self):
        recent = {'recent': [{'appid': 7, 'title': 'First', 'time': 1700000000}]}
        set_gui('cli_json', lambda args, cb, *a, **k: cb(recent, ''))

        class Hub:
            _games = [dict(self.GAME), {'slug': 'steam-8', 'display_name': 'Eight',
                                        'source': 'steam', 'appid': 8, 'achievements': [5, 5]}]
        w = nx.AchievementsWidget()
        w.fill(Hub())
        texts = self.texts(w)
        self.assertIn("8", texts)                            # 3 + 5 unlocked
        self.assertTrue(any('1 complete' in t for t in texts), texts)
        self.assertIn("First", texts)
        self.assertIn("Closest to 100%", texts)
        w.deleteLater()
        self.assertIn('achievements', {k for k, *_ in nx.DASH_KINDS})
        self.assertTrue(next(it for it in nx.dash_default() if it['id'] == 'achievements')
                        ['hidden'])


class _Prefs:
    def __init__(self):
        self.seen_repacks = None
        self.saves = 0

    def save(self):
        self.saves += 1


@unittest.skipIf(nx is None, "PyQt6 not available")
class Watcher(unittest.TestCase):
    def setUp(self):
        from nexus import watch
        self.watch = watch
        watch.STATE.unlink(missing_ok=True)
        self.addCleanup(watch.STATE.unlink, missing_ok=True)

    def cand(self, path, size=10, **kw):
        return {'path': path, 'name': path, 'slug': path, 'size_bytes': size,
                'fingerprint': 'f' + path, 'complete': True, 'installed': False,
                'update_for': None, **kw}

    def test_announces_finished_downloads_once(self):
        prefs = _Prefs()
        w = nx.RepackWatcher(prefs)
        ready = []
        w.ready.connect(ready.append)
        w.handle([self.cand('old')])                      # first run: just records
        self.assertEqual(ready, [])
        self.assertEqual(self.watch.load_seen(), ['old|fold'])
        w.handle([self.cand('old'), self.cand('new', complete=False)])
        self.assertTrue(w.poll.isActive())                # still downloading
        w.handle([self.cand('old'), self.cand('new', size=20)])
        self.assertEqual(ready, [])                       # finished, but size just changed
        w.handle([self.cand('old'), self.cand('new', size=20)])
        self.assertEqual([c['path'] for c in ready], ['new'])
        self.assertFalse(w.poll.isActive())
        w.handle([self.cand('old'), self.cand('new', size=20)])
        self.assertEqual(len(ready), 1)                   # never twice
        again = nx.RepackWatcher(_Prefs())                # nor by the agent, or a new window
        again.ready.connect(ready.append)
        again.handle([self.cand('new', size=20)])
        again.handle([self.cand('new', size=20)])
        self.assertEqual(len(ready), 1)

    def test_old_prefs_list_is_carried_over(self):
        prefs = _Prefs()
        prefs.seen_repacks = ['old|fold']
        w = nx.RepackWatcher(prefs)
        ready = []
        w.ready.connect(ready.append)
        w.handle([self.cand('old')])
        w.handle([self.cand('old')])
        self.assertEqual(ready, [])                       # not first run, already seen
        self.assertEqual(self.watch.load_seen(), ['old|fold'])

    def test_installed_only_when_it_is_an_update(self):
        prefs = _Prefs()
        prefs.seen_repacks = set()
        w = nx.RepackWatcher(prefs)
        ready = []
        w.ready.connect(ready.append)
        cands = [self.cand('same', installed=True),
                 self.cand('upd', installed=True, update_for='upd')]
        w.handle(cands)
        w.handle(cands)
        self.assertEqual([c['path'] for c in ready], ['upd'])


@unittest.skipIf(nx is None, "PyQt6 not available")
class InstallCommand(unittest.TestCase):
    def setUp(self):
        self._cli = nx.cli_json
        set_gui('cli_json', lambda *a, **k: None)
        self.page = nx.InstallPage()
        self.page.open({'steam': True, 'heroic': False, 'desktop': True, 'unattended': True})

    def tearDown(self):
        set_gui('cli_json', self._cli)
        self.page.deleteLater()

    def test_another_game_uses_import_without_repack_checks(self):
        self.page.verify.setChecked(True)
        self.page.pick_import('/d/setup_hollow_knight_1.5_(64bit)_(4).exe')
        self.assertEqual(self.page.name.placeholderText(), "Hollow knight")
        self.assertEqual(self.page.btn_go.text(), "Add game")
        sent = []
        self.page.submit.connect(lambda cmd, name, slug: sent.append((cmd, name)))
        self.page._go()
        cmd, name = sent[0]
        self.assertEqual(nx.cli_args(cmd)[:2],
                         ['--import', '/d/setup_hollow_knight_1.5_(64bit)_(4).exe'])
        self.assertNotIn('--verify', cmd)
        self.assertEqual(name, "Hollow knight")
        self.page._pick('/r/Repack')                          # back to a repack
        self.assertEqual(self.page.btn_go.text(), "Install")

    def test_silent_and_steam_never_waits(self):
        cmd = self.page._command('/r/Game', '')
        self.assertIn('--defer-steam', cmd)
        self.assertNotIn('--interactive', cmd)
        self.assertIn('--no-heroic', cmd)
        self.page.silent.setChecked(False)
        self.assertIn('--interactive', self.page._command('/r/Game', 'Name'))

    def test_install_all_queues_finished_downloads(self):
        sent = []
        self.page.submit.connect(lambda cmd, name, slug: sent.append((nx.cli_args(cmd)[0], slug)))
        self.page._scanned({'candidates': [
            {'path': '/r/A', 'name': 'A', 'slug': 'a', 'complete': True, 'installed': False},
            {'path': '/r/B', 'name': 'B', 'slug': 'b', 'complete': True, 'installed': False},
            {'path': '/r/C', 'name': 'C', 'slug': 'c', 'complete': False, 'installed': False},
            {'path': '/r/D', 'name': 'D', 'slug': 'd', 'complete': True, 'installed': True}],
            'scan_dirs': ['/r']}, '')
        self.assertFalse(self.page.btn_all.isHidden())
        self.page._install_all()
        self.assertEqual(sent, [('/r/A', 'a'), ('/r/B', 'b')])


@unittest.skipIf(nx is None, "PyQt6 not available")
class Config(unittest.TestCase):
    def test_toml_lists(self):
        text = '[install]\nscan_dirs = ["/a", "/b"]  # mine\nbase = "/g"\n'
        out = nx.toml_update(text, {('install', 'scan_dirs'): ['/c']})
        self.assertIn('scan_dirs = ["/c"]  # mine', out)
        self.assertIn('base = "/g"', out)
        out = nx.toml_update(out, {('install', 'scan_dirs'): None})
        self.assertNotIn('scan_dirs', out)


@unittest.skipIf(nx is None, "PyQt6 not available")
class Followups(unittest.TestCase):
    """One window for the class: each extra MainWindow is a lot of widgets to
    tear down, and the suite already builds plenty."""

    @classmethod
    def setUpClass(cls):
        cls._saved = (nx.cli_json, nx.LibraryPrefs.save, nx.CliQuery)
        set_gui('cli_json', lambda *a, **k: None)
        nx.LibraryPrefs.save = lambda self: None
        set_gui('CliQuery', lambda *a, **k: None)
        cls.win = nx.MainWindow()
        cls.win.resize(1300, 850)
        cls.win.show()
        QApplication.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.win.jobs.jobs.clear()
        cls.win.close()
        cls.win.deleteLater()
        QApplication.processEvents()
        del cls.win
        set_gui('cli_json', cls._saved[0])
        nx.LibraryPrefs.save = cls._saved[1]
        set_gui('CliQuery', cls._saved[2])

    def setUp(self):
        self.win._repack_updates = {}
        self.win._on_library({'games': fake_games(), 'disks': []}, '')
        self.toasts = []
        self.win.toast = lambda text, kind='info', action=None, timeout=0: \
            self.toasts.append((text, kind, action))

    def tearDown(self):
        del self.win.toast
        self.win.jobs.jobs.clear()

    def finished(self, state, events, argv=('nexus-cli', '/r/T', '--yes')):
        job = nx.Job(list(argv), "Installing T", 't', 'install', 'T')
        job.events = events
        job.state = state
        return job

    def test_success_offers_exe_change_trash_and_steam_later(self):
        gated = []
        job = self.finished('done', {
            'installed': {'slug': 't', 'source': '/r/T', 'steam_pending': True},
            'exe-candidates': {'chosen': '/p/T.exe', 'candidates': [{'path': '/p/T.exe'},
                                                                  {'path': '/p/L.exe'}]}})
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(nx.MainWindow, 'STEAM_PENDING', Path(d) / 'sp.json'), \
                mock.patch.object(self.win.gate, 'run', lambda what, ok, skip=None:
                                  gated.append(what)):
            self.win._install_followups(job)
            # Owed to Steam on disk too, so quitting first doesn't lose it.
            self.assertEqual(self.win._steam_pending(), [{'slug': 't', 'name': 'T'}])
        text, kind, actions = self.toasts[-1]
        self.assertIn('starts T.exe', text)
        self.assertEqual([a[0] for a in actions], ["Play", "Change game file", "Trash repack"])
        self.assertEqual(gated, ["add T to Steam"])

    def test_silent_failure_retries_with_the_window(self):
        sent = []
        job = self.finished('failed', {'retry-interactive': {'slug': 't'}})
        self.win._install_followups(job)
        actions = dict(self.toasts[-1][2])
        with mock.patch.object(self.win.jobs, 'submit', sent.append):
            actions["Retry with setup window"]()
        self.assertIn('--interactive', sent[0].argv)

    def test_other_failure_offers_verify(self):
        job = self.finished('failed', {})
        self.win._install_followups(job)
        self.assertEqual([a[0] for a in self.toasts[-1][2]],
                         ["Retry", "Verify & retry", "Show log"])

    def test_resume_runs_the_recorded_arguments(self):
        g = dict(Unfinished.GAME)
        started = []
        with mock.patch.object(self.win, '_start_install',
                               lambda cmd, name, slug='': started.append((cmd, slug))):
            self.win.on_game_action('launch', g)
        cmd, slug = started[0]
        self.assertEqual(cmd[:len(nx.CLI) + 2], [*nx.CLI, '/r/Half', '--yes'])
        self.assertIn('--defer-steam', cmd)
        self.assertEqual(slug, 'half')

    def test_play_state_repaints_home_without_rebuilding(self):
        w = self.win
        w.go(w.HOME)
        w._home_sig = None
        w._refresh_home()
        QApplication.processEvents()
        before = {id(t) for t in w.homeview.findChildren(nx.GameTile)}
        self.assertTrue(before)
        w._playing = {'alpha'}
        w._mark_states()
        w._refresh_home()
        after = {id(t) for t in w.homeview.findChildren(nx.GameTile)}
        self.assertEqual(before, after)
        self.assertEqual(w.homeview.hero.game.get('slug'), 'alpha')   # playing leads
        w._playing = set()
        w._mark_states()

    def test_repack_update_marks_the_game(self):
        self.win._repack_scanned([{'path': '/r/A2', 'update_for': 'alpha', 'complete': True}])
        alpha = next(g for g in self.win._games if g['slug'] == 'alpha')
        self.assertEqual(alpha['repack_update'], '/r/A2')
        self.assertTrue(alpha['update_pending'])
        # A library reload keeps the mark.
        self.win._on_library({'games': fake_games(), 'disks': []}, '')
        alpha = next(g for g in self.win._games if g['slug'] == 'alpha')
        self.assertEqual(alpha['repack_update'], '/r/A2')

    # ---- Windows (FEATURES of the Windows build) ----
    def sheet_rows(self):
        return [b.text() for b in self.win.sheet.findChildren(QPushButton, "SheetRow")
                if not b.isHidden()]

    def test_declined_admin_prompt_is_not_a_failure(self):
        job = self.finished('failed', {'install-cancelled': {'slug': 't', 'why': 'uac'}})
        sent = []
        with mock.patch.object(features, 'FEATURES', frozenset()):
            self.win._install_followups(job)
        text, kind, actions = self.toasts[-1]
        self.assertIn("admin prompt was declined", text)
        self.assertEqual(kind, 'info')
        with mock.patch.object(self.win.jobs, 'submit', sent.append):
            dict(actions)["Try again"]()
        self.assertEqual(sent[0].argv, job.argv)
        self.assertTrue(self.win.sheet.isHidden())

    def test_defender_quarantine_opens_a_sheet_that_changes_nothing(self):
        dll = r'C:\Games\t\vcruntime140_1_app.dll'
        job = self.finished('failed', {'antivirus': {
            'slug': 't', 'log_lines': [], 'faq': 'https://fitgirl-repacks.site/faq/',
            'detections': [{'file': dll, 'threat': 'Trojan:Win32/Wacatac.H!ml', 'at': 'x'}]}})
        self.addCleanup(self.win.sheet.close_sheet)
        with mock.patch.object(features, 'FEATURES', frozenset()):
            self.win._install_followups(job)
        sheet = self.win.sheet
        self.assertTrue(sheet.isVisible())
        self.assertEqual(sheet.title.text(), "Windows Defender removed a game file")
        body = " ".join(lab.text() for lab in sheet.findChildren(QLabel))
        self.assertIn("vcruntime140_1_app.dll — Trojan:Win32/Wacatac.H!ml", body)
        self.assertIn("Protection history", body)
        self.assertEqual(self.sheet_rows(), ["Open Windows Security",
                                             "FitGirl's FAQ on false positives", "Show log",
                                             "Close"])
        self.assertEqual([a[0] for a in self.toasts[-1][2]], ["What happened", "Retry",
                                                              "Show log"])
        with mock.patch.object(platform, 'open_url', return_value=True) as op:
            next(b for b in sheet.findChildren(QPushButton, "SheetRow")
                 if b.text() == "Open Windows Security").click()
        op.assert_called_once_with('windowsdefender://threat')
        self.assertTrue(sheet.isHidden())

    def test_uac_note_before_the_first_install_only(self):
        prefs = self.win.prefs
        self.addCleanup(setattr, prefs, 'uac_note_shown', prefs.uac_note_shown)
        self.addCleanup(self.win.sheet.close_sheet)
        prefs.uac_note_shown = False
        sent = []
        with mock.patch.object(features, 'FEATURES', frozenset()), \
                mock.patch.object(self.win.jobs, 'submit', sent.append):
            self.win._start_install([*nx.CLI, '/r/A'], 'A', 'a')
            self.win._start_install([*nx.CLI, '/r/B'], 'B', 'b')     # "Install all"
            self.assertEqual(sent, [])
            self.assertTrue(self.win.sheet.isVisible())
            self.assertIn("as administrator", " ".join(
                lab.text() for lab in self.win.sheet.findChildren(QLabel)))
            self.assertTrue(prefs.uac_note_shown)
            next(b for b in self.win.sheet.findChildren(QPushButton, "SheetRow")
                 if b.text() == "Install").click()
            self.assertEqual([j.slug for j in sent], ['a', 'b'])
            self.win._start_install([*nx.CLI, '/r/C'], 'C', 'c')     # shown once
            self.assertEqual([j.slug for j in sent], ['a', 'b', 'c'])
        prefs.uac_note_shown = False
        with mock.patch.object(features, 'FEATURES', features.ALL), \
                mock.patch.object(self.win.jobs, 'submit', sent.append):
            self.win._start_install([*nx.CLI, '/r/D'], 'D', 'd')     # Linux: no UAC
        self.assertEqual(sent[-1].slug, 'd')


if __name__ == '__main__':
    unittest.main()
