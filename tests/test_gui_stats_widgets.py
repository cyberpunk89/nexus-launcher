"""Home's Stats widgets (w-141): Backlog health, Where your space goes and
Library facts. They fill from a fake insights payload, leave the layout when
they have nothing, and keep a pad's way in (focus, Enter, Escape). The
payload itself is tested in test_insights.py."""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock

from PyQt6.QtCore import QEvent, QPoint
from PyQt6.QtGui import QFocusEvent

try:
    from test_gui_layout import nx
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx
from tests.patching import set_gui

if nx is not None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget
    from nexus.gui import dashboard as D
    from nexus.gui import stats_widgets as S
    from nexus.gui import theme

TODAY = date(2026, 11, 16)
GB = 10 ** 9


def games():
    return [{'slug': s, 'display_name': n, 'launcher': '/x'} for s, n in
            (('disco', 'Disco Elysium'), ('tunic', 'Tunic'), ('hades', 'Hades'), ('gris', 'Gris'))] + [
        {'slug': 'hid', 'display_name': 'Hidden One', 'launcher': '/h', 'hidden': True}]


def payload(**kw):
    p = {'v': 1, 'today': TODAY.isoformat(),
         'backlog': {'done': 14, 'playing': 6, 'started': 17, 'never': 13, 'main_hours_left': 310.0,
                     'pace_hours_week': 22.0, 'idle_year_bytes': 212 * GB},
         'space': [{'slug': 'disco', 'bytes': 60 * GB, 'idle_days': 5},
                   {'slug': 'tunic', 'bytes': 40 * GB, 'idle_days': 100},
                   {'slug': 'hades', 'bytes': 30 * GB, 'idle_days': 500},
                   {'slug': 'gris', 'bytes': 20 * GB, 'idle_days': None},
                   {'slug': 'hid', 'bytes': 90 * GB, 'idle_days': 1}],
         'space_more': {'n': 7, 'bytes': 15 * GB},
         'facts': [{'kind': 'library', 'slug': None, 'name': None, 'n': 44, 'hours': 812.4},
                   {'kind': 'top_genre', 'slug': None, 'name': 'RPG', 'n': 12},
                   {'kind': 'shortest_unplayed', 'slug': 'gris', 'name': 'Gris', 'n': 4.0},
                   {'kind': 'biggest', 'slug': 'hid', 'name': 'Hidden One', 'n': 90 * GB}]}
    p.update(kw)
    return p


class FakeHub:
    def __init__(self, data):
        self._games = games()
        self.dashboard = SimpleNamespace(feed=SimpleNamespace(data=data))
        self.opened, self.pages = [], []
        self._tile_clicked = lambda g, mods: self.opened.append(('page', g['slug']))
        self.go = self.pages.append


def texts(w):
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)   # a cleared label is gone
    return [lab.text() for lab in w.findChildren(QLabel)]


@unittest.skipIf(nx is None, "PyQt6 not available")
class Pure(unittest.TestCase):
    def test_squarify_fills_the_rect_with_proportional_areas(self):
        vals = [60, 40, 30, 20, 15, 5]
        rect = (0.0, 0.0, 400.0, 200.0)
        out = S.squarify(vals, rect)
        self.assertEqual(len(out), 6)
        total = sum(vals)
        for v, (x, y, w, h) in zip(vals, out):
            self.assertAlmostEqual(w * h, v / total * 400 * 200, places=4)
            self.assertGreaterEqual(x, -1e-9)
            self.assertLessEqual(x + w, 400 + 1e-9)
            self.assertLessEqual(y + h, 200 + 1e-9)
        self.assertAlmostEqual(sum(w * h for _x, _y, w, h in out), 400 * 200, places=4)
        for i, a in enumerate(out):                      # no overlaps
            for b in out[i + 1:]:
                ox = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
                oy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
                self.assertFalse(ox > 1e-6 and oy > 1e-6, (a, b))

    def test_squarify_keeps_blocks_near_square(self):
        out = S.squarify([10] * 12, (0.0, 0.0, 400.0, 200.0))
        for _x, _y, w, h in out:
            self.assertLess(max(w / h, h / w), 2.5)

    def test_squarify_edge_cases(self):
        self.assertEqual(S.squarify([], (0, 0, 10, 10)), [])
        self.assertEqual(S.squarify([0, 0], (0, 0, 10, 10)), [(0, 0, 0.0, 0.0)] * 2)
        one = S.squarify([5], (0.0, 0.0, 80.0, 40.0))
        self.assertEqual(one, [(0.0, 0.0, 80.0, 40.0)])
        zero = S.squarify([5, 0], (0.0, 0.0, 80.0, 40.0))
        self.assertEqual(zero[1][2] * zero[1][3], 0)

    def test_idle_buckets(self):
        self.assertEqual([S.idle_bucket(d) for d in (0, 30, 31, 365, 366, None)],
                         ['month', 'month', 'year', 'year', 'over', 'never'])
        self.assertEqual(S.idle_words(400), "Idle over a year")
        self.assertEqual(S.idle_words(None), "Never played")

    def test_text_on_picks_the_readable_one(self):
        for bg in ('#ffffff', '#101010', '#c4a7e7', '#f6c177', '#26233a'):
            got = S.text_on(bg)
            other = theme.P['bg'] if got == theme.P['text'] else theme.P['text']
            self.assertGreaterEqual(theme.contrast(got, bg), theme.contrast(other, bg))

    def test_backlog_lines(self):
        ln = S.backlog_lines(payload()['backlog'])
        self.assertEqual([(n, w) for n, w, _t in ln['counts']],
                         [(14, "done"), (6, "playing"), (17, "started"), (13, "never played")])
        self.assertEqual(ln['left'], ("310 h", "of main stories left"))
        self.assertEqual(ln['pace'], "About 14 weeks at your pace")
        self.assertEqual(ln['idle'], "212.0 GB is held by games idle for a year")

    def test_backlog_without_pace_or_idle_or_hours(self):
        ln = S.backlog_lines({'done': 1, 'main_hours_left': 5, 'pace_hours_week': 0})
        self.assertEqual(ln['pace'], "Your pace shows here once you play a little")
        self.assertEqual(ln['idle'], "")
        ln = S.backlog_lines({'done': 1, 'main_hours_left': 0, 'pace_hours_week': 4})
        self.assertIsNone(ln['left'])
        self.assertEqual(ln['pace'], "")
        self.assertEqual(S.backlog_lines({'main_hours_left': 1, 'pace_hours_week': 9})['pace'],
                         "About 1 week at your pace")

    def test_fact_text_per_kind(self):
        self.assertEqual(S.fact_text({'kind': 'library', 'n': 44, 'hours': 812.4}),
                         ("Your library", "44 games", "About 812 h played in all"))
        self.assertEqual(S.fact_text({'kind': 'library', 'n': 1, 'hours': 0})[1], "1 game")
        self.assertEqual(S.fact_text({'kind': 'top_genre', 'name': 'RPG', 'n': 12})[1:],
                         ("RPG", "12 games in your library"))
        self.assertEqual(S.fact_text({'kind': 'shortest_unplayed', 'name': 'Gris', 'n': 4.0})[0],
                         "Shortest unplayed")
        self.assertIn("4 h", S.fact_text({'kind': 'shortest_unplayed', 'name': 'Gris', 'n': 4.0})[2])
        self.assertEqual(S.fact_text({'kind': 'oldest', 'name': 'Doom', 'n': 1993})[2],
                         "Released in 1993")
        self.assertIn("90.0 GB", S.fact_text({'kind': 'biggest', 'name': 'X', 'n': 90 * GB})[2])
        self.assertIn("3 games", S.fact_text({'kind': 'top_studio', 'name': 'S', 'n': 3})[2])


@unittest.skipIf(nx is None, "PyQt6 not available")
class WidgetCase(unittest.TestCase):
    KIND = ''

    def make(self, data, size='small', hub=None):
        self.hub = hub or FakeHub(data)
        w = D.DASH_WIDGETS[self.KIND]()
        self.addCleanup(w.deleteLater)
        w.size_, w.options = size, {}
        w.bind(self.hub)
        w.fill(self.hub)
        return w

    def hosted(self, w, width=700, height=420):
        host = QWidget()
        self.addCleanup(host.deleteLater)
        QVBoxLayout(host).addWidget(w)
        host.resize(width, height)
        host.show()
        QApplication.processEvents()
        return host


class Backlog(WidgetCase):
    KIND = 'backlog'

    def test_fills_with_bar_counts_and_words(self):
        w = self.make(payload())
        t = texts(w)
        for x in ("14", "done", "6", "playing", "17", "started", "13", "never played", "310 h",
                  "of main stories left", "About 14 weeks at your pace"):
            self.assertIn(x, t)
        self.assertTrue(any("212.0 GB is held by games idle for a year" in x for x in t))
        self.assertEqual(w.bar.accessibleName(), "14 done, 6 playing, 17 started, 13 never played")

    def test_bar_segments_are_proportional_and_gapped(self):
        w = self.make(payload())
        w.bar.resize(500, w.bar.height())
        seg = w.bar.segments()
        self.assertEqual(len(seg), 4)
        total = sum(wd for _x, wd, _t in seg)
        self.assertAlmostEqual(total + 3 * theme.scaled(2), 500, places=3)
        self.assertAlmostEqual(seg[0][1] / total, 14 / 50, places=4)
        w.bar.parts = [(5, 'accent'), (0, 'success')]
        self.assertEqual(len(w.bar.segments()), 1)

    def test_renders_in_two_themes(self):
        self.addCleanup(nx.apply_appearance, nx.APPEARANCE_DEFAULT)
        for name in ('forest', 'dawn'):
            nx.apply_appearance({'theme': name})
            w = self.make(payload())
            self.hosted(w, 360, 300)
            self.assertFalse(w.grab().isNull())

    def test_no_pace_says_so_kindly(self):
        b = dict(payload()['backlog'], pace_hours_week=0)
        w = self.make(payload(backlog=b))
        self.assertIn("Your pace shows here once you play a little", texts(w))

    def test_leaves_the_layout_when_empty(self):
        for data in (None, payload(backlog={}), payload(backlog={'done': 0, 'never': 0}),
                     payload(backlog='x')):
            w = self.make(data)
            self.assertFalse(w.has_content(self.hub), data)

    def test_unchanged_payload_keeps_the_widgets(self):
        w = self.make(payload())
        bar = w.bar
        w.fill(self.hub)
        self.assertIs(w.bar, bar)


class Space(WidgetCase):
    KIND = 'spacemap'

    def _map(self, data=None, **kw):
        w = self.make(data or payload(), size='wide')
        self.host = self.hosted(w)
        return w, w.map

    def test_blocks_fill_the_map_and_drop_hidden_games(self):
        w, m = self._map()
        bl = m.blocks()
        self.assertEqual([b['slug'] for b in bl], ['disco', 'tunic', 'hades', 'gris', None])
        self.assertEqual(bl[-1]['name'], "+ 7 more")
        area = sum(b['rect'].width() * b['rect'].height() for b in bl)
        self.assertGreater(area, 0.9 * m.width() * m.height())
        self.assertGreater(bl[0]['rect'].width() * bl[0]['rect'].height(),
                           bl[1]['rect'].width() * bl[1]['rect'].height())
        self.assertNotIn("Hidden One", " ".join(texts(w)) + m.accessibleDescription())

    def test_colours_follow_the_idle_buckets(self):
        w, m = self._map()
        self.assertEqual([b['key'] for b in m.blocks()], ['month', 'year', 'over', 'never', 'more'])
        t = texts(w)
        for words in ("Played this month", "Within a year", "Idle over a year", "Never played"):
            self.assertIn(words, t)

    def test_header_total_and_open_link(self):
        w, m = self._map()
        self.assertTrue(any("in your installed games" in x for x in texts(w)))
        got = []
        w.go.connect(got.append)
        next(b for b in w.findChildren(QPushButton) if b.text() == "Open").click()
        self.assertEqual(got, ['storage'])

    def test_arrows_move_to_the_nearest_block_by_centre(self):
        w, m = self._map()
        with mock.patch('nexus.gui.art.MOTION', False):
            m.focusInEvent(QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
            self.assertEqual(m.hovered, 0)
            bl = m.blocks()
            c0 = bl[0]['rect'].center()
            QTest.keyClick(m, Qt.Key.Key_Right)
            nxt = m.hovered
            if nxt == 0:                                  # the first block spans the full width
                QTest.keyClick(m, Qt.Key.Key_Down)
                nxt = m.hovered
            self.assertNotEqual(nxt, 0)
            c1 = bl[nxt]['rect'].center()
            self.assertTrue(c1.x() > c0.x() or c1.y() > c0.y())
            QTest.keyClick(m, Qt.Key.Key_Left)
            QTest.keyClick(m, Qt.Key.Key_Up)
            self.assertIsNotNone(m.hovered)
            self.assertTrue(m.card.isVisible())

    def test_enter_opens_the_game_and_the_more_block_does_nothing(self):
        w, m = self._map()
        with mock.patch('nexus.gui.art.MOTION', False):
            m.focusInEvent(QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
            QTest.keyClick(m, Qt.Key.Key_Return)
            self.assertEqual(self.hub.opened, [('page', 'disco')])
            m._move_to(4)                                  # "+ 7 more"
            QTest.keyClick(m, Qt.Key.Key_Return)
            self.assertEqual(self.hub.opened, [('page', 'disco')])

    def test_escape_goes_back_to_open(self):
        w, m = self._map()
        focused = []
        w.open_btn.setFocus = lambda reason=None: focused.append(reason)
        m.leave_requested.emit()
        self.assertEqual(len(focused), 1)

    def test_hover_card_says_name_size_and_idle_words(self):
        w, m = self._map()
        with mock.patch('nexus.gui.art.MOTION', False):
            i = 2
            m.mouseMoveEvent(_move(m, m.blocks()[i]['rect'].center().toPoint()))
            self.assertEqual(m.hovered, i)
            self.assertTrue(m.card.isVisible())
            d = m.card.accessibleDescription()
            self.assertIn("Hades", d)
            self.assertIn("30.0 GB", d)
            self.assertIn("Idle over a year", d)
            self.assertIs(m.card.parent(), self.host)
            m.leaveEvent(None)
            self.assertFalse(m.card.isVisible())

    def test_card_goes_with_the_map(self):
        w, m = self._map()
        with mock.patch('nexus.gui.art.MOTION', False):
            m._show_card(0)
            card = m.card
            self.assertTrue(card.isVisible())
            m.hide()
            self.assertFalse(card.isVisible())

    def test_takes_tab_focus_and_keeps_its_arrows(self):
        w, m = self._map()
        self.assertEqual(m.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertEqual(m.NATIVE_ARROWS, 'all')
        self.assertTrue(m.OWN_FOCUS_RING)

    def test_renders_in_two_themes(self):
        self.addCleanup(nx.apply_appearance, nx.APPEARANCE_DEFAULT)
        for name in ('forest', 'dawn'):
            nx.apply_appearance({'theme': name})
            w, m = self._map()
            m.focusInEvent(QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
            self.assertFalse(w.grab().isNull())

    def test_leaves_the_layout_without_sizes_or_with_only_hidden_ones(self):
        for data in (None, payload(space=[]), payload(space=[{'slug': 'hid', 'bytes': 5, 'idle_days': 1}]),
                     payload(space='x'), payload(space=[{'slug': 'disco', 'bytes': 0}])):
            w = self.make(data, size='wide')
            self.assertFalse(w.has_content(self.hub), data)

    def test_no_more_block_without_more(self):
        w, m = self._map(payload(space_more={'n': 0, 'bytes': 0}))
        self.assertNotIn(None, [b['slug'] for b in m.blocks()])


def _move(widget, pos):
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QMouseEvent
    return QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), widget.mapToGlobal(QPointF(pos)),
                       Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)


class FactLines(WidgetCase):
    """Library facts is gone (w-166): its best fact moves into Backlog health
    at Wide (the shortest unplayed game) and into Your year (most played)."""
    KIND = 'backlog'

    def test_wide_backlog_shows_the_shortest_unplayed_game(self):
        w = self.make(payload(), size='wide')
        t = texts(w)
        self.assertIn("Shortest unplayed", t)
        self.assertIn("Gris", t)
        self.assertTrue(any("4 h" in x for x in t))

    def test_it_links_to_the_game_page(self):
        w = self.make(payload(), size='wide')
        row = next(r for r in w.findChildren(type(w.link_block([], lambda: None))))
        row.clicked.emit()
        self.assertEqual(self.hub.opened, [('page', 'gris')])

    def test_small_backlog_has_no_fact(self):
        w = self.make(payload(), size='small')
        self.assertNotIn("Shortest unplayed", texts(w))

    def test_no_fact_when_the_game_is_hidden_or_the_payload_has_none(self):
        hid = [{'kind': 'shortest_unplayed', 'slug': 'hid', 'name': 'Hidden One', 'n': 1.0}]
        for data in (payload(facts=hid), payload(facts=[]), payload(facts='x'),
                     payload(facts=[{'kind': 'top_genre', 'name': 'RPG', 'n': 3}])):
            w = self.make(data, size='wide')
            self.assertNotIn("Shortest unplayed", texts(w))
            self.assertNotIn("Hidden One", texts(w))
            self.assertTrue(w.has_content(self.hub))

    def test_a_new_payload_fact_rebuilds(self):
        w = self.make(payload(), size='wide')
        other = [{'kind': 'shortest_unplayed', 'slug': 'tunic', 'name': 'Tunic', 'n': 2.0}]
        self.hub.dashboard.feed.data = payload(facts=other)
        w.fill(self.hub)
        self.assertIn("Tunic", texts(w))
        self.assertNotIn("Gris", texts(w))

    def test_the_retired_widget_is_gone(self):
        self.assertNotIn('facts', D.DASH_WIDGETS)
        self.assertFalse(hasattr(S, 'FactsWidget'))


if __name__ == '__main__':
    unittest.main()
