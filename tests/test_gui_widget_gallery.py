"""The Customise gallery (w-158): live previews of the real widgets filled
from sample data, size chips that draw their shape, the gallery's groups,
search, filter and Recommended row. Offscreen; previews never ask the CLI
or the network."""

import unittest
from datetime import date
from unittest import mock

try:
    from test_gui_layout import nx, drop, flush_deletes, check_no_leaked_windows
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, drop, flush_deletes, check_no_leaked_windows
from tests.patching import patch_gui

if nx is not None:
    from PyQt6.QtCore import QEvent, QSize, Qt
    from PyQt6.QtWidgets import QApplication, QWidget
    from nexus.gui import widget_preview as WP
    from nexus.gui.widget_spec import SPECS

BOX = QSize(130, 84) if nx is not None else None


def _blank(pm):
    img = pm.toImage().scaled(16, 16)
    first = img.pixel(0, 0)
    return all(img.pixel(x, y) == first for x in range(16) for y in range(16))


@unittest.skipIf(nx is None, "PyQt6 not available")
class Previews(unittest.TestCase):
    def setUp(self):
        self.host = QWidget()
        self.addCleanup(drop, self.host)
        p = patch_gui('cli_json')
        self.cli = p.start()
        self.addCleanup(p.stop)
        q = patch_gui('CliQuery')
        self.query = q.start()
        self.addCleanup(q.stop)

    def test_every_kind_renders_every_size_without_cli(self):
        from PyQt6.QtNetwork import QNetworkAccessManager
        hub = WP.PreviewHub(WP.PreviewCache(self.host).bench(), date.today())
        bench = hub.homeview.viewport()
        with mock.patch.object(QNetworkAccessManager, 'get') as get:
            for s in SPECS:
                for size in s.sizes:
                    pm, real = WP.render(s.kind, size, BOX, bench, hub)
                    self.assertTrue(real, (s.kind, size))
                    self.assertFalse(pm.isNull(), (s.kind, size))
                    self.assertFalse(_blank(pm), (s.kind, size))
            get.assert_not_called()
        self.cli.assert_not_called()
        self.query.assert_not_called()

    def test_preview_cards_are_deleted_and_never_windows(self):
        from nexus.gui.dashboard import DashWidget
        seen = []

        class Spy(QWidget):
            def eventFilter(self, obj, ev):
                if ev.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
                    seen.append(type(obj).__name__)
                return False
        spy = Spy()
        QApplication.instance().installEventFilter(spy)
        self.addCleanup(QApplication.instance().removeEventFilter, spy)
        cache = WP.PreviewCache(self.host)
        cache.queue([(s.kind, s.default, BOX) for s in SPECS])
        cache.flush()
        self.assertEqual(cache.rendered, len(SPECS))
        bench = cache.bench()
        self.assertTrue(bench.isHidden())
        self.assertIs(bench.parentWidget(), self.host)
        flush_deletes()
        self.assertEqual([c for c in bench.findChildren(DashWidget)], [])
        self.assertEqual(seen, [])

    def test_cards_have_the_bench_as_parent_from_the_start(self):
        from nexus.gui.dashboard import DashWidget
        parents = []
        orig = DashWidget.__init__

        def init(w, *a, **k):
            orig(w, *a, **k)
            parents.append(w.parentWidget())
        cache = WP.PreviewCache(self.host)
        with mock.patch.object(DashWidget, '__init__', init):
            cache.queue([('week', 'small', BOX), ('heatmap', 'full', BOX)])
            cache.flush()
        self.assertEqual(parents, [cache.bench(), cache.bench()])

    def test_box_and_scale_match_the_board(self):
        from nexus.gui.widget_gallery import preview_box
        small, wide = preview_box('small'), preview_box('full')
        self.assertEqual(small.height(), nx.scaled(92))
        self.assertEqual(wide.height(), nx.scaled(84))
        self.assertAlmostEqual(small.width() / nx.scaled(136), 1.0, delta=0.08)
        self.assertAlmostEqual(wide.width() / nx.scaled(294), 1.0, delta=0.08)
        for box in (small, wide):                      # laid out at a width that scales by ~0.39
            self.assertAlmostEqual(box.width() / WP.ref_width(box), 0.39, delta=0.01)

    def test_cache_hit_until_theme_or_scale_changes(self):
        cache = WP.PreviewCache(self.host)
        cache.queue([('week', 'small', BOX)])
        cache.flush()
        k = cache.key('week', 'small', BOX)
        self.assertIsNotNone(cache.pixmap(k))
        cache.queue([('week', 'small', BOX)])
        self.assertEqual(cache.pending(), 0)                        # a hit: nothing to do
        self.assertEqual(cache.rendered, 1)
        with mock.patch.dict(nx.theme.P, {'surface': '#123456'}):
            self.assertNotEqual(cache.key('week', 'small', BOX), k)   # another theme
        with mock.patch.dict(nx.theme.LOOK, {'scale': 1.25}):
            self.assertNotEqual(cache.key('week', 'small', BOX), k)   # another Size
        self.assertNotEqual(cache.key('week', 'wide', BOX), k)
        cache.invalidate()
        self.assertIsNone(cache.pixmap(k))

    def test_render_error_falls_back_to_glyph_tile(self):
        from nexus.gui.dashboard import WeekWidget
        hub = WP.PreviewHub(WP.PreviewCache(self.host).bench(), date.today())
        with mock.patch.object(WeekWidget, 'fill', side_effect=RuntimeError("boom")), \
                mock.patch('sys.stderr') as err:
            pm, real = WP.render('week', 'small', BOX, hub.homeview.viewport(), hub)
            WP.render('week', 'small', BOX, hub.homeview.viewport(), hub)
        self.assertFalse(real)
        from nexus.gui import common, dashboard, friends
        from nexus.gui.art import ART
        self.assertIs(dashboard.FRIENDS, friends.FRIENDS)              # the sample world is gone
        self.assertIs(dashboard.COLLECTION_NAMES, common.COLLECTION_NAMES)
        self.assertNotIn('fetch', vars(ART))
        self.assertFalse(pm.isNull())
        self.assertFalse(_blank(pm))
        self.assertLessEqual(err.write.call_count, 1)               # logged once


@unittest.skipIf(nx is None, "PyQt6 not available")
class Shapes(unittest.TestCase):
    def test_cells_per_size(self):
        from nexus.gui.widget_gallery import shape_cells
        on = lambda z: [(r, c) for r, c, f in shape_cells(z) if f]   # noqa: E731
        self.assertEqual(on('small'), [(0, 0)])
        self.assertEqual(on('wide'), [(0, 0), (0, 1)])
        self.assertEqual(on('full'), [(0, 0), (0, 1), (0, 2)])
        self.assertEqual(on('cinematic'), on('full'))
        self.assertEqual(len(shape_cells('small')), 3)
        self.assertEqual(len(shape_cells('tall')), 6)                 # three by two
        self.assertEqual(on('tall'), [(0, 0), (1, 0)])

    def test_chip_draws_its_size(self):
        from nexus.gui.widget_gallery import ShapeGlyph, SizeChip
        host = QWidget()
        self.addCleanup(drop, host)
        chip = SizeChip('tall', host)
        self.assertEqual(chip.glyph.size_, 'tall')
        self.assertEqual(chip.text_label.text(), "Tall")
        self.assertGreater(chip.glyph.height(), ShapeGlyph('small', host).height())
        self.assertEqual(SizeChip('full', host).text_label.text(), "Full")
        pm = chip.glyph.grab()
        self.assertFalse(_blank(pm))


def _items(hidden=()):
    from nexus.gui.dashboard import dash_kind_defaults
    out = dash_kind_defaults()
    for it in out:
        it['hidden'] = it['id'] in hidden
    return out


@unittest.skipIf(nx is None, "PyQt6 not available")
class Gallery(unittest.TestCase):
    def setUp(self):
        from nexus.gui.widget_gallery import WidgetGallery
        self.host = QWidget()
        self.addCleanup(drop, self.host)
        self.g = WidgetGallery(self.host)
        self.addCleanup(self.g.stop)

    def counts(self):
        return {k: c.text() for k, (_b, c, _ks) in self.g._group_of.items()}

    def test_groups_counts_and_ticks(self):
        g = self.g
        g.sync(_items(hidden={'stage', 'save', 'heatmap', 'backlog'}))
        self.assertEqual(self.counts()['stage'], "1 of 3 on Home")      # For you now
        self.assertEqual(self.counts()['stats'], "3 of 5 on Home")
        self.assertEqual(g.filter.item('all').text(), "All 20")
        self.assertEqual(g.filter.item('on').text(), "On Home 16")
        self.assertEqual(g.filter.item('off').text(), "Not added 4")
        self.assertTrue(g.tile('week').tick.isVisibleTo(g.tile('week')))
        self.assertFalse(g.tile('heatmap').tick.isVisibleTo(g.tile('heatmap')))
        # on Home: no chips; not added with several sizes: a chip per size
        self.assertFalse(g.tile('week').chips_host.isVisibleTo(g.tile('week')))
        self.assertEqual(list(g.tile('heatmap').chip_btns), ['wide', 'full'])
        self.assertTrue(g.tile('heatmap').chips_host.isVisibleTo(g.tile('heatmap')))
        self.assertEqual(g.tile('save').chip_btns, {})                  # one size: no chips
        # narrow and wide tiles: constant widths from the dock
        self.assertEqual(g.tile('save').width() * 2 + nx.theme.SP['2'], g.tile('heatmap').width())

    def test_search_and_filter_hide_tiles_and_groups(self):
        g = self.g
        g.sync(_items(hidden={'heatmap', 'backlog'}))
        vis = lambda: {t.kind for t in g.tiles.values() if t.isVisibleTo(g)}   # noqa: E731
        def boxes():
            return {k for k, (b, _c, _ks) in g._group_of.items() if b.isVisibleTo(g)}
        g.set_filter('off')
        self.assertEqual(vis(), {'heatmap', 'backlog'})
        self.assertEqual(boxes(), {'stats'})
        g.set_filter('all')
        g.search.setText("year")
        self.assertEqual(vis(), {'heatmap'})                            # "Your year"
        g.search.setText("store")                                       # a group's name
        self.assertEqual(vis(), {'deals', 'upcoming', 'news'})
        g.search.setText("zzz")
        self.assertEqual(vis(), set())
        self.assertTrue(g.empty.isVisibleTo(g))
        g.search.setText("")
        self.assertEqual(len(vis()), 20)

    def test_merged_note_shown(self):
        g = self.g
        g.sync(_items())
        t = g.tile('pick')
        self.assertEqual(t.note.text(), "Now has Today's pick (was Game of the day)")
        self.assertTrue(t.note.isVisibleTo(t))
        self.assertFalse(g.tile('week').note.isVisibleTo(g.tile('week')))
        g.search.setText("game of the day")                             # the note is searched
        self.assertEqual({t.kind for t in g.tiles.values() if t.isVisibleTo(g)}, {'pick'})

    def test_tiles_survive_refresh(self):
        g = self.g
        g.sync(_items(hidden={'heatmap'}))
        before = {k: id(t) for k, t in g.tiles.items()}
        g.sync(_items(hidden={'week'}))
        g.open()
        self.assertEqual({k: id(t) for k, t in g.tiles.items()}, before)
        self.assertTrue(g.tile('heatmap').on_home)
        self.assertFalse(g.tile('week').on_home)

    def test_changed_key_after_open_is_queued_and_painted(self):
        g = self.g
        g.sync(_items(hidden={'week', 'heatmap'}))
        with patch_gui('cli_json'):
            g.open()
            g.cache.flush()
            g.sync(_items(hidden={'heatmap'}) and [dict(i, size='wide') if i['id'] == 'week'
                                                     else i for i in _items(hidden={'heatmap'})])
            t = g.tile('week')
            self.assertEqual(t.size_, 'wide')
            g.cache.flush()
            self.assertIsNotNone(g.cache.pixmap(t.preview.key))       # not a grey placeholder
            g.set_reasons({'heatmap': "Your best streak: 9 days"})    # the payload landed late
            g.cache.flush()
            r = g.rec_tiles['heatmap']
            self.assertIsNotNone(g.cache.pixmap(r.preview.key))

    def test_titles_elide_to_the_tile(self):
        g = self.g
        g.sync(_items(hidden={'friends'}))
        t = g.tile('friends')
        t.set_state(False, 'small')
        self.assertTrue(t.title.text().endswith("\u2026"), t.title.text())
        self.assertLessEqual(t.title.fontMetrics().horizontalAdvance(t.title.text()),
                             t.width() - t.shape.width())
        self.assertIn("Friends playing now", t.face.toolTip() + t.title.toolTip())

    def test_tile_state_restyles_the_face(self):
        g = self.g
        g.sync(_items())
        t = g.tile('week')
        t.face.ensurePolished()
        before = t.face.grab().toImage()
        t.set_selected(True)
        self.assertNotEqual(t.face.grab().toImage(), before)          # the descendant rule applied

    def test_drag_survives_the_tile_going_away(self):
        from PyQt6.QtGui import QDrag
        g = self.g
        g.sync(_items(hidden={'heatmap'}))
        t = g.tile('heatmap')

        def exec_(*_a, **_k):
            t.deleteLater()
            flush_deletes()
            return Qt.DropAction.IgnoreAction
        with mock.patch.object(QDrag, 'exec', exec_):
            t.start_drag()                                            # no RuntimeError

    def test_tick_radius_is_not_a_literal(self):
        import re
        src = (nx._REPO / 'nexus' / 'gui' / 'widget_gallery.py').read_text()
        self.assertEqual(re.findall(r'border-radius:\s*\{(?!pill)', src), [])

    def test_stop_stops_the_restyle(self):
        g = self.g
        g._restyle.start()
        g.stop()
        self.assertFalse(g._restyle.isActive())

    def test_previews_queued_on_open_and_painted_from_keys(self):
        g = self.g
        g.sync(_items())
        with patch_gui('cli_json'):
            g.open()
            self.assertGreater(g.cache.pending(), 0)
            g.cache.flush()
        t = g.tile('week')
        self.assertIsNotNone(g.cache.pixmap(t.preview.key))
        self.assertFalse(hasattr(t, 'pixmap'))                          # keys, not pixmaps
        n = g.cache.rendered
        g.open()                                                        # later opens: cached
        self.assertEqual(g.cache.pending(), 0)
        self.assertEqual(g.cache.rendered, n)

    def test_restyle_drops_previews_and_queues_again(self):
        g = self.g
        g.sync(_items())
        with patch_gui('cli_json'):
            g.open()
            g.cache.flush()
            old = g.tile('week').preview.key
            with mock.patch.dict(nx.theme.P, {'surface': '#102030'}):
                QApplication.sendEvent(g, QEvent(QEvent.Type.StyleChange))
                flush_deletes()
                self.assertIsNone(g.cache.pixmap(old))
                self.assertNotEqual(g.tile('week').preview.key, old)
                g.cache.stop()


@unittest.skipIf(nx is None, "PyQt6 not available")
class Recommend(unittest.TestCase):
    def test_order_and_limit(self):
        from nexus.gui.widget_gallery import recommended
        cands = {'news': "2 stories", 'friends': "3 playing", 'backlog': "", 'zzz': "x"}
        self.assertEqual(recommended(cands), ['friends', 'news'])
        self.assertEqual(recommended(cands, limit=3), ['friends', 'news', 'zzz'])
        self.assertEqual(recommended({}), [])

    def test_only_hidden_with_a_reason(self):
        from nexus.gui.widget_gallery import WidgetGallery
        host = QWidget()
        self.addCleanup(drop, host)
        g = WidgetGallery(host)
        g.sync(_items(hidden={'week', 'news', 'heatmap'}))
        g.set_reasons({'week': "You played 9.4 h this week", 'friends': "3 playing now",
                       'heatmap': '', 'news': "2 stories about your games"})
        self.assertEqual(g.recommended_kinds(), ['week', 'news'])      # friends is on Home
        self.assertEqual(g.rec_tiles['week'].note.text(), "You played 9.4 h this week")
        self.assertTrue(g.rec_box.isVisibleTo(g))
        g.set_filter('on')
        self.assertFalse(g.rec_box.isVisibleTo(g))                      # only under All
        g.set_filter('all')
        g.search.setText("week")
        self.assertFalse(g.rec_box.isVisibleTo(g))                      # ... with no search
        g.search.setText("")
        g.sync(_items(hidden={'news'}))                                 # week was added
        self.assertEqual(list(g.rec_tiles), ['news'])

    def test_widgets_say_why_from_data_in_hand(self):
        from nexus.gui import widget_preview as WP
        from nexus.gui.dashboard import DASH_WIDGETS
        host = QWidget()
        self.addCleanup(drop, host)
        hub = WP.PreviewHub(WP.PreviewCache(host).bench(), date.today())
        with WP.sample_world(hub):
            why = {}
            for k, cls in DASH_WIDGETS.items():
                why[k] = cls(host).why(hub)
        self.assertEqual(why['updates'], "2 games with an update ready")
        self.assertIn("playing now", why['friends'])
        self.assertIn("stories about your games", why['news'])
        self.assertTrue(why['onthisday'])
        self.assertEqual(why['stage'], '')


def tearDownModule():
    if nx is not None:
        check_no_leaked_windows()
