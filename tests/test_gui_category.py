"""Library category pages (w-98, nexus/gui/category.py): the prefs they
keep, the mosaic header, the page's views, routing from See all, row titles
and the game page's chips, and Back / Escape / mouse Back. Offscreen, CLI
stubbed, prefs never written (except the round-trip test, in a temp dir)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_gui_layout import nx, stop_leftovers
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, stop_leftovers
from tests.patching import set_gui  # noqa: E402,F401


@unittest.skipIf(nx is None, "PyQt6 not available")
class Prefs(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'library.json'
            path.write_text(json.dumps({'some_newer_key': [1, 2]}))
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                p = nx.LibraryPrefs()
                self.assertEqual((p.category_sort, p.category_view), ({}, {}))
                p.category_sort['genre:RPG'] = 'most'
                p.category_view['col:A: b'] = 'list'
                p.save()
                q = nx.LibraryPrefs()
                self.assertEqual(q.category_sort, {'genre:RPG': 'most'})
                self.assertEqual(q.category_view, {'col:A: b': 'list'})
                self.assertEqual(json.loads(path.read_text())['some_newer_key'], [1, 2])

    def test_bad_values_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'library.json'
            many = {f'col:c{i}': 'grid' for i in range(300)}
            path.write_text(json.dumps({
                'category_sort': {'genre:RPG': 'played', 'bogus': 'name', 'fav': 'nope',
                                  'src:steam': 3, 'friends:1': 'name'},
                'category_view': many}))
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                p = nx.LibraryPrefs()
            self.assertEqual(p.category_sort, {'genre:RPG': 'played'})
            self.assertEqual(len(p.category_view), 200)
            path.write_text(json.dumps({'category_sort': [1], 'category_view': 'grid'}))
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                p = nx.LibraryPrefs()
            self.assertEqual((p.category_sort, p.category_view), ({}, {}))


def lib_games(n=8, source='steam', prefix='g'):
    """Plain library dicts, the newest played first (g0 yesterday...)."""
    from datetime import datetime, timedelta
    now = datetime.now()
    return [{'slug': f'{prefix}{i}', 'display_name': f'{prefix.upper()}{i}', 'source': source,
             'installed': True, 'playtime_minutes': 60 * (i + 1),
             'last_played': (now - timedelta(days=i + 1)).isoformat(timespec='seconds'),
             'favorite': False, 'collections': []} for i in range(n)]


def a_category(games, key='src:steam', kind='store', title="Steam"):
    from nexus import categories
    return categories.Category(key, kind, title, "Your games from Steam", nx.G['steam'], None,
                               tuple(games))


@unittest.skipIf(nx is None, "PyQt6 not available")
class Mosaic(unittest.TestCase):
    def header(self, width, games):
        from nexus.gui.category import MosaicHeader
        from PyQt6.QtWidgets import QApplication
        h = MosaicHeader()
        self.addCleanup(h.deleteLater)
        h.resize(width, h.height())
        h.set_category(a_category(games))
        h.show()
        QApplication.processEvents()
        return h

    def test_cover_count_by_width(self):
        # The header is the page width less its margins and the scroll bar.
        h = self.header(848, lib_games(8))
        self.assertEqual(h.cover_count(), 4)
        self.assertEqual(h.shown_slugs(), ['g0', 'g1', 'g2', 'g3'])   # last played first
        h.resize(1388, h.height())
        self.assertEqual(h.cover_count(), 6)
        h.resize(h.minimumSizeHint().width(), h.height())        # as narrow as it goes
        self.assertLess(h.width(), 520)
        self.assertEqual(h.cover_count(), 1)
        h.grab()                                       # paints without a hitch

    def test_one_to_three_games_one_cover(self):
        for n in (1, 2, 3):
            h = self.header(1388, lib_games(n))
            self.assertEqual(h.cover_count(), 1, n)
            self.assertFalse(h.glyph_shown())
        self.assertEqual(self.header(1388, lib_games(4)).cover_count(), 4)

    def test_no_games_shows_glyph(self):
        h = self.header(848, [])
        self.assertEqual(h.cover_count(), 0)
        self.assertTrue(h.glyph_shown())
        h.grab()

    def test_covers_fixed_while_open(self):
        games = lib_games(8)
        h = self.header(1388, games)
        first = h.shown_slugs()
        # g7 was just played: it would lead a fresh mosaic, not this one.
        fresh = [dict(g) for g in games]
        fresh[7]['last_played'] = fresh[0]['last_played'][:-2] + '59'
        del fresh[2]                                     # g2 left the category
        h.set_category(a_category(fresh))
        self.assertEqual(h.shown_slugs(), [s for s in first if s != 'g2'])   # no top-up
        self.assertNotIn('g7', h.shown_slugs())
        h.set_category(a_category(fresh, key='src:other'))       # another page: picked again
        self.assertEqual(h.shown_slugs()[0], 'g7')

    def test_header_texts_and_actions(self):
        from nexus import categories
        games = lib_games(3)
        h = self.header(848, games)
        cat = a_category(games)
        h.set_category(cat, categories.Stats(3, 360, 1, 12 * 10**9), games[0])
        self.assertEqual(h.title.toolTip(), "Steam")
        self.assertIn("3 games", h.story.text())
        self.assertIn("6 h played", h.story.text())
        self.assertIn("1 finished", h.story.text())
        self.assertIn("12 GB on disk", h.story.text())
        self.assertIn("G0", h.btn_continue.text())
        self.assertTrue(h.btn_more.isHidden())           # a store has no menu
        h.set_category(a_category(games, 'genre:RPG', 'genre', "RPG"),
                       categories.Stats(3, 0, 0, 0), None)
        self.assertTrue(h.btn_continue.isHidden())
        self.assertFalse(h.btn_more.isHidden())
        self.assertEqual(h.story.text(), "3 games")


class _Prefs:
    """What the page reads and writes of LibraryPrefs."""
    def __init__(self):
        self.category_sort, self.category_view, self.tile = {}, {}, 160
        self.saved = 0

    def save(self):
        self.saved += 1


@unittest.skipIf(nx is None, "PyQt6 not available")
class Page(unittest.TestCase):
    def page(self, games, cuts=(), size=(900, 800)):
        from nexus.gui.category import CategoryPage
        from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget
        host = QWidget()
        self.addCleanup(host.deleteLater)
        host.resize(*size)
        p = CategoryPage(_Prefs())
        QVBoxLayout(host).addWidget(p)
        host.show()
        cat = a_category(games)
        cut_games = {c.key: [g for g in games if g['slug'] in sl] for c, sl in cuts}
        p.set_category(cat, cuts=[c for c, _sl in cuts], cut_games=cut_games)
        for _ in range(3):
            QApplication.processEvents()
        return p

    @staticmethod
    def cut(key, title, n):
        from nexus import categories
        return categories.Cut(key, title, nx.G['history'], n)

    def shown_tiles(self, w):
        return [t for t in w.findChildren(nx.GameTile) if not t.isHidden()]

    def test_rows_capped_per_cut(self):
        from nexus import categories
        games = lib_games(50)
        cuts = [(self.cut('smart:continue', "Continue playing", 30), {f'g{i}' for i in range(30)}),
                (self.cut('smart:never', "Never played", 2), {'g48', 'g49'})]
        p = self.page(games, cuts)
        self.assertEqual(p.view, 'rows')                         # the default
        rows = p.row_titles()
        self.assertEqual(rows, ["Continue playing", "Never played", "All games"])
        counts = [len(r.tiles) for r in p.rows()]
        self.assertEqual(counts, [categories.ROW_CAP, 2, categories.ROW_CAP])
        self.assertTrue(p.chips_box.isHidden())                  # the rows are the cuts
        # A row's See all: the grid on that cut, for this visit only, one fill.
        fills = []
        p.shown_changed.connect(lambda: fills.append(1))
        p.rows()[1].see_all.emit()
        self.assertEqual((p.view, p.cut), ('grid', 'smart:never'))
        self.assertEqual([g['slug'] for g in p.shown_games()], ['g48', 'g49'])
        self.assertEqual(len(fills), 1)
        self.assertEqual(p.prefs.category_view, {})              # nothing saved
        self.assertEqual(p.view_seg.current(), 'grid')
        p.set_view('list')                                       # a choice is saved
        self.assertEqual(p.prefs.category_view, {'src:steam': 'list'})

    def test_cut_chip_filters_grid(self):
        games = lib_games(10)
        cuts = [(self.cut('smart:continue', "Continue playing", 3), {'g1', 'g4', 'g7'})]
        p = self.page(games, cuts)
        p.set_view('grid')
        self.assertFalse(p.chips_box.isHidden())
        chips = p.chip_buttons()
        self.assertEqual([c.text().split()[0] for c in chips], ["All", "Continue"])
        self.assertIn("10", chips[0].text())
        chips[1].click()
        self.assertEqual(sorted(p.games), ['g1', 'g4', 'g7'])
        self.assertEqual(sorted(p.grid.games), ['g1', 'g4', 'g7'])
        p.set_sort('name')
        self.assertEqual([g['slug'] for g in p.shown_games()], ['g1', 'g4', 'g7'])
        self.assertEqual(p.prefs.category_sort, {'src:steam': 'name'})
        chips[0].click()
        self.assertEqual(len(p.games), 10)
        # A refresh where the cut is gone falls back to All.
        p.set_category(a_category(games), cuts=[], cut_games={})
        self.assertIsNone(p.cut)
        self.assertEqual(len(p.shown_games()), 10)

    def test_three_thousand_games_build_only_visible_tiles(self):
        from nexus import categories
        games = lib_games(3000)
        cuts = [(self.cut(f'smart:{k}', k.title(), 1000), {f'g{i}' for i in range(n, n + 1000)})
                for k, n in (('continue', 0), ('unfinished', 1000), ('never', 2000))]
        p = self.page(games, cuts)
        self.assertLessEqual(len(self.shown_tiles(p)), 4 * categories.ROW_CAP)
        p.set_view('grid')
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        cols = p.grid.flow_host.columns(p.grid.flow_host.width())
        self.assertGreater(cols, 1)
        self.assertLess(len(self.shown_tiles(p.grid)), 8 * cols)
        self.assertEqual(len(p.grid.games), 3000)
        self.assertIs(p.header.parentWidget(), p.top)
        self.assertIs(p.top.parentWidget(), p.grid.container)       # it scrolls with the grid
        bar = p.grid.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.assertIn('g2999', p.grid.tiles)
        self.assertLess(len(self.shown_tiles(p.grid)), 8 * cols)

    def test_view_protocol_tile_for_reveal(self):
        games = lib_games(60)
        p = self.page(games, size=(900, 600))
        # Rows: tile_for finds the game in its row; reveal scrolls to it.
        t = p.tile_for('g3')
        self.assertIsInstance(t, nx.GameTile)
        self.assertEqual(t.game['slug'], 'g3')
        self.assertIs(p.tile_for(), p.tiles['g0'][0])
        p.mark_selected({'g3'})
        self.assertTrue(t._selected)
        # Grid: a far tile is built on demand.
        p.set_view('grid')
        self.assertNotIn('g59', p.tiles)
        self.assertEqual(p.tile_for('g59').game['slug'], 'g59')
        p.reveal('g59')
        self.assertIn('g59', p.tiles)
        self.assertIn('g59', p.games)
        # List: the table itself stands for its rows.
        p.set_view('list')
        self.assertIs(p.tile_for(), p.listview)
        self.assertEqual(p.listview.rowCount(), 60)
        self.assertIs(p.top.parentWidget(), p.list_top)
        p.set_view('rows')
        self.assertIs(p.top.parentWidget(), p.rows_host)
        self.assertFalse(p.top.isHidden())

    def test_more_like_this_in_every_view(self):
        from nexus import categories
        games = lib_games(6)
        p = self.page(games)
        other = categories.Category('genre:RPG', 'genre', "RPG", "t", nx.G['grid'], None,
                                    tuple(games[:3]))
        p.set_category(a_category(games), related=[other])
        for view, parent in (('rows', p.rows_host), ('grid', p.grid.container),
                             ('list', p.list_foot)):
            p.set_view(view)
            self.assertIs(p.foot.parentWidget(), parent, view)
            self.assertFalse(p.foot.isHidden(), view)
        self.assertFalse(p.list_foot.isHidden())
        p.set_view('grid')
        self.assertTrue(p.list_foot.isHidden())

    def test_tile_title_elided_after_polish(self):
        from nexus import categories
        from nexus.gui.category import MosaicTile
        from PyQt6.QtWidgets import QApplication
        long = "A very long collection name that never fits in one tile"
        t = MosaicTile(categories.Category('col:x', 'collection', long, "t", nx.G['grid'],
                                           None, tuple(lib_games(4))))
        self.addCleanup(t.deleteLater)
        t.show()
        QApplication.processEvents()
        text = t.title.text()
        self.assertTrue(text.endswith("…"), text)
        fm = t.title.fontMetrics()                     # the theme's font, measured after polish
        self.assertLessEqual(fm.horizontalAdvance(text), int(t.width() * 0.5))


def win_games():
    """12 Repacks games (r0 played yesterday...), 12 Steam (s0...), one
    favourite in a collection, and facts for a few."""
    gs = lib_games(12, 'nexus', 'r') + lib_games(12, 'steam', 's')
    for i, g in enumerate(gs):
        g['launcher'] = '/x'
        if g['source'] == 'steam':
            g['appid'] = 100 + i
    return gs


def facts_index():
    return {'v': 1, 'built': 1, 'missing': {'steam': 0, 'hltb': 0},
            'games': {'s1': {'genres': ['RPG'], 'modes': ['single', 'coop'], 'length': None},
                      's2': {'genres': ['RPG'], 'modes': ['single'], 'length': None},
                      'r3': {'genres': ['Racing'], 'modes': ['coop'], 'length': None}}}


class _Window(unittest.TestCase):
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
        for name, ret in (('power_caps', set()), ('power', True), ('bluetooth_pads', []),
                          ('bluetooth_disconnect', True)):
            p = mock.patch(f'nexus.platform.{name}', return_value=ret)
            p.start()
            self.addCleanup(p.stop)
        self.win = w = nx.MainWindow()
        w.prefs.dashboard = nx.dash_default()
        w.prefs.collections = {'Couch': ['s3', 'r1']}
        w.prefs.favorites = {'s5'}
        w.prefs.smart_hidden = []
        w.prefs.library_state = {}
        w.prefs.category_sort, w.prefs.category_view = {}, {}
        w._mirror_collections()
        w.resize(1300, 850)
        w.show()
        w._facts_idx = facts_index()
        w._facts_stamp = ('fixed',)
        w._sync_facts = lambda: False
        w._on_library({'games': win_games(), 'disks': []}, '')
        w.go(w.LIBRARY)
        w.set_view('home', save=False)
        self.pump()

    def tearDown(self):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtWidgets import QApplication
        stop_leftovers(self.win.jobs)
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

    @staticmethod
    def pump(n=4):
        from PyQt6.QtTest import QTest
        for _ in range(n):
            QTest.qWait(5)

    def on_page(self, key=None) -> bool:
        w = self.win
        shown = w.views.currentWidget() is w.catpage and w._category_open()
        return shown and (key is None or w.catpage.cat.key == key)

    def slugs(self):
        return sorted(g['slug'] for g in self.win.catpage.cat.games)


@unittest.skipIf(nx is None, "PyQt6 not available")
class Routing(_Window):
    def test_see_all_opens_page_for_each_kind(self):
        w = self.win
        rows = {r.title: r for r in w.home.findChildren(nx.ShelfRow) if not r.isHidden()}
        rows["Steam"].see_all.emit()                       # a store row
        self.assertTrue(self.on_page('src:steam'))
        self.assertEqual(len(self.slugs()), 12)
        self.assertTrue(w._lib_bar.isHidden())
        self.assertTrue(w._lib_chips.isHidden())
        self.assertIn("12 games", w.catpage.header.story.text())
        w.close_category()
        self.assertFalse(w._category_open())
        self.assertIs(w.views.currentWidget(), w.home)
        self.assertFalse(w._lib_bar.isHidden())
        # A smart row's See all, and its title.
        row = next(x for x in w.home._smart if isinstance(x, nx.ShelfRow)
                   and x.title == "Continue playing")
        row.see_all.emit()
        self.assertTrue(self.on_page('smart:continue'))
        w.close_category()
        from PyQt6.QtTest import QTest
        from PyQt6.QtCore import Qt
        QTest.mouseClick(row.name_label, Qt.MouseButton.LeftButton)
        self.assertTrue(self.on_page('smart:continue'))
        # Favourites, a collection, a shelf you built, a genre.
        for key, want in (('fav', ['s5']), ('col:Couch', ['r1', 's3']), ('genre:RPG', ['s1', 's2'])):
            self.assertTrue(w.open_category(key), key)
            self.assertTrue(self.on_page(key))
            self.assertEqual(self.slugs(), want)
        shelf = w.prefs.add_shelf({'name': "Long", 'match': 'all', 'rules': [
            {'field': 'playtime', 'op': 'atleast', 'value': 10}]})
        self.assertTrue(w.open_category('user:' + shelf['id']))
        self.assertEqual(self.slugs(), sorted(f'{p}{i}' for p in 'rs' for i in (9, 10, 11)))
        self.assertEqual(w.catpage.header.title.toolTip(), "Long")

    def test_genre_and_coop_chip_open_page(self):
        w = self.win
        w.details.filter_library.emit('genre:RPG')
        self.assertTrue(self.on_page('genre:RPG'))
        w.details.filter_library.emit('smart:coop')
        self.assertTrue(self.on_page('smart:coop'))
        self.assertEqual(self.slugs(), ['r3', 's1'])
        self.assertEqual(w.filter.currentData(), 'all')         # the combo is left alone

    def test_unknown_or_empty_key_does_not_crash(self):
        w = self.win
        for key in ('bogus', 'col:Nope', 'friends:1', '', None, 'user:zz'):
            self.assertFalse(w.open_category(key), key)
            self.assertFalse(w._category_open())
        # No games: no page (a shelf of yours aside).
        w.prefs.favorites = set()
        w._on_library({'games': win_games(), 'disks': []}, '')
        for key in ('genre:Horror', 'genre:Bogus', 'src:epic', 'fav'):
            self.assertFalse(w.open_category(key), key)
            self.assertFalse(w._category_open())
        shelf = w.prefs.add_shelf({'name': "None yet", 'match': 'all', 'rules': [
            {'field': 'playtime', 'op': 'atleast', 'value': 100}]})
        self.assertTrue(w.open_category('user:' + shelf['id']))
        self.assertIn("rules", w.catpage._rows_empty.text())
        w.close_category()
        # A Nexus genre before the facts index has anything: a page that says so.
        w._facts_idx = {'v': 1, 'built': None, 'games': {}, 'missing': None}
        self.assertFalse(w.open_category('genre:Bogus'))
        self.assertTrue(w.open_category('genre:Horror'))
        self.assertEqual(w.catpage.cat.games, ())
        self.assertFalse(w.catpage._rows_empty.isHidden())
        self.assertIn("learns", w.catpage._rows_empty.text())
        w.catpage.set_view('grid')
        w.catpage.set_view('list')
        # The collection goes while its page is open: back to Browse.
        w._facts_idx = facts_index()
        w.open_category('col:Couch')
        w._delete_collection('Couch')
        self.assertFalse(w._category_open())

    def test_back_to_a_deleted_category_lands_on_browse(self):
        w = self.win
        w.open_category('col:Couch')
        w.open_category('src:steam')                 # history: Browse, Couch
        w.prefs.delete_collection('Couch')
        w._collections_changed()
        self.assertTrue(w.go_back())                 # Couch is gone
        self.assertFalse(w._category_open())
        self.assertIs(w.views.currentWidget(), w.home)
        self.assertFalse(w._lib_bar.isHidden())

    def test_status_combo_still_filters_grid(self):
        w = self.win
        w._show_filter('smart:never')
        self.assertFalse(w._category_open())
        self.assertEqual(w.filter.currentData(), 'smart:never')
        self.assertIs(w.views.currentWidget(), w.library)
        # From an open page, too: the page closes for the filtered grid.
        w.open_category('src:steam')
        w._show_filter('smart:continue')
        self.assertFalse(w._category_open())
        self.assertIs(w.views.currentWidget(), w.library)
        self.assertEqual(w.filter.currentData(), 'smart:continue')

    def test_pick_for_me_stays_inside(self):
        w = self.win
        w.open_category('col:Couch')
        picked = []
        w.surprise = lambda pool=None: picked.append([g['slug'] for g in pool])
        w.catpage.header.btn_pick.click()
        self.assertEqual(sorted(picked[0]), ['r1', 's3'])
        # The cut in the grid narrows it.
        w.open_category('src:steam')
        w.catpage.set_view('grid')
        w.catpage.set_cut(next(iter(w.catpage._cut_games)))
        w.catpage.header.btn_pick.click()
        self.assertEqual(picked[1], [g['slug'] for g in w.catpage.shown_games()])
        self.assertLess(len(picked[1]), 12)
        # Ctrl+D picks from the page as well.
        self.assertEqual(w._shown_games, w.catpage.shown_games())


@unittest.skipIf(nx is None, "PyQt6 not available")
class History(_Window):
    def browse_row(self, title):
        return next(r for r in self.win.home.findChildren(nx.ShelfRow)
                    if not r.isHidden() and r.title == title)

    def test_back_returns_to_browse_scroll_and_focus(self):
        w = self.win
        w.activateWindow()
        bar = w.home.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        tile = self.browse_row("Steam").tiles[4]
        tile.setFocus()
        self.pump()
        self.browse_row("Steam").see_all.emit()
        self.assertTrue(self.on_page('src:steam'))
        self.pump()
        w.go_back()
        self.pump(6)
        self.assertFalse(w._category_open())
        self.assertIs(w.views.currentWidget(), w.home)
        self.assertEqual(bar.value(), bar.maximum())
        from PyQt6.QtWidgets import QApplication
        self.assertIs(QApplication.focusWidget(), tile)
        w.go_forward()                                  # and forward again
        self.assertTrue(self.on_page('src:steam'))

    def test_escape_closes_page(self):
        w = self.win
        w.open_category('fav')
        w.search.clearFocus()
        w._escape()
        self.assertFalse(w._category_open())
        self.assertEqual(w.current(), w.LIBRARY)
        # With a game selected, Escape closes the game page first.
        w.open_category('fav')
        from PyQt6.QtCore import Qt
        w._tile_clicked(w.game('s5'), Qt.KeyboardModifier.NoModifier)
        self.assertTrue(w.details.isVisible())
        w._escape()
        self.assertTrue(w._category_open())
        w._escape()
        self.assertFalse(w._category_open())

    def test_mouse_back_button(self):
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtWidgets import QApplication
        w = self.win
        w.open_category('col:Couch')
        ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
                         Qt.MouseButton.BackButton, Qt.MouseButton.BackButton,
                         Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(w.catpage, ev)
        self.assertFalse(w._category_open())
        self.assertEqual(w.current(), w.LIBRARY)

    def test_more_like_this_then_back_twice(self):
        w = self.win
        w.open_category('genre:RPG')
        from nexus.gui.category import MosaicTile
        tiles = [t for t in w.catpage.related_box.findChildren(MosaicTile)]
        self.assertTrue(tiles)
        other = tiles[0].key
        tiles[0].click()
        self.assertTrue(self.on_page(other))
        self.assertTrue(w.go_back())
        self.assertTrue(self.on_page('genre:RPG'))
        self.assertTrue(w.go_back())
        self.assertFalse(w._category_open())
        self.assertIs(w.views.currentWidget(), w.home)

    def test_from_home_back_goes_home(self):
        w = self.win
        w.go(w.HOME)
        w._see_all('steam', 'name')                       # a Home row's See all
        self.assertTrue(self.on_page('src:steam'))
        self.assertEqual(w.catpage.sort.currentData(), 'name')
        self.assertNotIn('src:steam', w.prefs.category_sort)   # a default, not a choice
        w.go_back()
        self.assertEqual(w.current(), w.HOME)
        # Re-clicking the Library tab on a page closes it.
        w.go(w.LIBRARY)
        self.assertTrue(self.on_page('src:steam'))
        b = w.tabs.item(w.LIBRARY)
        b.pressed.emit()
        b.clicked.emit()
        self.assertFalse(w._category_open())

    def test_typing_a_search_closes_the_page_and_back_returns(self):
        w = self.win
        w.open_category('src:steam')
        w.search.setText("s1")
        self.assertFalse(w._category_open())
        w.search.clear()
        w.go_back()
        self.assertTrue(self.on_page('src:steam'))


if __name__ == '__main__':
    unittest.main()
