"""Library category pages (w-98, nexus/gui/category.py): the prefs they
keep, the mosaic header, the page's views, routing from See all, row titles
and the game page's chips, and Back / Escape / mouse Back. Browse's grouped
rows and the All categories hub (w-99). Offscreen, CLI
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

    def test_pins_round_trip(self):
        """w-100: category_pins, in order, cleaned on load and on save."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'library.json'
            path.write_text(json.dumps({'category_pins': ['genre:RPG', 'bogus', 'genre:RPG',
                                                          'col:A: b']}))
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                p = nx.LibraryPrefs()
                self.assertEqual(p.category_pins, ['genre:RPG', 'col:A: b'])
                p.category_pins = p.category_pins + ['fav', 'friends:1']
                p.save()
                self.assertEqual(json.loads(path.read_text())['category_pins'],
                                 ['genre:RPG', 'col:A: b', 'fav'])
                self.assertEqual(nx.LibraryPrefs().category_pins,
                                 ['genre:RPG', 'col:A: b', 'fav'])

    def test_older_json_without_pins(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'library.json'
            path.write_text(json.dumps({'favorites': ['a']}))
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                self.assertEqual(nx.LibraryPrefs().category_pins, [])
            for bad in ('fav', {'fav': 1}, 7, None):
                path.write_text(json.dumps({'category_pins': bad}))
                with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                    self.assertEqual(nx.LibraryPrefs().category_pins, [], bad)

    def test_rename_collection_moves_pin(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'library.json'
            path.write_text(json.dumps({'collections': {'Couch': [], 'Short': []},
                                        'category_pins': ['fav', 'col:Couch', 'col:Short']}))
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                p = nx.LibraryPrefs()
                self.assertEqual(p.rename_collection('Couch', 'Sofa'), 'Sofa')
                self.assertEqual(p.category_pins, ['fav', 'col:Sofa', 'col:Short'])
                self.assertEqual(nx.LibraryPrefs().category_pins,
                                 ['fav', 'col:Sofa', 'col:Short'])

    def test_delete_collection_drops_pin(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'library.json'
            path.write_text(json.dumps({'collections': {'Couch': []},
                                        'category_pins': ['col:Couch', 'fav']}))
            with mock.patch.object(nx.LibraryPrefs, 'PATH', path):
                p = nx.LibraryPrefs()
                p.delete_collection('Couch')
                self.assertEqual(p.category_pins, ['fav'])
                self.assertEqual(nx.LibraryPrefs().category_pins, ['fav'])


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


@unittest.skipIf(nx is None, "PyQt6 not available")
class Tabs(unittest.TestCase):
    """CategoryTabs (w-100): Browse, the pinned categories, All categories as
    underline tabs; rebuilt only when the tabs change."""

    def strip(self, width=900):
        from nexus.gui.category import CategoryTabs
        from PyQt6.QtWidgets import QApplication, QHBoxLayout, QWidget
        host = QWidget()
        self.addCleanup(host.deleteLater)
        host.resize(width, 80)
        t = CategoryTabs()
        QHBoxLayout(host).addWidget(t)
        host.show()
        QApplication.processEvents()
        return host, t

    TABS = [('browse', "Browse"), ('genre:RPG', "RPG"), ('fav', "Favourites"),
            ('all', "All categories")]

    def test_same_signature_keeps_buttons(self):
        from PyQt6.QtWidgets import QApplication
        _host, t = self.strip()
        picked = []
        t.picked.connect(picked.append)
        t.set_tabs(self.TABS, 'browse')
        seg = t.seg
        b = t.button('genre:RPG')
        t.set_tabs(list(self.TABS), 'genre:RPG')          # same tabs: kept, current moves
        self.assertIs(t.seg, seg)
        self.assertIs(t.button('genre:RPG'), b)
        self.assertEqual(t.current(), 'genre:RPG')
        self.assertEqual(picked, [])                       # set_tabs never emits
        t.set_tabs(self.TABS[:2] + self.TABS[3:], 'browse')   # Favourites unpinned
        QApplication.processEvents()
        self.assertIsNot(t.seg, seg)
        self.assertEqual(t.keys(), ['browse', 'genre:RPG', 'all'])
        t.button('all').click()
        self.assertEqual(picked, ['all'])

    def test_rebuild_keeps_focus_off_hidden(self):
        from PyQt6.QtWidgets import QApplication
        host, t = self.strip()
        host.activateWindow()
        t.set_tabs(self.TABS, 'fav')
        t.button('fav').setFocus()
        QApplication.processEvents()
        self.assertIs(QApplication.focusWidget(), t.button('fav'))
        t.set_tabs(self.TABS[:2] + self.TABS[3:], 'browse')
        f = QApplication.focusWidget()
        self.assertIs(f, t.button('browse'))               # moved before the old one hid
        QApplication.processEvents()
        self.assertIs(QApplication.focusWidget(), t.button('browse'))

    def test_eight_tabs_fit_900px(self):
        from PyQt6.QtWidgets import QApplication
        _host, t = self.strip(900)
        long = "A really long collection name that goes on"
        tabs = [('browse', "Browse")] + [(f'col:{long} {i}', f"{long} {i}")
                                         for i in range(6)] + [('all', "All categories")]
        t.set_tabs(tabs, 'browse')
        for _ in range(3):
            QApplication.processEvents()
        right = max(t.button(k).geometry().right() for k, _ in tabs)
        self.assertLessEqual(t.seg.x() + right, t.width())
        for k, title in tabs:
            b = t.button(k)
            self.assertTrue(b.text())
            self.assertLessEqual(len(b.text()), 18, b.text())
            self.assertEqual(b.toolTip(), title)
        self.assertEqual(t.button('browse').text(), "Browse")   # short ones stay whole
        self.assertEqual(t.button('all').text(), "All categories")


@unittest.skipIf(nx is None, "PyQt6 not available")
class Pin(unittest.TestCase):
    def test_pin_button_states(self):
        """w-100: Pin / Pinned in the header; full (6 pinned) greys it out
        with a reason; a click asks for the toggle."""
        from nexus.gui.category import MosaicHeader
        from PyQt6.QtWidgets import QApplication
        h = MosaicHeader()
        self.addCleanup(h.deleteLater)
        h.resize(900, h.height())
        h.set_category(a_category(lib_games(4)))
        h.show()
        QApplication.processEvents()
        asked = []
        h.pin.connect(lambda: asked.append(1))
        h.set_pin_state('off')
        self.assertEqual((h.btn_pin.text(), h.btn_pin.isEnabled(), h.btn_pin.isChecked()),
                         ("Pin", True, False))
        h.btn_pin.click()
        self.assertEqual(asked, [1])
        h.set_pin_state('on')
        self.assertEqual((h.btn_pin.text(), h.btn_pin.isEnabled(), h.btn_pin.isChecked()),
                         ("Pinned", True, True))
        self.assertIn("Unpin", h.btn_pin.toolTip())
        h.set_pin_state('full')
        self.assertEqual((h.btn_pin.text(), h.btn_pin.isEnabled()), ("Pin", False))
        self.assertEqual(h.btn_pin.toolTip(), "6 pinned, unpin one first")


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

    def theatre_page(self, games, size=(1600, 900)):
        from nexus.gui.category import CategoryPage
        from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget
        host = QWidget()
        self.addCleanup(host.deleteLater)
        host.resize(*size)
        prefs = _Prefs()
        prefs.category_view = {'src:steam': 'list'}       # the desktop's: not for Theatre
        p = CategoryPage(prefs, theatre=True, scale=1.4, tile=230)
        QVBoxLayout(host).addWidget(p)
        host.show()
        p.set_category(a_category(games),
                       cuts=[self.cut('smart:continue', "Continue playing", 2)],
                       cut_games={'smart:continue': games[:2]})
        for _ in range(3):
            QApplication.processEvents()
        return p

    def test_theatre_page_grid_only_big_tiles(self):
        from nexus.gui.category import CategoryPage
        p = self.theatre_page(lib_games(12))
        self.assertEqual(p.view, 'grid')
        self.assertTrue(p.bar.isHidden())                  # no cuts, sort or view switch
        self.assertTrue(p.foot.isHidden())                 # no More like this
        self.assertTrue(p.header.btn_back.isHidden())      # B is the way back
        self.assertTrue(p.header.btn_more.isHidden())
        desk = CategoryPage(_Prefs())
        self.addCleanup(desk.deleteLater)
        self.assertGreater(p.header.height(), desk.header.height())
        tiles = self.shown_tiles(p)
        self.assertTrue(tiles)
        for t in tiles:
            self.assertTrue(t.theatre)
            self.assertEqual(t.tw, 230)
        self.assertIs(p.tile_for(), p.grid.tile_for())
        p.set_view('rows')                                 # never anything but the grid
        self.assertEqual(p.view, 'grid')

    def test_theatre_page_writes_no_category_view(self):
        p = self.theatre_page(lib_games(6))
        self.assertEqual(p.prefs.category_view, {'src:steam': 'list'})
        self.assertEqual(p.prefs.saved, 0)
        p.set_sort('name')                                 # the sort is the page's own
        self.assertEqual(p.prefs.category_sort, {'src:steam': 'name'})
        self.assertEqual([g['slug'] for g in p.shown_games()][:2], ['g0', 'g1'])
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
        lines = t.title.text().split('\n')
        self.assertEqual(len(lines), 2)                 # w-99: two lines, then elided
        self.assertTrue(lines[1].endswith("…"), lines)
        self.assertTrue(long.startswith(lines[0]))      # broken between words
        fm = t.title.fontMetrics()                     # the theme's font, measured after polish
        for line in lines:
            self.assertLessEqual(fm.horizontalAdvance(line), t.text_width())
        # The text keeps left of the first cover.
        self.assertLess(t.text_width(), t.width() - 2 * t.layout().contentsMargins().left())

    def test_short_title_one_line_long_word_elided(self):
        from nexus.gui.category import two_lines
        from PyQt6.QtGui import QFontMetrics
        from PyQt6.QtWidgets import QApplication
        fm = QFontMetrics(QApplication.font())
        self.assertEqual(two_lines("RPG", fm, 200), ["RPG"])
        two = two_lines("Continue playing", fm, fm.horizontalAdvance("Continue") + 4)
        self.assertEqual(two, ["Continue", "playing"])
        one = two_lines("Supercalifragilistic games", fm, fm.horizontalAdvance("Super"))
        self.assertEqual(len(one), 1)
        self.assertTrue(one[0].endswith("…"))


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
        row = w.home.row_for('smart:continue')
        self.assertEqual(row.title, "Continue playing")
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


GENRES8 = ('RPG', 'Action', 'Adventure', 'Indie', 'Strategy', 'Racing', 'Puzzle', 'Horror')


def rich_library(n_steam=40, n_nexus=10):
    """A library with something in every smart rule (5 games each, so 7
    rules on Windows, 8 with Proton), 8 genres of 4+ games, two stores.
    Returns (games, facts index)."""
    from datetime import datetime, timedelta
    now = datetime.now()

    def ago(d):
        return (now - timedelta(days=d)).isoformat(timespec='seconds')
    games, facts = [], {}
    for i in range(n_steam):
        g = {'slug': f'b{i}', 'display_name': f'B{i:04d}', 'source': 'steam', 'installed': True,
             'launcher': '/x', 'appid': 1000 + i, 'favorite': False, 'collections': [],
             'playtime_minutes': 30, 'last_played': ago(60)}
        f = {'genres': [GENRES8[i % 8]], 'modes': ['single'], 'length': None,
             'status': 'released'}
        band = i // 5
        if band == 0:                                       # continue
            g.update(playtime_minutes=60, last_played=ago(2))
            f['length'] = {'main': 20}
        elif band == 1:                                     # unfinished
            g.update(playtime_minutes=120)
            f['length'] = {'main': 20}
        elif band == 2:                                     # almost done
            g.update(playtime_minutes=900)
            f['length'] = {'main': 17}
        elif band == 3:                                     # never played, short
            g.update(playtime_minutes=0, last_played=None)
            f['length'] = {'main': 3}
        elif band == 4:
            f['modes'] = ['single', 'coop']
        elif band == 5:
            f['status'] = 'early_access'
        elif band == 6:
            g['protondb'] = 'platinum'
        games.append(g)
        facts[g['slug']] = f
    for i in range(n_nexus):
        games.append({'slug': f'n{i}', 'display_name': f'N{i:04d}', 'source': 'nexus',
                      'installed': True, 'launcher': '/x', 'favorite': False, 'collections': [],
                      'playtime_minutes': 45, 'last_played': ago(i + 1)})
    idx = {'v': 1, 'built': 1, 'missing': {'steam': 0, 'hltb': 0}, 'games': facts}
    return games, idx


class _Rich(_Window):
    """The window on rich_library()'s Browse."""

    def setUp(self):
        super().setUp()
        self.load()

    def load(self, games=None, idx=None):
        w = self.win
        w.prefs.collections = {'Couch': ['b3', 'n4']}
        w.prefs.favorites = {'b1', 'n2'}
        w._mirror_collections()
        gs, ix = rich_library()
        w._facts_idx = idx or ix
        w._on_library({'games': games or gs, 'disks': []}, '')
        if w._category_open():
            w.close_category(_history=False)
        w.set_view('home', save=False)
        self.pump()

    def order(self):
        """Browse's group layout as ('kicker', group) and row keys."""
        from nexus.gui.library import BrowseKicker
        out = []
        for wdg in self.win.home._order:
            out.append(('kicker', wdg.gkey) if isinstance(wdg, BrowseKicker) else wdg.key)
        return out

    def on_hub(self) -> bool:
        w = self.win
        return w.views.currentWidget() is w.cathub and w._hub_open()


@unittest.skipIf(nx is None, "PyQt6 not available")
class Browse(_Rich):
    def test_groups_kickers_order(self):
        from nexus import categories, smart
        w = self.win
        o = self.order()
        kick = [x[1] for x in o if isinstance(x, tuple)]
        self.assertEqual(kick, ['for_you', 'yours', 'genres', 'stores'])
        i = {g: o.index(('kicker', g)) for g in kick}
        for_you = o[i['for_you'] + 1:i['yours']]
        self.assertEqual(len(for_you), 6)                                 # capped
        self.assertEqual(for_you, [k for k in smart.rule_keys(True) if k in for_you])
        self.assertEqual(o[i['yours'] + 1:i['genres']], ['fav', 'col:Couch'])
        genres = o[i['genres'] + 1:i['stores']]
        self.assertEqual(len(genres), 6)
        self.assertTrue(all(k.startswith('genre:') for k in genres))
        self.assertEqual(o[i['stores'] + 1:], ['src:nexus', 'src:steam'])   # stores last
        head = w.home.head
        self.assertEqual([b.text() for b in head.chips().values()],
                         ["For you", "Yours", "Genres", "Stores"])
        self.assertTrue(head.note.isHidden())                             # facts built
        self.assertIsNotNone(w.home.kicker('genres').btn_all)
        self.assertIsNone(w.home.kicker('stores').btn_all)
        self.assertEqual(w.home.kicker('yours').text.text(), "YOURS")
        # Installing sits above the groups; with a job it is the rows layout.
        self.assertEqual(w.home.lay.indexOf(head), 0)
        self.assertEqual(categories.HUB, 'all')

    def test_reload_repoints_rows_and_tiles_by_key(self):
        w = self.win
        rows = {r.key: r for r in w.home.group_rows()}
        tiles = {id(t) for r in rows.values() for t in r.tiles}
        calls = []
        orig = w.home.set_groups
        w.home.set_groups = lambda *a, **k: (calls.append(1), orig(*a, **k))
        gs, idx = rich_library()
        w._on_library({'games': gs, 'disks': []}, '')        # a reload: fresh dicts
        self.pump()
        self.assertEqual(len(calls), 1)                       # filled once
        now = {r.key: r for r in w.home.group_rows()}
        self.assertEqual({k: id(r) for k, r in now.items()}, {k: id(r) for k, r in rows.items()})
        self.assertEqual({id(t) for r in now.values() for t in r.tiles}, tiles)
        self.assertIs(now['src:steam'].tiles[0].game, w.game(now['src:steam'].tiles[0].game['slug']))

    def test_reload_keeps_scroll_place(self):
        w = self.win
        bar = w.home.verticalScrollBar()
        self.assertGreater(bar.maximum(), 600)
        bar.setValue(bar.maximum() // 2)
        self.pump()
        before = bar.value()
        self.load()
        self.pump(6)
        self.assertEqual(bar.value(), before)

    def test_rows_capped_count_shows_total(self):
        from nexus import categories
        row = self.win.home.row_for('src:steam')
        self.assertEqual(len(row.tiles), categories.ROW_CAP)
        self.assertEqual(row.count_label.text(), "40")
        # See all opens the store's page on Name, as the row once was.
        row.see_all.emit()
        self.assertTrue(self.on_page('src:steam'))
        self.assertEqual(self.win.catpage.sort.currentData(), 'name')
        self.assertEqual(len(self.win.catpage.cat.games), 40)

    def test_three_thousand_games_capped_tiles(self):
        from nexus import categories
        from nexus.gui.tiles import GameTile
        gs, idx = rich_library(n_steam=2990, n_nexus=10)
        self.load(gs, idx)
        w = self.win
        rows = w.home.group_rows()
        self.assertTrue(rows)
        for r in rows:
            self.assertLessEqual(len(r.tiles), categories.ROW_CAP, r.key)
        live = [t for t in w.home.findChildren(GameTile) if not t.isHidden()]
        self.assertLessEqual(len(live), len(rows) * categories.ROW_CAP)
        self.assertEqual(w.home.row_for('src:steam').count_label.text(), "2990")

    def test_continue_wide_yours_looks(self):
        from nexus.gui.library import HomeView
        home = self.win.home
        cont = home.row_for('smart:continue')
        self.assertEqual(cont.kind, 'wide')
        self.assertEqual(cont.tile_w, HomeView.WIDE_TILE)
        self.assertEqual(home.row_for('smart:never').kind, 'cover')
        fav = home.row_for('fav')
        self.assertEqual(fav.look[0], nx.G['star'])
        self.assertIn(fav.look[1], ('col_gold', 'accent'))
        self.assertIsNone(home.row_for('src:steam').look)
        # A collection with a look wears it; a recolour makes the row again.
        w = self.win
        w.prefs.collection_look['Couch'] = {'icon': 'heart', 'color': 'rose'}
        w._collections_changed()
        self.pump()
        look = home.row_for('col:Couch').look
        self.assertIsNotNone(look)
        self.assertEqual(look[1], 'col_rose')

    def test_jump_chip_scrolls_and_moves_focus(self):
        from PyQt6.QtWidgets import QApplication
        w = self.win
        w.activateWindow()
        bar = w.home.verticalScrollBar()
        bar.setValue(0)
        self.pump()
        w.home.head.chips()['genres'].click()
        self.pump()
        self.assertGreater(bar.value(), 0)
        k = w.home.kicker('genres')
        from PyQt6.QtCore import QPoint
        y = k.mapTo(w.home.viewport(), QPoint(0, 0)).y()
        self.assertTrue(0 <= y < 80, y)
        first = next(r for r in w.home.group_rows() if r.key.startswith('genre:'))
        self.assertIs(QApplication.focusWidget(), first.tiles[0])

    def test_home_mode_unchanged(self):
        from nexus.gui.library import BrowseHead, BrowseKicker
        hv = self.win.homeview
        self.assertEqual(hv.mode, 'home')
        self.assertIsNone(hv.head)
        self.assertEqual(hv.group_rows(), [])
        self.assertEqual(hv.findChildren(BrowseHead), [])
        self.assertEqual(hv.findChildren(BrowseKicker), [])
        self.assertEqual(hv.group_lay.count(), 0)

    def test_hide_any_row_and_restore(self):
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QPushButton
        w = self.win
        for key in ('fav', 'col:Couch', 'src:steam'):
            w._hide_smart(key)
            self.assertIsNone(w.home.row_for(key), key)
        self.assertEqual(w.prefs.smart_hidden, ['fav', 'col:Couch', 'src:steam'])
        self.assertEqual(w.home.head.btn_hidden.text(), "3 hidden")
        # Gone from Browse only: the hub and the page still have them.
        w._open_hub()
        self.assertTrue(self.on_hub())
        for key in ('fav', 'col:Couch', 'src:steam'):
            self.assertIsNotNone(w.cathub.tile(key), key)
        w.close_category()
        w.input.mode = 'pad'
        w._smart_hidden_menu(QPoint())
        texts = [b.text() for b in w.sheet.findChildren(QPushButton)
                 if b.objectName() == 'SheetRow' and b.isVisibleTo(w.sheet)]
        self.assertEqual(texts, ["Show “Favourites” again", "Show “Couch” again",
                                 "Show “Steam” again"])
        w.sheet.close_sheet()
        # A store's or favourites' ⋮: See all and Hide only.
        w._smart_menu('src:nexus', QPoint())
        texts = [b.text() for b in w.sheet.findChildren(QPushButton)
                 if b.objectName() == 'SheetRow' and b.isVisibleTo(w.sheet)]
        self.assertEqual(texts, ["See all", "Hide this row"])
        w.sheet.close_sheet()
        w.input.mode = 'mouse'
        w._unhide_smart('fav')
        self.assertIsNotNone(w.home.row_for('fav'))
        self.assertEqual(w.home.head.btn_hidden.text(), "2 hidden")
        # A hidden collection that is deleted no longer counts.
        w._delete_collection('Couch')
        self.assertEqual(w.home.head.btn_hidden.text(), "1 hidden")

    def test_new_shelf_stays(self):
        w = self.win
        b = w.home.head.btn_new
        self.assertFalse(b.isHidden())
        b.click()
        self.assertTrue(w.modal.isVisible())
        w._escape()
        self.pump()
        self.assertEqual(w.prefs.smart_shelves, [])

    def test_windows_set_no_linux_row_no_heroic(self):
        from nexus import features
        gs, idx = rich_library()
        gs.append({'slug': 'h0', 'display_name': 'H0', 'source': 'heroic', 'installed': True,
                   'launcher': '/x', 'playtime_minutes': 5, 'last_played': None})
        saved = features.FEATURES
        try:
            features.FEATURES = features.ALL
            self.load([dict(g) for g in gs], idx)
            keys = [r.key for r in self.win.home.group_rows()]
            self.assertIn('src:heroic', keys)
            features.FEATURES = features.WINDOWS
            self.load([dict(g) for g in gs], idx)
            keys = [r.key for r in self.win.home.group_rows()]
            self.assertNotIn('smart:linux', keys)
            self.assertNotIn('src:heroic', keys)
            self.win._open_hub()
            self.assertIsNone(self.win.cathub.tile('smart:linux'))
            self.assertIsNone(self.win.cathub.tile('src:heroic'))
        finally:
            features.FEATURES = saved


@unittest.skipIf(nx is None, "PyQt6 not available")
class Hub(unittest.TestCase):
    def groups(self):
        from nexus import categories
        gs, idx = rich_library()
        return categories.categories(gs, idx['games'], categories.PrefsView(), linux=True)

    def hub(self):
        from nexus.gui.category import CategoryHub
        from PyQt6.QtCore import QCoreApplication, QEvent
        from PyQt6.QtWidgets import QApplication
        h = CategoryHub()

        def gone():
            h.close()
            h.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self.addCleanup(gone)
        h.resize(900, 600)
        h.set_groups(self.groups())
        h.show()
        QApplication.processEvents()
        return h

    def test_tiles_per_group_include_hidden(self):
        h = self.hub()
        self.assertEqual(h.group_keys(), ['for_you', 'genres', 'stores'])
        self.assertEqual(len(h.tiles_in('for_you')), 8)                      # uncapped
        self.assertEqual(len(h.tiles_in('genres')), 8)
        self.assertEqual(h.title.text(), "All categories")

    def test_tiles_reused_by_key(self):
        from unittest import mock
        from nexus import categories
        from nexus.gui.category import MosaicTile
        h = self.hub()
        before = {k: id(h.tile(k)) for g in self.groups() for k in [c.key for c in g.cats]}
        with mock.patch.object(MosaicTile, 'set_category') as setc:
            h.set_groups(self.groups())                       # nothing changed: untouched
            setc.assert_not_called()
        groups = self.groups()
        steam = categories.lookup(groups, 'src:steam')
        fewer = categories.Category(steam.key, steam.kind, steam.title, steam.tip, steam.glyph,
                                    None, steam.games[:3])
        groups[-1] = categories.Group('stores', "Stores", (fewer,))
        h.set_groups(groups)
        self.assertEqual(id(h.tile('src:steam')), before['src:steam'])
        self.assertEqual(h.tile('src:steam').count.text(), "3 games")
        self.assertIsNone(h.tile('src:nexus'))
        self.assertEqual(id(h.tile('genre:RPG')), before['genre:RPG'])
        # One game out, another in: the count stays, the tile still follows.
        swapped = categories.Category(steam.key, steam.kind, steam.title, steam.tip,
                                      steam.glyph, None, steam.games[1:4])
        groups[-1] = categories.Group('stores', "Stores", (swapped,))
        h.set_groups(groups)
        self.assertEqual(h.tile('src:steam').count.text(), "3 games")
        self.assertEqual(set(h.tile('src:steam')._by), {g['slug'] for g in steam.games[1:4]})
        self.assertEqual(len(h.findChildren(MosaicTile)), sum(len(g.cats) for g in groups)
                         + 1)                          # the dropped one waits for deleteLater

    def test_jump_to_group(self):
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QApplication
        h = self.hub()
        before = QApplication.focusWidget()
        h.jump_to('stores')
        first = h.tiles_in('stores')[0]
        self.assertIs(h.tile_for(), first)                  # where the window focuses
        QApplication.processEvents()                         # the second, laid-out pass
        bar = h.verticalScrollBar()
        self.assertGreater(bar.value(), 0)
        y = first.mapTo(h.viewport(), QPoint(0, 0)).y()
        self.assertTrue(0 <= y < h.viewport().height(), y)
        self.assertIs(QApplication.focusWidget(), before)   # the hub never moves the focus
        h.jump_to('nope')                                        # nothing happens
        QApplication.processEvents()
        self.assertIs(h.tile_for(), first)

    def test_view_protocol_noops(self):
        h = self.hub()
        self.assertEqual((h.tiles, h.games, h.shown_games()), ({}, {}, []))
        self.assertIsNone(h.tile_for('b1'))
        self.assertIs(h.tile_for(), h.tiles_in('for_you')[0])
        h.reveal('b1')
        h.mark_selected({'b1'})
        h.set_jobs(None)
        h.set_friends_badge(True)
        h.refresh_friends()
        h.tile('genre:RPG').click()                               # opened last: Back lands there
        self.assertIs(h.tile_for(), h.tile('genre:RPG'))


@unittest.skipIf(nx is None, "PyQt6 not available")
class HubHistory(_Rich):
    def test_hub_tile_page_back_back_to_browse_scroll(self):
        w = self.win
        bar = w.home.verticalScrollBar()
        bar.setValue(bar.maximum() // 3)
        self.pump()
        before = bar.value()
        self.assertGreater(before, 0)
        w.home.head.btn_hub.click()
        self.assertTrue(self.on_hub())
        self.assertEqual(w._place(), (w.LIBRARY, 'cat', 'all'))
        self.assertTrue(w._lib_bar.isHidden())
        self.assertEqual(w._shown_games, [])             # no game tiles: Ctrl+D picks nothing
        w.cathub.tile('genre:RPG').click()
        self.assertTrue(self.on_page('genre:RPG'))
        self.assertTrue(w.go_back())
        self.assertTrue(self.on_hub())
        self.assertIs(w.cathub.tile_for(), w.cathub.tile('genre:RPG'))
        self.assertTrue(w.go_back())
        self.pump(6)
        self.assertFalse(w._category_open())
        self.assertIs(w.views.currentWidget(), w.home)
        self.assertEqual(bar.value(), before)

    def test_all_genres_opens_hub_at_genres(self):
        w = self.win
        w.home.kicker('genres').btn_all.click()
        self.pump(6)
        self.assertTrue(self.on_hub())
        self.assertGreater(w.cathub.verticalScrollBar().value(), 0)
        self.assertEqual(w.cathub._last, w.cathub.tiles_in('genres')[0].key)

    def test_keys_focus_jumped_group_once_unless_covered(self):
        from PyQt6.QtWidgets import QApplication
        from nexus.gui.category import MosaicTile
        w = self.win
        w.activateWindow()
        w.input.mode = 'keyboard'
        moves = []
        orig = MosaicTile.focusInEvent
        def spy(t, ev):                       # a Qt virtual: must return None
            moves.append(t.key)
            orig(t, ev)
        with mock.patch.object(MosaicTile, 'focusInEvent', spy):
            w.home.kicker('genres').btn_all.click()
            self.pump(6)
        first = w.cathub.tiles_in('genres')[0]
        self.assertIs(QApplication.focusWidget(), first)
        self.assertEqual(moves, [first.key])                     # once, straight there
        # Something opens over the hub before the deferred focus: it stays put.
        w.close_category()
        w.home.kicker('genres').btn_all.click()
        w._new_shelf()                                           # the shelf modal
        self.pump(6)
        self.assertTrue(w.modal.isVisible())
        self.assertNotIsInstance(QApplication.focusWidget(), MosaicTile)
        w._escape()
        self.pump()

    def test_hub_is_history_place(self):
        w = self.win
        w._open_hub()
        w.go(w.HOME)
        self.assertTrue(w.go_back())
        self.assertEqual(w.current(), w.LIBRARY)
        self.assertTrue(self.on_hub())
        self.assertTrue(w.go_forward())
        self.assertEqual(w.current(), w.HOME)

    def test_escape_from_hub(self):
        w = self.win
        w._open_hub()
        w.search.clearFocus()
        w._escape()
        self.assertFalse(w._category_open())
        self.assertIs(w.views.currentWidget(), w.home)
        self.assertFalse(w._lib_chips.isHidden())

    def test_theatre_hub_does_nothing(self):
        w = self.win
        w._theatre = True
        try:
            self.assertFalse(w.open_category('all'))
            self.assertFalse(w._category_open())
        finally:
            w._theatre = False

    def test_reveal_a_game_closes_the_hub(self):
        w = self.win
        w._open_hub()
        w._reveal_game('b7')
        self.assertFalse(w._category_open())
        self.assertEqual(w.selected, ['b7'])



@unittest.skipIf(nx is None, "PyQt6 not available")
class Pins(_Rich):
    """Pinned categories as Library tabs on the desktop (w-100)."""

    def tabs(self):
        return self.win.cat_tabs

    def test_strip_without_pins(self):
        """w-116: Browse and All games are fixed tabs, so the strip shows
        with no pin too."""
        w = self.win
        self.assertEqual(w.prefs.category_pins, [])
        self.assertFalse(self.tabs().isHidden())
        self.assertEqual(self.tabs().keys(), ['browse', 'games', 'all'])
        w.open_category('src:steam')
        self.assertEqual(self.tabs().current(), '')            # not pinned: no tab
        self.assertEqual(w.catpage.header.btn_pin.text(), "Pin")
        self.assertFalse(w.catpage.header.btn_pin.isHidden())

    def test_pin_adds_tab_and_survives_restart(self):
        import tempfile
        from pathlib import Path
        w = self.win
        w.open_category('src:steam')
        w.catpage.header.btn_pin.click()
        self.assertEqual(w.prefs.category_pins, ['src:steam'])
        self.assertEqual(w.catpage.header.btn_pin.text(), "Pinned")
        self.assertFalse(self.tabs().isHidden())
        self.assertEqual(self.tabs().keys(), ['browse', 'games', 'src:steam', 'all'])
        self.assertEqual(self.tabs().current(), 'src:steam')
        self.assertEqual(self.tabs().button('browse').text(), "Browse")
        w.close_category()
        self.assertFalse(self.tabs().isHidden())           # Browse keeps the strip
        self.assertEqual(self.tabs().current(), 'browse')
        w.set_view('grid', save=False)
        self.assertEqual(self.tabs().current(), 'games')
        # Saved, and read back by the next start.
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(nx.LibraryPrefs, 'PATH', Path(d) / 'library.json'):
            self._saved[1](w.prefs)                         # the real save
            w2 = nx.MainWindow()
            try:
                self.assertEqual(w2.prefs.category_pins, ['src:steam'])
                w2._facts_idx = facts_index()
                w2._sync_facts = lambda: False
                w2.show()
                w2._on_library({'games': win_games(), 'disks': []}, '')
                w2.go(w2.LIBRARY)
                self.pump()
                self.assertEqual(w2.cat_tabs.keys(), ['browse', 'games', 'src:steam', 'all'])
            finally:
                stop_leftovers(w2.jobs)
                w2.close()
                w2.deleteLater()

    def test_unpin_from_header_removes_tab(self):
        w = self.win
        w.prefs.category_pins = ['fav', 'src:steam']
        w.apply_filter()
        self.assertEqual(self.tabs().keys(), ['browse', 'games', 'fav', 'src:steam', 'all'])
        w.open_category('src:steam')
        self.assertEqual(w.catpage.header.btn_pin.text(), "Pinned")
        w.catpage.header.btn_pin.click()
        self.assertEqual(w.prefs.category_pins, ['fav'])
        self.assertEqual(self.tabs().keys(), ['browse', 'games', 'fav', 'all'])
        self.assertEqual(w.catpage.header.btn_pin.text(), "Pin")
        w.catpage.header.btn_pin.click()                   # and back, last
        self.assertEqual(w.prefs.category_pins, ['fav', 'src:steam'])
        w.prefs.category_pins = [f'genre:{g}' for g in GENRES8[:6]]
        w._refresh_category()
        self.assertFalse(w.catpage.header.btn_pin.isEnabled())   # full

    def test_tab_opens_page_back_returns_browse_place(self):
        from PyQt6.QtWidgets import QApplication
        w = self.win
        w.activateWindow()
        w.prefs.category_pins = ['fav', 'src:steam']
        w.apply_filter()
        self.pump()
        bar = w.home.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum() // 2)
        place = bar.value()
        self.tabs().button('src:steam').click()
        self.assertTrue(self.on_page('src:steam'))
        self.assertEqual(self.tabs().current(), 'src:steam')
        self.assertFalse(self.tabs().isHidden())
        self.tabs().button('fav').click()                   # tab to tab
        self.assertTrue(self.on_page('fav'))
        self.tabs().button('browse').click()
        self.pump(6)
        self.assertFalse(w._category_open())
        self.assertIs(w.views.currentWidget(), w.home)
        self.assertEqual(bar.value(), place)
        self.assertEqual(self.tabs().current(), 'browse')
        w.go_back()                                         # history: the pages
        self.assertTrue(self.on_page('fav'))
        QApplication.processEvents()

    def test_all_games_tab(self):
        """w-116: All games is the second tab, a divider after it, and opens
        the plain grid (sorted by name on a fresh start) with every game."""
        w = self.win
        w.prefs.category_pins = ['fav']
        w.apply_filter()
        tabs = self.tabs()
        self.assertEqual(tabs.keys()[:2], ['browse', 'games'])
        self.assertIsNotNone(tabs.divider)
        lay = tabs.seg.layout()
        self.assertEqual(lay.indexOf(tabs.divider), lay.indexOf(tabs.button('games')) + 1)
        w.open_category('fav')
        tabs.button('games').click()
        self.assertFalse(w._category_open())
        self.assertIs(w.views.currentWidget(), w.library)
        self.assertEqual(tabs.current(), 'games')
        every = {g['slug'] for g in w._games if not g.get('hidden') and not g.get('dup_of')
                 and nx.is_installed(g)}
        self.assertEqual(set(w.library.games), every)          # no category: every game
        tabs.button('browse').click()
        self.assertIs(w.views.currentWidget(), w.home)
        self.assertEqual(tabs.current(), 'browse')
        w.set_view('list', save=False)                     # All games keeps the last of grid/list
        tabs.button('browse').click()
        tabs.button('games').click()
        self.assertIs(w.views.currentWidget(), w.listview)

    def test_letter_jump(self):
        """w-116: on the grid sorted by name, the A-Z strip lights the
        letters that have a game; a letter focuses its first tile."""
        from nexus.gui.letters import letter_of
        w = self.win
        w.activateWindow()
        w.sort.setCurrentIndex(w.sort.findData('name'))
        self.tabs().button('games').click()
        self.pump()
        self.assertFalse(w.letters.isHidden())
        names = [g.get('display_name') or g['slug'] for g in w._shown_games]
        lit = {letter_of(n) for n in names}
        self.assertEqual(set(w.letters.lit()), lit)
        last = sorted(lit)[-1]
        first = next(g['slug'] for g in w._shown_games
                     if letter_of(g.get('display_name') or g['slug']) == last)
        w.letters.buttons[last].click()
        self.pump()
        from PyQt6.QtWidgets import QApplication
        f = QApplication.focusWidget()
        self.assertEqual(getattr(f, 'game', {}).get('slug'), first)
        w.sort.setCurrentIndex(w.sort.findData('most'))       # not by name: no strip
        self.assertTrue(w.letters.isHidden())
        w.sort.setCurrentIndex(w.sort.findData('name'))
        self.tabs().button('browse').click()
        self.assertTrue(w.letters.isHidden())

    def test_all_categories_tab_opens_hub(self):
        w = self.win
        w.prefs.category_pins = ['fav']
        w.apply_filter()
        self.tabs().button('all').click()
        self.assertTrue(self.on_hub())
        self.assertEqual(self.tabs().current(), 'all')
        self.assertFalse(self.tabs().isHidden())
        self.tabs().button('fav').click()
        self.assertTrue(self.on_page('fav'))



class _Theatre(_Rich):
    """Theatre on rich_library(), its Library tab on show."""

    def setUp(self):
        super().setUp()
        w = self.win
        w.activateWindow()
        w.prefs.theatre_library = {'sort': 'name', 'show': 'installed', 'view': 'browse'}
        w.theatre.set_library_state(w.prefs.theatre_library)
        w.set_theatre(True)
        self.th = w.theatre
        self.th.tabs.set_current('library', emit=True)
        self.pump()

    def tearDown(self):
        self.win.set_theatre(False)
        super().tearDown()

    def row_keys(self):
        return [r.key for r in self.th.lib_rows_list()]

    def row(self, key):
        return next(r for r in self.th.lib_rows_list() if r.key == key)

    def key(self, k, w=None):
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtWidgets import QApplication
        target = w or QApplication.focusWidget()
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):    # A on a button: both
            QApplication.sendEvent(target, QKeyEvent(kind, k, Qt.KeyboardModifier.NoModifier))
        self.pump()

    def focus(self):
        from PyQt6.QtWidgets import QApplication
        return QApplication.focusWidget()


@unittest.skipIf(nx is None, "PyQt6 not available")
class TheatreRows(_Theatre):
    """Theatre's Library as rows from categories.browse (w-100)."""

    def test_rows_follow_browse_continue_first_wide(self):
        from nexus import categories
        th = self.th
        self.assertIs(th.lib_stack.currentWidget(), th.lib_rows)
        w = self.win
        from nexus.features import has
        groups = categories.categories(
            [g for g in w._games if not g.get('hidden')], w._smart_facts(), w._prefs_view(),
            has('proton'))
        want = [c.key for g in categories.browse(groups, w.prefs.smart_hidden) for c in g.cats]
        self.assertEqual(self.row_keys(), want)
        self.assertEqual(self.row_keys()[0], 'smart:continue')
        first = self.row('smart:continue')
        self.assertEqual((first.kind, first.tile_w), ('wide', th.WIDE))
        for t in first.tiles:
            self.assertTrue(t.theatre)
        steam = self.row('src:steam')
        self.assertEqual(len(steam.tiles), categories.ROW_CAP)
        self.assertEqual(steam.count_label.text(), '40')
        self.assertEqual(self.row_keys()[-2:], ['src:nexus', 'src:steam'])   # stores last
        self.assertEqual(th.lib_tabs.keys(), ['browse', 'games'])
        self.assertEqual(th.lib_tabs.current(), 'browse')

    def test_rows_reused_by_key_keep_focus(self):
        th = self.th
        row = self.row('src:steam')
        tile = row.tiles[3]
        tile.setFocus()
        self.pump()
        slug = tile.game['slug']
        w = self.win
        w._on_library({'games': [dict(g) for g in rich_library()[0]], 'disks': []}, '')
        self.pump()
        self.assertIs(self.row('src:steam'), row)
        self.assertIs(row.tiles[3], tile)
        self.assertIs(self.focus(), tile)
        self.assertEqual(tile.game['slug'], slug)

    def test_no_groups_shows_old_grid(self):
        th = self.th
        th.set_games([])
        self.pump()
        self.assertEqual(self.row_keys(), [])
        self.assertIs(th.lib_stack.currentWidget(), th.lib)

    def test_all_games_keeps_sort_and_show(self):
        th = self.th
        saved = []
        th.library_changed.connect(saved.append)
        th.lib_tabs.button('games').click()
        self.pump()
        self.assertIs(th.lib_stack.currentWidget(), th.lib)
        self.assertEqual(th.lib_tabs.current(), 'games')
        self.assertEqual(saved[-1], {'sort': 'name', 'show': 'installed', 'view': 'all'})
        slugs = [th.lib_flow.itemAt(i).widget().game['slug'] for i in range(th.lib_flow.count())]
        self.assertEqual(slugs[:2], ['b0', 'b1'])              # A to Z, as saved
        self.assertEqual(len(slugs), 50)
        th._pick_library(self.win.theatre_sheet, 'sort', 'most', 1)
        self.assertEqual(saved[-1]['view'], 'all')
        th.lib_tabs.button('browse').click()
        self.pump()
        self.assertIs(th.lib_stack.currentWidget(), th.lib_rows)
        self.assertEqual(saved[-1], {'sort': 'most', 'show': 'installed', 'view': 'browse'})

    def test_windows_set_no_linux_no_heroic(self):
        from nexus import features
        gs, idx = rich_library()
        gs.append({'slug': 'h0', 'display_name': 'H0', 'source': 'heroic', 'installed': True,
                   'launcher': '/x', 'playtime_minutes': 5, 'last_played': None})
        saved = features.FEATURES
        try:
            features.FEATURES = features.ALL
            self.load([dict(g) for g in gs], idx)
            self.pump()
            every = lambda: [c.key for g in self.th._cat_groups for c in g.cats]  # noqa: E731
            self.assertIn('src:heroic', self.row_keys())
            self.assertIn('smart:linux', every())               # past For you's cap here
            features.FEATURES = features.WINDOWS
            self.win._home_sig = None                     # the same games: rebuild anyway
            self.load([dict(g) for g in gs], idx)
            self.pump()
            self.assertNotIn('smart:linux', every())
            self.assertNotIn('src:heroic', every())
            self.assertNotIn('src:heroic', self.row_keys())
        finally:
            features.FEATURES = saved

    def test_pins_are_tabs_in_theatre(self):
        th = self.th
        w = self.win
        w.prefs.category_pins = ['fav', 'genre:Gone']
        th.pins_changed()
        self.assertEqual(th.lib_tabs.keys(), ['browse', 'fav', 'games'])
        th.lib_tabs.button('fav').click()
        self.pump()
        self.assertIs(th.pages.currentWidget(), th.catpage)
        self.assertEqual(th.catpage.cat.key, 'fav')
        self.assertEqual(th.catpage.header.btn_pin.text(), "Pinned")
        th.catpage.header.btn_pin.click()                       # unpin, here too
        self.assertEqual(w.prefs.category_pins, ['genre:Gone'])
        th.back()
        self.pump()
        self.assertIs(th.pages.currentWidget(), th.lib_page)
        self.assertEqual(th.lib_tabs.keys(), ['browse', 'games'])   # an unknown genre: none

    def test_empty_pinned_category_is_a_tab_on_both(self):
        """A pinned category with no games keeps its tab, on the desktop and
        in Theatre alike; its page says it is empty."""
        w, th = self.win, self.th
        w.prefs.collections = {**w.prefs.collections, 'Empty': []}
        w._mirror_collections()
        w.prefs.category_pins = ['col:Empty', 'fav']
        th.set_games([g for g in w._games if not g.get('hidden')])
        self.pump()
        self.assertEqual(th.lib_tabs.keys(), ['browse', 'col:Empty', 'fav', 'games'])
        th.lib_tabs.button('col:Empty').click()
        self.pump()
        self.assertEqual(th.catpage.cat.key, 'col:Empty')
        self.assertEqual(th.catpage.shown_games(), [])
        th.back()
        w.set_theatre(False)
        w.go(w.LIBRARY)
        w.apply_filter()
        self.assertEqual(w.cat_tabs.keys(), ['browse', 'games', 'col:Empty', 'fav', 'all'])
        w.cat_tabs.button('col:Empty').click()
        self.assertTrue(self.on_page('col:Empty'))
        self.assertEqual(w.catpage.shown_games(), [])
        w.set_theatre(True)                                     # tearDown leaves it


@unittest.skipIf(nx is None, "PyQt6 not available")
class TheatreAB(_Theatre):
    """A, B, X, Y on Theatre's rows and category page (w-100)."""

    def test_x_on_row_opens_page(self):
        from PyQt6.QtCore import Qt
        th = self.th
        tile = self.row('src:steam').tiles[2]
        tile.setFocus()
        self.pump()
        self.key(Qt.Key.Key_Menu)                                # X
        self.assertIs(th.pages.currentWidget(), th.catpage)
        self.assertEqual(th.catpage.cat.key, 'src:steam')
        self.assertFalse(self.win.theatre_sheet.isVisible())     # not the options sheet
        self.pump()
        f = self.focus()
        self.assertIsInstance(f, nx.GameTile)
        self.assertTrue(th.catpage.isAncestorOf(f))
        self.assertEqual(f.tw, th.TILE)

    def test_a_on_see_all_opens_page(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QPushButton
        th = self.th
        b = next(b for b in self.row('fav').findChildren(QPushButton) if b.text() == "See all")
        b.setFocus()
        self.pump()
        self.key(Qt.Key.Key_Space)                               # A on a button
        self.assertIs(th.pages.currentWidget(), th.catpage)
        self.assertEqual(th.catpage.cat.key, 'fav')

    def test_b_returns_same_row_tile_scroll(self):
        from PyQt6.QtCore import Qt
        th = self.th
        bar = th.lib_rows.verticalScrollBar()
        row = self.row('src:steam')
        tile = row.tiles[6]
        from nexus.gui.input import focus_and_reveal
        focus_and_reveal(tile)
        self.pump(6)
        place, offset = bar.value(), row.bar.value()
        self.assertGreater(place, 0)
        self.key(Qt.Key.Key_Menu)
        self.assertIs(th.pages.currentWidget(), th.catpage)
        self.win._escape()                                       # B
        self.pump(6)
        self.assertIs(th.pages.currentWidget(), th.lib_page)
        self.assertIs(self.focus(), tile)
        self.assertEqual(bar.value(), place)
        self.assertEqual(row.bar.value(), offset)

    def test_focus_moves_once_into_page_and_back(self):
        """w-60: focus goes straight to the target before the page switch,
        never through nowhere or a widget on the way."""
        from PyQt6.QtWidgets import QApplication
        th = self.th
        tile = self.row('src:steam').tiles[1]
        tile.setFocus()
        self.pump()
        seen = []
        app = QApplication.instance()
        app.focusChanged.connect(self._spy)
        self._seen = seen
        try:
            th.open_category('src:steam')
            self.assertEqual(len(seen), 1, seen)
            self.assertTrue(th.catpage.isAncestorOf(seen[0]))
            del seen[:]
            th.close_category()
            self.assertEqual(seen, [tile])
        finally:
            app.focusChanged.disconnect(self._spy)

    def _spy(self, _old, new):
        self._seen.append(new)

    def test_y_picks_inside_category(self):
        th = self.th
        w = self.win
        th.open_category('fav')
        self.pump()
        picks = []
        th.pick_in.connect(picks.append)                         # what the window picks from
        w._pad_action('search')                                  # Y
        th.pick_in.disconnect(picks.append)
        self.assertEqual(sorted(g['slug'] for g in picks[0]), ['b1', 'n2'])
        self.assertTrue(th.details.isVisible())                  # surprise opened a game
        th.back()
        self.pump()
        self.assertFalse(th.search.isVisible())
        w.surprise(th.catpage.shown_games())
        self.assertTrue(th.details.isVisible())
        self.assertIn(th.details.game['slug'], ('b1', 'n2'))

    def test_lb_rb_drops_page(self):
        th = self.th
        w = self.win
        th.open_category('src:steam')
        self.assertIs(th.pages.currentWidget(), th.catpage)
        w._pad_action('page-next')
        self.pump()
        self.assertIsNot(th.pages.currentWidget(), th.catpage)
        self.assertIsNone(th.category_key())
        th.tabs.set_current('library', emit=True)
        self.pump()
        self.assertIs(th.pages.currentWidget(), th.lib_page)
        th.open_category('src:steam')
        th.go_home()
        self.assertIsNone(th.category_key())
        self.assertFalse(th.back())                             # Home: B does nothing more

    def test_view_opens_category_sort(self):
        from PyQt6.QtWidgets import QPushButton
        th = self.th
        w = self.win
        th.open_category('src:steam')
        self.pump()
        w._pad_action('view')
        sheet = w.theatre_sheet
        self.assertTrue(sheet.isVisible())
        rows = [b.text() for b in sheet.findChildren(QPushButton)
                if b.objectName() == 'SheetRow' and b.isVisibleTo(sheet)]
        self.assertEqual(rows, ["Recently played", "Name", "Most played", "Recently added",
                                "Size on disk"])
        next(b for b in sheet.findChildren(QPushButton) if b.text() == "Name").click()
        self.pump()
        self.assertTrue(sheet.isVisible())                       # stays up
        self.assertEqual(w.prefs.category_sort.get('src:steam'), 'name')
        self.assertEqual(th.catpage.shown_games()[0]['slug'], 'b0')
        self.assertEqual(w.prefs.theatre_library['sort'], 'name')   # Library's own untouched
        w._pad_action('view')                                    # View closes it
        self.assertFalse(sheet.isVisible())

    def test_hints_on_rows_and_page(self):
        th = self.th
        w = self.win
        self.row('fav').tiles[0].setFocus()
        self.pump()
        w._update_hints()
        hints = dict(w.hints._shown[1])
        self.assertEqual(hints.get('x'), 'See all')
        th.open_category('fav')
        self.pump()
        th.catpage.tile_for().setFocus()
        self.pump()
        w._update_hints()
        hints = dict(w.hints._shown[1])
        self.assertEqual((hints.get('x'), hints.get('y'), hints.get('View'), hints.get('b')),
                         ('Options', 'Pick for me', 'Sort', 'Back'))


@unittest.skipIf(nx is None, "PyQt6 not available")
class Header(_Rich):
    """w-117: the one-row Library header, the Filter popover and ⋮."""

    def test_one_row_and_soft_tags(self):
        w = self.win
        bar = w._lib_bar
        for b in (w.btn_filter, w.sort, w.view_seg, w.btn_lib_menu, w.count_label,
                  w.filter_tag):
            self.assertTrue(bar.isAncestorOf(b))
        for b in (w.filter, w.own_seg, w.chips['steam']):
            self.assertTrue(w.filter_pop.card.isAncestorOf(b))  # in the popover
        self.assertTrue(w.btn_surprise.isHidden())
        self.assertTrue(w.btn_refresh.isHidden())
        self.assertTrue(w.home.head.bar.isHidden())              # no jump chips
        self.assertRegex(w.count_label.text(), r"^\d+ games?$")
        self.assertEqual(w.filter_tag.text(), "Installed · every store")
        self.assertEqual(w.btn_filter.text(), "Filter")

    def test_filters_combine_and_say_so(self):
        w = self.win
        before = len(w._shown_games)
        w._set_chip('steam')
        steam = len(w._shown_games)
        w.filter.setCurrentIndex(w.filter.findData('hidden'))
        self.assertEqual(w.filter_tag.text(), "Installed · Steam · Hidden games")
        self.assertEqual(w.btn_filter.text(), "Steam · Hidden games")
        self.assertTrue(w.btn_filter.property('active'))
        self.assertTrue(all(g.get('source') == 'steam' and g.get('hidden')
                            for g in w._shown_games))
        w._reset_filters()
        self.assertEqual(len(w._shown_games), before)
        self.assertLessEqual(steam, before)
        self.assertEqual(w.btn_filter.text(), "Filter")
        self.assertEqual((w._chip, w.prefs.library_state['status']), ('all', 'all'))

    def test_restored_from_library_state(self):
        w = self.win
        w.prefs.library_state = {'sort': 'most', 'status': 'all', 'chip': 'steam'}
        w._restore_library_state()
        w.apply_filter()
        self.assertEqual(w.btn_filter.text(), "Steam")
        self.assertEqual(w.sort.currentData(), 'most')

    def test_popover_keys_and_pad(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication
        w = self.win
        w.activateWindow()
        w.go(w.LIBRARY)
        self.assertTrue(w.btn_filter.focusPolicy() & Qt.FocusPolicy.TabFocus)
        w.btn_filter.setFocus()
        w._pad_action('view')                                   # View opens it
        self.pump()
        self.assertTrue(w._filter_open())
        self.assertIs(w.nav_scope(), w.filter_pop.card)
        self.assertTrue(w.filter_pop.card.isAncestorOf(QApplication.focusWidget()))
        w._escape()                                             # B / Escape close it
        self.assertFalse(w._filter_open())
        self.assertIs(QApplication.focusWidget(), w.btn_filter)
        w.btn_filter.click()
        self.assertTrue(w._filter_open())
        w.open_category('src:steam')                            # a page closes it
        self.assertFalse(w._filter_open())

    def test_new_shelf_on_the_yours_kicker(self):
        w = self.win
        k = w.home.kicker('yours')
        self.assertIsNotNone(k.btn_new)
        self.assertFalse(k.btn_new.isHidden())
        called = []
        w.home.new_shelf.disconnect()
        w.home.new_shelf.connect(lambda: called.append(1))
        k.btn_new.click()
        self.assertEqual(called, [1])


@unittest.skipIf(nx is None, "PyQt6 not available")
class Letters(unittest.TestCase):
    """w-116: the A-Z strip's letters (nexus/gui/letters.py)."""

    def test_letter_of(self):
        from nexus.gui.letters import first_by_letter, letter_of
        self.assertEqual([letter_of(n) for n in ("alan wake", "Ōkami", "7 Days to Die",
                                                 "", "  zeno", "¡Hola!")],
                         ['A', 'O', '#', '#', 'Z', '#'])
        self.assertEqual(first_by_letter(["8-bit", "Alpha", "anno", "Beta"]),
                         {'#': 0, 'A': 1, 'B': 3})


if __name__ == '__main__':
    unittest.main()
