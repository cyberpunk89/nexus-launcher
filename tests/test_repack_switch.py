"""Settings > Repack features (config [repacks] enabled) and the Install
page's two tabs (w-22, w-23), plus the first-run setup fixes that came with
them (w-15). Offscreen, every CLI call stubbed."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_gui_layout import nx, stop_leftovers
    from test_gui_window import fake_games
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, stop_leftovers
    from tests.test_gui_window import fake_games
from tests.patching import patch_gui, set_gui  # noqa: E402

from nexus import alerts  # noqa: E402

if nx is not None:
    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QApplication


def _toml(text: str):
    d = tempfile.TemporaryDirectory()
    f = Path(d.name) / 'config.toml'
    f.write_text(text)
    return d, f


class Config(unittest.TestCase):
    def test_default_on_and_off_by_config(self):
        from nexus.cli import config as C
        from nexus.gui import config as GC
        for text, want in (('', True), ('[repacks]\nenabled = false\n', False),
                           ('[repacks]\nenabled = true\n', True), ('repacks = "x"\n', True)):
            d, f = _toml(text)
            self.addCleanup(d.cleanup)
            with mock.patch.object(C, 'CONFIG_PATH', f):
                self.assertIs(C.load_config()['repacks'], want, text)
            with mock.patch.object(GC, 'CONFIG_PATH', f):
                self.assertIs(GC.load_config()['repacks'], want, text)

    def test_repack_alerts_go_with_the_switch(self):
        d, f = _toml('[repacks]\nenabled = false\n[alerts]\nfitgirl = true\n')
        self.addCleanup(d.cleanup)
        with mock.patch.object(alerts, 'CONFIG_PATH', f):
            s = alerts.settings()
        self.assertFalse(s['fitgirl'] or s['repack_updates'])
        self.assertTrue(s['atl'])

    def test_agent_skips_fitgirl_with_the_switch_off(self):
        from nexus.cli import agent as A
        with mock.patch.object(A, 'load_config', return_value={'repacks': False}), \
                mock.patch('nexus.repackfeed.refresh', side_effect=AssertionError("fetched")):
            self.assertEqual(A.Agent.task_feed(mock.Mock()), A.EVERY['feed'])


@unittest.skipIf(nx is None, "PyQt6 not available")
class InstallPage(unittest.TestCase):
    def setUp(self):
        self._cli = nx.cli_json
        set_gui('cli_json', lambda *a, **k: None)
        from nexus.gui.install import InstallPage as IP
        self.page = IP()
        self.page.resize(900, 900)
        self.page.show()

    def tearDown(self):
        set_gui('cli_json', self._cli)
        self.page.deleteLater()

    def test_two_tabs_and_what_each_shows(self):
        p = self.page
        self.assertEqual(p.tabs.keys(), ['repack', 'game'])
        self.assertTrue(p.repack_box.isVisibleTo(p) and p.steps.isVisibleTo(p))
        self.assertFalse(p.add_box.isVisibleTo(p))
        p.set_mode('game')
        self.assertEqual(p.title.text(), "Add a game")
        self.assertTrue(p.add_box.isVisibleTo(p))
        self.assertFalse(p.repack_box.isVisibleTo(p) or p.silent.isVisibleTo(p))
        self.assertEqual(p.btn_go.text(), "Add game")

    def test_add_exe_builds_the_command(self):
        p = self.page
        p.set_mode('game')
        exe = '/home/u/Games/lutris/x/drive_c/Program Files/X/X.exe'
        p.pick_exe(exe)
        sent = []
        p.submit.connect(lambda cmd, name, slug: sent.append(cmd))
        p._go()
        self.assertIn('--add-exe', sent[0])
        self.assertEqual(sent[0][sent[0].index('--add-exe') + 1], exe)
        self.assertNotIn('--in-prefix', sent[0])
        self.assertNotIn('--interactive', sent[0])          # no setup window to show
        if p.in_prefix.isVisibleTo(p):                      # Linux: offered for a prefix
            p.in_prefix.setChecked(True)
            p._go()
            self.assertIn('--in-prefix', sent[-1])

    def test_steps_follow_the_pick_and_the_safety_look(self):
        p = self.page
        self.assertEqual(p.steps.current, 0)
        with patch_gui('cli_json', lambda *a, **k: None):
            p._pick('/d/repack', 'Game')
        self.assertEqual(p.steps.current, 1)                # being looked over
        p._safety_done('/d/repack', {'verdict': 'ok', 'findings': []})
        self.assertEqual(p.steps.current, 2)

    def test_found_repacks_are_cards(self):
        p = self.page
        p._scanned({'candidates': [{'name': 'A Game', 'path': '/d/a', 'size_bytes': 10}],
                    'scan_dirs': ['/d']}, '')
        self.assertEqual(p.scan_flow.count(), 1)
        with patch_gui('cli_json', lambda *a, **k: None):
            p.scan_flow.itemAt(0).widget().click()
        self.assertEqual(p.folder.text(), '/d/a')

    def test_repacks_off_leaves_only_add_a_game(self):
        p = self.page
        p.set_repacks(False)
        self.assertFalse(p.tabs.isVisibleTo(p))
        self.assertEqual(p._mode, 'game')
        p.set_mode('repack')                                # not allowed now
        self.assertEqual(p._mode, 'game')
        p._pick('/d/dropped-folder')                        # a dropped folder: a game folder
        self.assertEqual(p._kind, 'import')
        p.set_repacks(True)
        self.assertTrue(p.tabs.isVisibleTo(p))


@unittest.skipIf(nx is None, "PyQt6 not available")
class Window(unittest.TestCase):
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
        self.win = nx.MainWindow()
        self.win.resize(1300, 850)
        self.win.show()
        self.win._on_library({'games': fake_games(), 'disks': []}, '')
        QApplication.processEvents()

    def tearDown(self):
        stop_leftovers(self.win.jobs)
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

    def test_switch_off_hides_repack_parts_and_stops_their_checks(self):
        w = self.win
        w.config = {**w.config, 'repacks': False, 'gamedb_enabled': True}
        w._apply_repacks()
        self.assertFalse(w.icons[w.DOWNLOADS].isVisibleTo(w))
        self.assertFalse(w.install.tabs.isVisibleTo(w.install))
        w.dealspage.res = {'repacks': {'10': {'slug': 'x'}}}
        self.assertEqual(w.dealspage.repacks(), {})
        asked = []
        with patch_gui('cli_json', lambda args, cb, *_a, **_k: asked.append(args[0])):
            w._check_repack_feed()
            w._torrent_polling = False
            w._poll_torrents()
        self.assertNotIn('--repack-feed', asked)
        self.assertNotIn('--repack-index', asked)
        self.assertNotIn('--torrents', asked)
        w.config = {**w.config, 'repacks': True}
        w._apply_repacks()
        self.assertTrue(w.icons[w.DOWNLOADS].isVisibleTo(w))
        self.assertTrue(w.dealspage.repacks())

    def test_settings_switch_hides_rows_and_saves(self):
        st = self.win.settings
        st.load()
        st.repacks.setChecked(False)
        self.assertFalse(any(r.available for r in st.sections['torrent'].rows))
        self.assertFalse(any(r.available for r in st._repack_rows))
        self.assertFalse(st.sections['torrent'].isVisible())
        st.repacks.setChecked(True)
        self.assertTrue(all(r.available for r in st._repack_rows))

    def test_setup_without_repacks_and_skip_saves_nothing(self):
        w = self.win
        saved = []
        with patch_gui('cli_json', lambda *a, **k: None), \
                patch_gui('save_config', saved.append), \
                mock.patch.object(w, 'set_appearance'):
            w.open_setup()
            st = w.setup
            st.repacks.setChecked(False)
            self.assertFalse(st.scan_box.isVisibleTo(st) or st.torrent_box.isVisibleTo(st))
            st.scan.setText('/d/dl')
            u, _opts = st.answers_now()
            self.assertIs(u[('repacks', 'enabled')], False)
            self.assertNotIn(('install', 'scan_dir'), u)
            st.base.setText('/g/Games')
            st._skip()
        self.assertEqual(saved, [])                         # Skip keeps nothing (w-15)

    def test_setup_preview_writes_no_prefs(self):
        w = self.win
        with mock.patch.object(type(w.prefs), 'save') as save:
            w.setup.preview.emit({'theme': 'dawn'})
        save.assert_not_called()


@unittest.skipIf(nx is None, "PyQt6 not available")
class Prefs(unittest.TestCase):
    def test_bad_tile_value_doesnt_stop_start_up(self):
        from nexus.gui import prefs
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'library.json'
            for raw, want in (('"big"', prefs.TILE_W), ('9999', 260), ('null', prefs.TILE_W)):
                f.write_text(f'{{"tile": {raw}}}')
                with mock.patch.object(prefs.LibraryPrefs, 'PATH', f):
                    self.assertEqual(prefs.LibraryPrefs().tile, want, raw)


if __name__ == '__main__':
    unittest.main()
