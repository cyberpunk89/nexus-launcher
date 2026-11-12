"""Smart shelves in the window: the Library's Browse rows and head (w-99),
the Smart group in the status filter, See all / Save as collection / hide,
the game page's filter key (_show_filter), and the Home widget. Offscreen,
CLI stubbed, prefs never written; the facts index is set in memory."""

import unittest
from datetime import datetime, timedelta

try:
    from test_gui_layout import nx, stop_leftovers
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, stop_leftovers
from tests.patching import linux_features, set_gui  # noqa: E402

if nx is not None:
    from PyQt6.QtCore import QEvent, QPoint
    from PyQt6.QtWidgets import QApplication
    from nexus import features
    from nexus.gui.library import ShelfRow


def ago(days):
    return (datetime.now() - timedelta(days=days)).isoformat(timespec='seconds')


def games():
    out = []
    for i in range(3):                      # played this week
        out.append({'slug': f'play{i}', 'display_name': f'Played {i}', 'source': 'steam',
                    'appid': 100 + i, 'launcher': 'steam://x', 'last_played': ago(i + 1),
                    'playtime_minutes': 90, 'protondb': 'platinum'})
    for i in range(3):                      # never played
        out.append({'slug': f'new{i}', 'display_name': f'New {i}', 'source': 'nexus',
                    'launcher': '/n', 'added_at': ago(i)})
    return out


def facts_for(*slugs, **fact):
    return {'v': 1, 'built': 1, 'missing': {'steam': 0, 'hltb': 0},
            'games': {s: {'genres': ['RPG'], 'modes': ['single'], 'length': None, **fact}
                      for s in slugs}}


@unittest.skipIf(nx is None, "PyQt6 not available")
class SmartShelves(unittest.TestCase):
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
        self.win = w = nx.MainWindow()
        w.prefs.dashboard = nx.dash_default()
        w.prefs.collections = {}
        w.prefs.smart_hidden = []
        w.prefs.library_state = {}
        w.resize(1300, 850)
        w.show()
        self.load(games())
        w.go(w.LIBRARY)
        w.set_view('home', save=False)
        QApplication.processEvents()

    def tearDown(self):
        stop_leftovers(self.win.jobs)
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

    def load(self, gs, idx=None):
        w = self.win
        w._facts_idx = idx or {'v': 1, 'built': None, 'games': {}, 'missing': None}
        w._smart_sig = None
        w._home_sig = None
        w._on_library({'games': gs, 'disks': []}, '')

    def rows(self):
        return [r.title for r in self.win.home.findChildren(ShelfRow) if not r.isHidden()
                and r.parentWidget() is not None]

    def smart_rows(self):
        """Browse's rows for smart rules, genres and your shelves, in order."""
        return [r.title for r in self.win.home.group_rows()
                if r.key.startswith(('smart:', 'genre:', 'user:'))]

    def filter_keys(self):
        f = self.win.filter
        return [f.itemData(i) for i in range(f.count())
                if str(f.itemData(i) or '').startswith(('smart:', 'genre:'))]

    @linux_features
    def test_first_run_shows_play_history_shelves_without_facts(self):
        self.win.apply_filter()                  # built before the feature set changed
        self.assertEqual(self.smart_rows(), ["Continue playing", "Never played",
                                             "Runs great on Linux"])
        note = self.win.home.head.note
        self.assertFalse(note.isHidden())
        self.assertIn("learns about your games", note.text())
        self.assertIn("0 of 3", note.text())              # the Steam games have store ids
        self.load(games(), facts_for('play0'))            # built: no note
        self.assertTrue(note.isHidden())
        self.assertEqual(self.win._smart_note(games()), '')
        self.assertIn('smart:continue', self.filter_keys())

    def test_three_games_minimum(self):
        self.load(games()[:2] + games()[3:])
        self.assertEqual(self.smart_rows(), ["Never played"])

    @linux_features
    def test_no_linux_shelf_on_windows(self):
        self.win.apply_filter()
        self.assertIn("Runs great on Linux", self.smart_rows())
        saved = features.FEATURES
        features.FEATURES = features.WINDOWS
        try:
            self.win._smart_sig = None
            self.win.apply_filter()
            self.assertEqual(self.smart_rows(), ["Continue playing", "Never played"])
            self.assertNotIn('smart:linux', self.filter_keys())
        finally:
            features.FEATURES = saved

    def test_hide_and_bring_back(self):
        w = self.win
        w._hide_smart('smart:continue')
        self.assertEqual(self.smart_rows()[:1], ["Never played"])
        self.assertIn('smart:continue', self.filter_keys())          # still filterable
        head = w.home.head
        self.assertEqual(head.btn_hidden.text(), "1 hidden")
        self.assertFalse(head.btn_hidden.isHidden())
        w._unhide_smart('smart:continue')
        self.assertEqual(self.smart_rows()[:2], ["Continue playing", "Never played"])
        self.assertTrue(head.btn_hidden.isHidden())

    def test_save_as_collection(self):
        w = self.win
        w._save_smart('smart:never')
        self.assertEqual(sorted(w.prefs.collections["Never played"]), ['new0', 'new1', 'new2'])
        self.assertIn('col:Never played', w.chips)
        w._save_smart('smart:never')                                 # again: no duplicates
        self.assertEqual(len(w.prefs.collections["Never played"]), 3)

    def test_see_all_opens_the_page_and_the_filter_still_works(self):
        w = self.win
        row = w.home.row_for('smart:never')
        self.assertEqual(row.title, "Never played")
        row.see_all.emit()                          # w-98: the category's own page
        self.assertIs(w.views.currentWidget(), w.catpage)
        self.assertEqual(w.catpage.cat.key, 'smart:never')
        self.assertEqual(w.filter.currentData(), 'all')
        w.close_category()
        # The status combo's Smart group still filters the plain grid.
        w._show_filter('smart:never')
        self.assertEqual(w.filter.currentData(), 'smart:never')
        self.assertIs(w.views.currentWidget(), w.library)              # the grid
        self.assertEqual(sorted(g['slug'] for g in w._shown_games), ['new0', 'new1', 'new2'])
        self.assertEqual(w.count_label.text(), "3 of 6")
        self.assertFalse(w.btn_clear_filter.isHidden())
        w._clear_filter()
        self.assertEqual(w.filter.currentData(), 'all')
        self.assertIs(w.views.currentWidget(), w.home)
        self.assertTrue(w.btn_clear_filter.isHidden())

    def test_facts_shelves_and_genre_from_a_game_page(self):
        w = self.win
        idx = facts_for('play0', 'play1', 'play2', 'new0')
        idx['games']['new0']['genres'] = ['Racing']
        self.load(games(), idx)
        self.assertIn("RPG", self.smart_rows())
        self.assertIn('genre:RPG', self.filter_keys())
        w._set_chip('steam')
        w._show_filter('genre:Racing', from_page=True)
        self.assertEqual(w._chip, 'all')
        self.assertEqual(w.filter.currentData(), 'genre:Racing')
        self.assertEqual([g['slug'] for g in w._shown_games], ['new0'])
        self.assertEqual(w.prefs.library_state['status'], 'genre:Racing')

    def test_a_genre_outside_the_top_three_gets_a_temporary_entry(self):
        w = self.win
        w._show_filter('genre:Sports')
        self.assertEqual(w.filter.currentData(), 'genre:Sports')
        self.assertIn('genre:Sports', self.filter_keys())
        self.assertEqual(w._shown_games, [])
        w._clear_filter()
        w.apply_filter()
        self.assertNotIn('genre:Sports', self.filter_keys())

    def test_coop_key_from_the_game_page_contract(self):
        w = self.win
        self.load(games(), facts_for('play0', modes=['single', 'coop']))
        w._show_filter('smart:coop', from_page=True)
        self.assertEqual(w.filter.currentData(), 'smart:coop')
        self.assertEqual([g['slug'] for g in w._shown_games], ['play0'])
        w._show_filter('nonsense')                                    # ignored
        self.assertEqual(w.filter.currentData(), 'smart:coop')

    def test_restored_smart_filter_survives_start_up(self):
        w = nx.MainWindow()
        try:
            w.prefs.library_state = {'status': 'smart:never'}
            w._restore_library_state()
            w._facts_idx = {'v': 1, 'built': None, 'games': {}, 'missing': None}
            w.go(w.LIBRARY)
            w._on_library({'games': games(), 'disks': []}, '')
            self.assertEqual(w.filter.currentData(), 'smart:never')
        finally:
            stop_leftovers(w.jobs)
            w.close()
            w.deleteLater()

    def test_no_smart_rows_leaves_the_head_with_new_shelf(self):
        # w-51: "New shelf" stays with no smart row (w-117: in the ⋮ menu).
        self.load([games()[0], games()[3]], facts_for('play0'))
        w = self.win
        head = w.home.head
        self.assertEqual(self.smart_rows(), [])
        self.assertFalse(head.btn_new.isHidden())
        self.assertTrue(head.btn_hidden.isHidden())
        w.input.mode = 'pad'
        got = []
        w.sheet.choose = lambda title, items, *a, **k: got.extend(t for t, *_ in items)
        w._library_menu()
        self.assertEqual(got, ["Pick for me", "New shelf", "Refresh"])

    def test_shelf_menu_offers_three_actions(self):
        w = self.win
        w.input.mode = 'pad'
        w._smart_menu('smart:never', QPoint())
        from PyQt6.QtWidgets import QPushButton
        texts = [b.text() for b in w.sheet.findChildren(QPushButton)
                 if b.objectName() == 'SheetRow' and b.isVisibleTo(w.sheet)]
        self.assertEqual(texts, ["See all", "Save as collection", "Hide this shelf"])
        w.sheet.close_sheet()

    def test_home_widget(self):
        w = self.win
        dash = w.dashboard
        it = next(i for i in w.prefs.dashboard if i['id'] == 'smart')
        self.assertEqual(it['shelf'], 'unfinished')
        self.assertFalse(dash.widgets['smart'].has_content(w))       # no lengths: nothing
        self.assertIn('smart', dash._skip())
        it['shelf'] = 'never'
        self.assertEqual(sorted(g['slug'] for g in dash.widgets['smart'].games(w)),
                         ['new0', 'new1', 'new2'])
        dash.set_editing(True)
        dash._edit('smart', 'shelf')
        it = next(i for i in w.prefs.dashboard if i['id'] == 'smart')
        self.assertEqual(it['shelf'], 'short')
        self.assertEqual(dash.widgets['smart'].btn_shelf.text(), "Shelf: Short games")
        dash.set_editing(False)

    def test_dashboard_layout_keeps_the_shelf(self):
        items = nx.dash_normalize([{'id': 'installed', 'size': 'full'},
                                   {'id': 'smart', 'size': 'full', 'shelf': 'coop'}])
        self.assertEqual(next(i for i in items if i['id'] == 'smart')['shelf'], 'coop')
        items = nx.dash_normalize([{'id': 'smart', 'shelf': 'bogus'}])
        self.assertEqual(next(i for i in items if i['id'] == 'smart')['shelf'], 'unfinished')
        old = [{'id': 'jump'}, {'id': 'installed'}, {'id': 'deals'}]
        ids = [i['id'] for i in nx.dash_normalize(old)]
        self.assertEqual(ids[ids.index('installed') + 1], 'smart')

    def test_repacks_off_leaves_no_repack_wording(self):
        w = self.win
        w.config['repacks'] = False
        w.apply_filter()
        self.assertIsNone(w.home.row_for('src:nexus'))
        texts = [r.title for r in w.home.group_rows()] + [w.home.head.note.text()] + [
            b.text() for b in w.home.head.chips().values()]
        for t in texts:
            self.assertNotIn('repack', t.lower())

    def test_prefs_keep_hidden_shelves(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'library.json'
            path.write_text('{"smart_hidden": ["smart:never", "bogus", 3, "genre:RPG", '
                            '"fav", "col:A:b", "src:steam", "all", "friends:1", "smart:never"]}')
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                p = nx.LibraryPrefs()
                # w-99: any category hides (Browse); the hub's key and Play
                # together never do.
                self.assertEqual(p.smart_hidden, ['smart:never', 'genre:RPG', 'fav',
                                                  'col:A:b', 'src:steam'])
            many = [f'col:c{i}' for i in range(60)]
            path.write_text(__import__('json').dumps({'smart_hidden': many}))
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                self.assertEqual(nx.LibraryPrefs().smart_hidden, many[:50])


if __name__ == '__main__':
    unittest.main()
