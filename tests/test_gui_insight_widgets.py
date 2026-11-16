"""Home's insight widgets (w-139, w-140): On this day, Game of the day, Your
year (heatmap) and Next milestones. They fill from a fake insights payload,
leave the layout when they have nothing, and keep their action buttons real
(pad). The payload itself is tested in test_insights.py."""

import unittest
from datetime import date, timedelta

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize
from PyQt6.QtGui import QFocusEvent
from types import SimpleNamespace
from unittest import mock

try:
    from test_gui_layout import nx, stop_leftovers
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx, stop_leftovers
from tests.patching import set_gui

if nx is not None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QPushButton
    from nexus.gui import dashboard as D
    from nexus.gui import insight_widgets as W
    from nexus.gui import daycard as DC

TODAY = date(2026, 11, 16)                       # a Monday


def games():
    return [{'slug': 'disco', 'display_name': 'Disco Elysium', 'launcher': '/d'},
            {'slug': 'tunic', 'display_name': 'Tunic', 'launcher': '/t'},
            {'slug': 'hades', 'display_name': 'Hades', 'launcher': '/h'},
            {'slug': 'hid', 'display_name': 'Hidden One', 'launcher': '/x', 'hidden': True},
            {'slug': 'off', 'display_name': 'Not Installed', 'source': 'nexus', 'installed': False}]


def payload(**kw):
    p = {'v': 1, 'today': TODAY.isoformat(),
         'days': {(TODAY - timedelta(days=i)).isoformat(): 30.0 * (i % 4) for i in range(0, 120)},
         'streak': {'now': 3, 'best': 9}, 'busiest_day': 'Saturday', 'year_minutes': 8520.0,
         'on_this_day': [{'slug': 'disco', 'name': 'Disco Elysium', 'years_ago': 1,
                          'date': '2025-11-15', 'minutes': 160.0},
                         {'slug': 'tunic', 'name': 'Tunic', 'years_ago': 3,
                          'date': '2023-11-17', 'minutes': 45.0},
                         {'slug': 'hid', 'name': 'Hidden One', 'years_ago': 2,
                          'date': '2024-11-16', 'minutes': 500.0}],
         'daily': {'slug': 'hades', 'reason': 'released', 'years': 5},
         'daily_alt': {'slug': 'tunic', 'reason': 'pick', 'years': None},
         'milestones': [{'kind': 'hours', 'slug': 'disco', 'name': 'Disco Elysium',
                         'have': 97.0, 'goal': 100},
                        {'kind': 'finished', 'slug': None, 'name': None, 'have': 9, 'goal': 10},
                        {'kind': 'year', 'slug': None, 'name': None, 'have': 142.0, 'goal': 150},
                        {'kind': 'hours', 'slug': 'hid', 'name': 'Hidden One',
                         'have': 49.0, 'goal': 50}]}
    p.update(kw)
    return p


class FakeHub:
    def __init__(self, data, rerolled=None):
        self._games = games()
        self.dashboard = SimpleNamespace(feed=SimpleNamespace(data=data))
        self.prefs = SimpleNamespace(daily_rerolled=rerolled, save=mock.Mock())
        self.actions, self.opened, self.pages = [], [], []
        self.on_game_action = lambda a, g: self.actions.append((a, g['slug']))
        self._open_on_play = lambda g: self.opened.append(('play', g['slug']))
        self._tile_clicked = lambda g, mods: self.opened.append(('page', g['slug']))
        self.go = self.pages.append

    def game(self, slug):
        return next((g for g in self._games if g['slug'] == slug), None)


def texts(w):
    from PyQt6.QtWidgets import QLabel
    return [lab.text() for lab in w.findChildren(QLabel)]


def buttons(w):
    return [b.text() for b in w.findChildren(QPushButton) if not w.editbar.isAncestorOf(b) and not b.isHidden()]


class Pure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if nx is None:
            raise unittest.SkipTest("PyQt6 not available")

    def test_memories_drop_hidden_and_unknown_games(self):
        vis = {g['slug']: g for g in games() if not g.get('hidden')}
        out = W.memories(payload(), vis)
        self.assertEqual([m['slug'] for m in out], ['disco', 'tunic'])
        self.assertEqual(W.memories(None, vis), [])
        self.assertEqual(W.memories({'on_this_day': 'x'}, vis), [])

    def test_years_ago_wording(self):
        self.assertEqual(W.years_ago_text(1), "A year ago today")
        self.assertEqual(W.years_ago_text(3), "3 years ago today")

    def test_daily_choices_use_the_payload_then_a_seeded_pick(self):
        vis = {'hades': {'slug': 'hades'}, 'tunic': {'slug': 'tunic'}}
        first, alt = W.daily_choices(payload(), vis, TODAY)
        self.assertEqual((first['slug'], alt['slug']), ('hades', 'tunic'))
        # The payload's pick is a game the user hid: the next usable one steps up.
        p = payload(daily={'slug': 'hid', 'reason': 'pick', 'years': None})
        first, alt = W.daily_choices(p, vis, TODAY)
        self.assertEqual(first['slug'], 'tunic')
        self.assertIn(alt['slug'], vis)
        # No payload at all: a pick seeded by the date, the same all day.
        a = W.daily_choices(None, vis, TODAY)[0]
        b = W.daily_choices(None, vis, TODAY)[0]
        self.assertEqual(a, b)
        self.assertEqual(a['reason'], 'pick')
        self.assertEqual(W.daily_choices(None, {}, TODAY), (None, None))

    def test_daily_reason_text(self):
        self.assertEqual(W.reason_text({'reason': 'released', 'years': 1}), "Released a year ago today")
        self.assertEqual(W.reason_text({'reason': 'released', 'years': 7}), "Released 7 years ago today")
        self.assertEqual(W.reason_text({'reason': 'first_played', 'years': 2}),
                         "You first played it 2 years ago today")
        self.assertEqual(W.reason_text({'reason': 'pick', 'years': None}), "Today's pick")

    def test_heat_columns_end_on_this_week_and_stop_at_today(self):
        cols = W.heat_columns(TODAY, 4)
        self.assertEqual(len(cols), 4)
        self.assertTrue(all(len(c) == 7 for c in cols))
        self.assertEqual(cols[-1][0], TODAY)                       # a Monday: the week starts today
        self.assertTrue(all(d is None for d in cols[-1][1:]))      # the future is empty
        self.assertEqual(cols[0][0], TODAY - timedelta(days=21))
        mid = W.heat_columns(date(2026, 11, 19), 2)                # a Thursday
        self.assertEqual([d for d in mid[-1] if d], [date(2026, 11, 16) + timedelta(days=i)
                                                       for i in range(4)])

    def test_heat_level(self):
        self.assertEqual(W.heat_level(0, 100), 0)
        self.assertEqual(W.heat_level(5, 100), 1)
        self.assertEqual(W.heat_level(100, 100), 4)
        self.assertEqual(W.heat_level(25, 100), 1)
        self.assertEqual(W.heat_level(26, 100), 2)

    def test_milestone_text(self):
        self.assertEqual(W.milestone_text({'kind': 'hours', 'name': 'Tunic', 'have': 97.0, 'goal': 100}),
                         ("Tunic", "97 of 100 h", "3 h to go"))
        self.assertEqual(W.milestone_text({'kind': 'finished', 'have': 9, 'goal': 10})[0],
                         "Games finished")
        self.assertEqual(W.milestone_text({'kind': 'finished', 'have': 9, 'goal': 10})[2], "1 to go")
        self.assertEqual(W.milestone_text({'kind': 'year', 'have': 142.0, 'goal': 150})[0],
                         "Hours this year")


DAY = date(2026, 9, 22)                                        # a Tuesday
DET = {'n': 3, 'more': 0, 'first': '19:05', 'last': '23:10', 'streak_pos': {'day': 2, 'of': 4},
       'games': [{'slug': 'disco', 'name': 'Disco Elysium', 'minutes': 95.0, 'source': 'nexus'},
                 {'slug': 'hades', 'name': 'Hades', 'minutes': 25.0, 'source': 'steam'},
                 {'slug': 'tunic', 'name': 'Tunic', 'minutes': 20.0, 'source': 'estimated'}]}


@unittest.skipIf(nx is None, "PyQt6 not available")
class DayCardText(unittest.TestCase):
    def test_header_line_two_rows_and_footer(self):
        ct = DC.card_text(DAY, 140.0, DET, 100.0, TODAY)
        self.assertEqual((ct.head, ct.total), ("Tuesday 22 Sep", "2 h 20 min"))
        self.assertEqual(ct.line2, "3 sessions \u00b7 19:05 \u2013 23:10")
        self.assertEqual(ct.footer, "Day 2 of a 4-day streak \u00b7 40 min above average")
        self.assertEqual([(r.name, r.time, r.pill) for r in ct.rows],
                         [("Disco Elysium", "1 h 35 min", ""), ("Hades", "25 min", "Steam"),
                          ("Tunic", "20 min", "Estimated")])
        self.assertAlmostEqual(ct.rows[0].share, 95 / 140)
        self.assertEqual(ct.rows[0].source, "Nexus")        # the word is kept, the pill is not shown

    def test_other_year_names_the_year(self):
        self.assertEqual(DC.card_text(DAY.replace(year=2025), 10, None, 0, TODAY).head,
                         "Monday 22 Sep 2025")

    def test_empty_day_is_one_line(self):
        ct = DC.card_text(date(2026, 7, 15), 0, None, 90.0, TODAY)
        self.assertEqual(ct.text(), "Wednesday 15 Jul \u00b7 no play")
        self.assertEqual((ct.rows, ct.line2, ct.footer), ([], "", ""))

    def test_one_session_one_time_and_no_streak_no_footer_without_average(self):
        det = dict(DET, n=1, last='19:05', streak_pos=None)
        ct = DC.card_text(DAY, 60.0, det, 0.0, TODAY)
        self.assertEqual(ct.line2, "1 session \u00b7 19:05")
        self.assertEqual(ct.footer, "")

    def test_below_and_about_average_are_said_kindly(self):
        det = dict(DET, streak_pos=None)
        self.assertEqual(DC.card_text(DAY, 60.0, det, 100.0, TODAY).footer, "40 min below average")
        self.assertEqual(DC.card_text(DAY, 98.0, det, 100.0, TODAY).footer, "About your average day")

    def test_more_and_total_without_detail(self):
        ct = DC.card_text(DAY, 30.0, dict(DET, more=2), 0, TODAY)
        self.assertEqual(ct.more, "+ 2 more")
        bare = DC.card_text(DAY, 30.0, None, 0, TODAY)
        self.assertEqual((bare.total, bare.rows), ("30 min", []))

    def test_text_carries_every_word(self):
        t = DC.card_text(DAY, 140.0, DET, 100.0, TODAY).text()
        for w in ("Tuesday 22 Sep", "2 h 20 min", "3 sessions", "19:05", "Hades, 25 min, Steam",
                  "Day 2 of a 4-day streak", "40 min above average"):
            self.assertIn(w, t)

    def test_place_right_then_flip_left_then_up(self):
        b = QRectF(0, 0, 800, 400)
        size = QSize(300, 180)
        cell = QRectF(100, 50, 12, 12)
        p = DC.place_card(cell, size, b, 8, 4)
        self.assertEqual(p.x(), cell.right() + 8)
        near = QRectF(700, 50, 12, 12)
        p = DC.place_card(near, size, b, 8, 4)
        self.assertEqual(p.x(), near.left() - 8 - 300)
        low = QRectF(100, 380, 12, 12)
        p = DC.place_card(low, size, b, 8, 4)
        self.assertEqual(p.y(), 400 - 4 - 180)
        tiny = DC.place_card(cell, QSize(900, 500), b, 8, 4)      # bigger than the window
        self.assertEqual((tiny.x(), tiny.y()), (4.0, 4.0))


@unittest.skipIf(nx is None, "PyQt6 not available")
class WidgetCase(unittest.TestCase):
    CLS = None

    def make(self, data, size='small', hub=None, **opts):
        self.hub = hub or FakeHub(data)
        w = self.CLS()
        self.addCleanup(w.deleteLater)
        w.size_, w.options = size, opts
        w.bind(self.hub)
        w.fill(self.hub)
        return w


class OnThisDay(WidgetCase):
    CLS = D.DASH_WIDGETS['onthisday'] if nx is not None else None

    def test_fills_with_the_closest_memory(self):
        w = self.make(payload())
        t = texts(w)
        self.assertIn("A year ago today", t)
        self.assertIn("Disco Elysium", t)
        self.assertTrue(any("2 h 40 min" in x and "15 Nov 2025" in x for x in t), t)
        self.assertTrue(w.has_content(self.hub))
        self.assertNotIn("Hidden One", t)

    def test_play_again_is_a_real_button_that_plays(self):
        w = self.make(payload())
        b = next(b for b in w.findChildren(QPushButton) if b.text() == "Play again" and not b.isHidden())
        self.assertNotEqual(b.focusPolicy(), Qt.FocusPolicy.NoFocus)
        b.click()
        self.assertEqual(self.hub.actions, [('launch', 'disco')])

    def test_a_game_that_cannot_start_opens_its_page(self):
        data = payload(on_this_day=[{'slug': 'off', 'name': 'Not Installed', 'years_ago': 2,
                                     'date': '2024-11-16', 'minutes': 60.0}])
        w = self.make(data)
        b = next(b for b in w.findChildren(QPushButton) if b.text() == "Open page")
        b.click()
        self.assertEqual((self.hub.actions, self.hub.opened), ([], [('play', 'off')]))

    def test_wide_lists_the_other_memories(self):
        w = self.make(payload(), size='wide')
        self.assertIn("Tunic", texts(w))

    def test_nothing_to_say_leaves_the_layout(self):
        for data in (None, payload(on_this_day=[]),
                     payload(on_this_day=[{'slug': 'hid', 'name': 'x', 'years_ago': 1,
                                           'date': '2025-11-16', 'minutes': 5.0}])):
            w = self.make(data)
            self.assertFalse(w.has_content(self.hub))

    def test_unchanged_payload_keeps_the_widgets(self):
        w = self.make(payload())
        b = next(b for b in w.findChildren(QPushButton) if b.text() == "Play again")
        w.fill(self.hub)
        self.assertIs(next(b2 for b2 in w.findChildren(QPushButton) if b2.text() == "Play again"), b)


class Daily(WidgetCase):
    CLS = D.DASH_WIDGETS['daily'] if nx is not None else None

    def test_fills_with_the_pick_and_its_reason(self):
        w = self.make(payload())
        t = texts(w)
        self.assertIn("Hades", t)
        self.assertIn("Released 5 years ago today", t)
        self.assertTrue(w.has_content(self.hub))
        self.assertIn("Another day", buttons(w))

    def test_play_button(self):
        w = self.make(payload())
        next(b for b in w.findChildren(QPushButton) if b.text() == "Play").click()
        self.assertEqual(self.hub.actions, [('launch', 'hades')])

    def test_another_day_rerolls_once_and_is_remembered(self):
        w = self.make(payload())
        next(b for b in w.findChildren(QPushButton) if b.text() == "Another day").click()
        self.assertEqual(self.hub.prefs.daily_rerolled, TODAY.isoformat())
        self.hub.prefs.save.assert_called()
        self.assertIn("Tunic", texts(w))
        self.assertNotIn("Another day", buttons(w))                # once per day

    def test_remembered_across_a_restart(self):
        w = self.make(payload(), hub=FakeHub(payload(), rerolled=TODAY.isoformat()))
        self.assertIn("Tunic", texts(w))
        self.assertNotIn("Another day", buttons(w))

    def test_a_new_day_gives_the_re_roll_back(self):
        old = (TODAY - timedelta(days=1)).isoformat()
        w = self.make(payload(), hub=FakeHub(payload(), rerolled=old))
        self.assertIn("Hades", texts(w))
        self.assertIn("Another day", buttons(w))

    def test_leaves_the_layout_with_no_game(self):
        hub = FakeHub(payload())
        hub._games = []
        w = self.make(payload(), hub=hub)
        self.assertFalse(w.has_content(hub))

    def test_a_hidden_game_is_never_the_pick(self):
        data = payload(daily={'slug': 'hid', 'reason': 'pick', 'years': None})
        w = self.make(data)
        self.assertNotIn("Hidden One", texts(w))


class Heatmap(WidgetCase):
    CLS = D.DASH_WIDGETS['heatmap'] if nx is not None else None

    def test_stats_come_from_the_payload(self):
        w = self.make(payload(), size='full')
        t = " | ".join(texts(w))
        self.assertIn("3 day streak", t)
        self.assertIn("Best this year", t)
        self.assertIn("9 days", t)
        self.assertIn("Saturday", t)
        self.assertIn("142.0 h", t)

    def test_no_streak_is_said_kindly(self):
        w = self.make(payload(streak={'now': 0, 'best': 0}))
        self.assertIn("No streak right now", " | ".join(texts(w)))

    def test_as_many_weeks_as_fit_up_to_52(self):
        w = self.make(payload(), size='full')
        g = w.grid
        g.resize(2000, g.height())
        self.assertEqual(g.weeks_fit(), 52)
        g.resize(400, g.height())
        n = g.weeks_fit()
        self.assertTrue(4 <= n < 52)
        self.assertLessEqual(n * (g.cell + g.gap) - g.gap + g.left_pad(), 400)
        g.resize(250, g.height())
        self.assertLess(g.weeks_fit(), n)

    def test_today_is_ringed(self):
        w = self.make(payload(), size='full')
        w.grid.resize(800, w.grid.height())
        cell = w.grid.cell_rect(TODAY)
        self.assertIsNotNone(cell)
        self.assertEqual(w.grid.today_cell(), TODAY)

    def test_hovered_cell_is_marked_and_cleared_on_leave(self):
        w = self.make(payload(), size='full')
        g = w.grid
        g.resize(800, g.height())
        d = TODAY - timedelta(days=3)
        g.hover_at(g.cell_rect(d).center().toPoint())
        self.assertEqual(g.hovered, d)
        g.hover_at(QPoint(1, 1))                           # off every cell
        self.assertIsNone(g.hovered)
        g.hover_at(g.cell_rect(d).center().toPoint())
        g.leaveEvent(None)
        self.assertIsNone(g.hovered)

    def _host(self, data, width=900):
        """The heatmap in a plain window, so the card has a window to live in."""
        from PyQt6.QtWidgets import QVBoxLayout, QWidget
        w = self.make(data, size='full')
        host = QWidget()
        self.addCleanup(host.deleteLater)
        QVBoxLayout(host).addWidget(w)
        host.resize(width, 320)
        host.show()
        QApplication.processEvents()
        return host, w.grid

    def _hover(self, g, d):
        g.hover_at(g.cell_rect(d).center().toPoint())

    def _detail(self):
        day = TODAY - timedelta(days=1)
        return day, payload(day_games={day.isoformat(): dict(DET)}, avg_minutes=100.0)

    def test_hover_shows_the_card_beside_the_cell_inside_the_window(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data)
            before = set(QApplication.topLevelWidgets())
            self._hover(g, day)
            self.assertIs(g.card.parent(), host)
            self.assertTrue(g.card.isVisible())
            self.assertEqual(set(QApplication.topLevelWidgets()), before)    # no window made
            cell = QRectF(g.mapTo(host, g.cell_rect(day).topLeft().toPoint()).x(),
                          g.mapTo(host, g.cell_rect(day).topLeft().toPoint()).y(), g.cell, g.cell)
            card = g.card.card_rect().translated(QPointF(g.card.pos()))
            self.assertTrue(QRectF(host.rect()).contains(card))
            self.assertTrue(card.left() >= cell.right() or card.right() <= cell.left())
            self.assertTrue(g.card.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
            self.assertEqual(g.card.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_card_flips_left_at_the_right_edge_and_moves_up_at_the_bottom(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data, width=520)
            g.resize(500, g.height())
            QApplication.processEvents()
            self._hover(g, day)
            cell = g.mapTo(host, g.cell_rect(day).topLeft().toPoint())
            card = g.card.card_rect().translated(QPointF(g.card.pos()))
            self.assertLessEqual(card.right(), cell.x())                      # flipped left
            self.assertGreaterEqual(card.left(), 0)
            host.resize(host.width(), 150)                                    # shorter than the card
            QApplication.processEvents()
            self._hover(g, TODAY)
            self._hover(g, day)
            card = g.card.card_rect().translated(QPointF(g.card.pos()))
            self.assertLessEqual(card.bottom(), host.height())
            self.assertGreaterEqual(card.top(), 0)

    def test_empty_day_shows_one_line(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data)
            self._hover(g, TODAY - timedelta(days=8))
            self.assertTrue(g.card.isVisible())
            self.assertEqual(g.card.ct.rows, [])
            self.assertIn("no play", g.card.accessibleDescription())
            self.assertLess(g.card.height(), self._full_height(g, day))

    def _full_height(self, g, day):
        g._show_card(day)
        return g.card.height()

    def test_leaving_the_grid_hides_the_card(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data)
            self._hover(g, day)
            self.assertTrue(g.card.isVisible())
            g.leaveEvent(None)
            self.assertFalse(g.card.isVisible())

    def test_motion_off_has_no_delay_motion_on_waits(self):
        day, data = self._detail()
        host, g = self._host(data)
        with mock.patch('nexus.gui.art.MOTION', False):
            self._hover(g, day)
            self.assertTrue(g.card.isVisible())
            g.leaveEvent(None)
        with mock.patch('nexus.gui.art.MOTION', True):
            self._hover(g, day)
            self.assertFalse(g.card.isVisible())            # after the short delay
            self.assertTrue(g.show_timer.isActive())
            g.show_timer.timeout.emit()
            self.assertTrue(g.card.isVisible())
            self.assertEqual(g.card.opacity(), 0.0)           # fades in from nothing
            g.card.vanish()
            g.card.appear()
            g.card._fade.stop()

    def test_moving_between_cells_moves_the_card_at_once(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', True):
            host, g = self._host(data)
            g._show_card(day)
            other = TODAY - timedelta(days=9)
            self._hover(g, other)
            self.assertFalse(g.show_timer.isActive())
            self.assertIn(f"{other:%A} {other.day}", g.card.accessibleDescription())

    def test_accessible_text_is_the_card_text(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data)
            self._hover(g, day)
            self.assertEqual(g.card.accessibleDescription(), g.card_text(day).text())
            self.assertIn("Day 2 of a 4-day streak", g.card.accessibleDescription())
            self.assertEqual(g.card.accessibleName(), g.card_text(day).head)

    def test_the_qtooltip_is_gone(self):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QHelpEvent
        day, data = self._detail()
        host, g = self._host(data)
        with mock.patch('nexus.gui.insight_widgets.QToolTip', create=True) as tip:
            ev = QHelpEvent(QEvent.Type.ToolTip, g.cell_rect(day).center().toPoint(), QPoint(5, 5))
            g.event(ev)
            tip.showText.assert_not_called()
        self.assertFalse(hasattr(g, 'tip_at'))

    def test_card_paints_in_forest_and_dawn_with_initial_fallback(self):
        day, data = self._detail()
        self.assertEqual(DC.DayCard.initial("Disco Elysium"), "D")
        self.assertEqual(DC.DayCard.initial("  ?!x"), "X")
        self.addCleanup(nx.apply_appearance, nx.APPEARANCE_DEFAULT)
        for theme in ('forest', 'dawn'):
            with self.subTest(theme=theme), mock.patch('nexus.gui.art.MOTION', False):
                nx.apply_appearance({'theme': theme})
                host, g = self._host(data)
                self._hover(g, day)
                self.assertFalse(g.card.grab().isNull())

    # ---- keyboard and pad (w-149) ----
    def _focus(self, g, reason=Qt.FocusReason.TabFocusReason):
        g.focusInEvent(QFocusEvent(QEvent.Type.FocusIn, reason))

    def _key(self, g, key):
        QTest.keyClick(g, key)

    def test_grid_takes_tab_focus_and_keeps_arrows(self):
        from nexus.gui.input import _native_arrows
        w = self.make(payload(), size='full')
        self.assertEqual(w.grid.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        for k in (Qt.Key.Key_Left, Qt.Key.Key_Up):          # SpatialNav must not steal them
            self.assertTrue(_native_arrows(w.grid, k))
        self.assertTrue(w.grid.OWN_FOCUS_RING)              # the grid draws its own ring

    def test_tab_lands_on_today_and_shows_its_card(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', True):       # the keyboard shows it at once
            host, g = self._host(data)
            self._focus(g)
            self.assertEqual(g.hovered, TODAY)
            self.assertTrue(g.card.isVisible())
            self.assertEqual(g.accessibleDescription(), g.card.accessibleDescription())

    def test_mouse_focus_keeps_the_hovered_day(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data)
            self._hover(g, day)
            self._focus(g, Qt.FocusReason.MouseFocusReason)
            self.assertEqual(g.hovered, day)

    def test_arrows_move_a_week_or_a_day_and_the_card_follows(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data)
            self._focus(g)
            self._key(g, Qt.Key.Key_Left)
            self.assertEqual(g.hovered, TODAY - timedelta(days=7))
            self._key(g, Qt.Key.Key_Up)
            self.assertEqual(g.hovered, TODAY - timedelta(days=8))
            self._key(g, Qt.Key.Key_Down)
            self._key(g, Qt.Key.Key_Right)
            self.assertEqual(g.hovered, TODAY)
            self._key(g, Qt.Key.Key_Right)                   # no future week: stays
            self._key(g, Qt.Key.Key_Down)                    # nor a future day
            self.assertEqual(g.hovered, TODAY)
            self.assertEqual(g.accessibleDescription(), g.card.accessibleDescription())
            self.assertIn(f"{g.hovered:%A}", g.card.accessibleDescription())

    def test_home_and_end_jump_to_the_first_and_last_week(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data)
            self._focus(g)
            self._key(g, Qt.Key.Key_Left)
            first = g._columns()[0]
            at = g.hovered
            self._key(g, Qt.Key.Key_Home)
            self.assertEqual(g.hovered, first[at.weekday()])
            self._key(g, Qt.Key.Key_Left)                    # past the first week: stays
            self.assertEqual(g.hovered, first[at.weekday()])
            self._key(g, Qt.Key.Key_End)
            self.assertEqual(g.hovered, TODAY - timedelta(days=TODAY.weekday() - at.weekday())
                             if at.weekday() <= TODAY.weekday() else TODAY)
            self._key(g, Qt.Key.Key_Escape)

    def test_focus_ring_follows_the_cell_and_goes_with_focus(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data)
            self.assertFalse(g.keyboard)
            self._focus(g)
            self.assertTrue(g.keyboard)
            self.assertFalse(g.grab().isNull())
            g.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason))
            self.assertFalse(g.keyboard)
            self.assertIsNone(g.hovered)
            self.assertFalse(g.card.isVisible())

    def test_mouse_leaving_keeps_the_keyboard_cell(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data)
            self._focus(g)
            g.leaveEvent(None)
            self.assertEqual(g.hovered, TODAY)
            self.assertTrue(g.card.isVisible())

    def test_enter_asks_for_the_day_and_escape_asks_to_leave(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data)
            got, left = [], []
            g.activated.connect(got.append)
            g.leave_requested.connect(lambda: left.append(1))
            self._focus(g)
            self._key(g, Qt.Key.Key_Left)
            self._key(g, Qt.Key.Key_Return)
            self.assertEqual(got, [TODAY - timedelta(days=7)])
            self._key(g, Qt.Key.Key_Escape)
            self.assertEqual(left, [1])

    def test_enter_target_is_one_small_function(self):
        self.assertEqual(W.enter_target(DET), ('game', 'disco'))
        self.assertEqual(W.enter_target(None), ('history', None))
        self.assertEqual(W.enter_target({'games': []}), ('history', None))
        with mock.patch.object(W, 'ENTER_OPENS', 'history'):
            self.assertEqual(W.enter_target(DET), ('history', None))

    def test_enter_opens_the_top_game_or_play_history(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data)
            w = host.layout().itemAt(0).widget()
            pages = []
            w.go.connect(pages.append)
            g.activated.emit(day)
            self.assertEqual(self.hub.opened, [('page', 'disco')])
            g.activated.emit(TODAY - timedelta(days=8))      # nothing played
            self.assertEqual(pages, ['history'])
            # a top game Home does not show (hidden) falls back to Play history
            hid = payload(day_games={day.isoformat(): dict(
                DET, games=[dict(DET['games'][0], slug='hid')])})
            self.hub.dashboard.feed.data = hid
            w.fill(self.hub)
            g2 = w.grid
            g2.activated.emit(day)
            self.assertEqual(pages, ['history', 'history'])

    def test_escape_returns_focus_to_open(self):
        day, data = self._detail()
        with mock.patch('nexus.gui.art.MOTION', False):
            host, g = self._host(data)
            w = host.layout().itemAt(0).widget()
            focused = []
            btn = w.open_btn
            btn.setFocus = lambda reason=None: focused.append(reason)
            g.leave_requested.emit()
            self.assertEqual(len(focused), 1)

    def test_renders(self):
        w = self.make(payload(), size='full')
        w.resize(900, 300)
        self.assertFalse(w.grab().isNull())

    def test_open_goes_to_play_history(self):
        w = self.make(payload(), size='full')
        got = []
        w.go.connect(got.append)
        next(b for b in w.findChildren(QPushButton) if b.text() == "Open").click()
        self.assertEqual(got, ['history'])

    def test_no_payload_shows_a_kind_empty_state(self):
        w = self.make(None)
        self.assertTrue(w.has_content(self.hub))
        self.assertTrue(any("play" in x.lower() for x in texts(w)))


class Milestones(WidgetCase):
    CLS = D.DASH_WIDGETS['milestones'] if nx is not None else None

    def test_rows_and_links(self):
        w = self.make(payload())
        t = texts(w)
        self.assertIn("Disco Elysium", t)
        self.assertIn("97 of 100 h", t)
        self.assertIn("Games finished", t)
        self.assertNotIn("Hidden One", t)                          # a hidden game has no link
        w.rows[0].clicked.emit()
        self.assertEqual(self.hub.opened, [('page', 'disco')])

    def test_library_wide_rows_open_a_page(self):
        w = self.make(payload())
        by = {r.property('kind'): r for r in w.rows}
        by['finished'].clicked.emit()
        by['year'].clicked.emit()
        self.assertEqual(self.hub.pages, ['library', 'history'])

    def test_leaves_the_layout_when_nothing_is_close(self):
        w = self.make(payload(milestones=[]))
        self.assertFalse(w.has_content(self.hub))
        w = self.make(None)
        self.assertFalse(w.has_content(self.hub))

    def test_wide_shows_more_rows_than_small(self):
        s = self.make(payload(), size='small')
        wd = self.make(payload(), size='wide')
        self.assertLessEqual(len(s.rows), W.SMALL_ROWS)
        self.assertGreaterEqual(len(wd.rows), len(s.rows))


@unittest.skipIf(nx is None, "PyQt6 not available")
class TheatreCards(unittest.TestCase):
    """Theatre's Home cards from the payload (w-144): pure, same words as the widgets."""

    def shown(self):
        return {g['slug']: g for g in games() if not g.get('hidden')}

    def test_cards_in_order_with_the_widgets_words(self):
        cards = W.theatre_cards(payload(), self.shown(), TODAY)
        self.assertEqual([c['key'] for c in cards],
                         ['card:onthisday', 'card:daily', 'card:milestones'])
        otd, daily, ms = cards
        self.assertEqual((otd['kicker'], otd['title'], otd['slug']),
                         ("ON THIS DAY", "Disco Elysium", 'disco'))
        self.assertTrue(otd['sub'].startswith("A year ago today"))
        self.assertEqual((daily['kicker'], daily['title'], daily['sub'], daily['slug']),
                         ("GAME OF THE DAY", "Hades", "Released 5 years ago today", 'hades'))
        self.assertEqual((ms['kicker'], ms['title'], ms['slug']),
                         ("NEXT MILESTONE", "Disco Elysium", 'disco'))
        self.assertIn("97 of 100 h", ms['sub'])
        for c in cards:
            self.assertEqual(set(c), {'key', 'kicker', 'title', 'sub', 'glyph', 'slug'})

    def test_hidden_games_and_no_data_give_no_card(self):
        self.assertEqual(W.theatre_cards(None, self.shown(), TODAY), [])
        self.assertEqual(W.theatre_cards(payload(), {}, TODAY), [])
        only_hidden = payload(on_this_day=[payload()['on_this_day'][2]],
                              milestones=[payload()['milestones'][3]])
        keys = [c['key'] for c in W.theatre_cards(only_hidden, self.shown(), TODAY)]
        self.assertEqual(keys, ['card:daily'])

    def test_a_milestone_without_a_game_is_skipped(self):
        data = payload(milestones=payload()['milestones'][1:3])     # finished, year: no page to open
        keys = [c['key'] for c in W.theatre_cards(data, self.shown(), TODAY)]
        self.assertNotIn('card:milestones', keys)


class InTheRegistry(unittest.TestCase):
    def test_specs_and_tables(self):
        for kind, sizes, default in (('onthisday', ('small', 'wide'), 'small'),
                                     ('daily', ('small',), 'small'),
                                     ('heatmap', ('wide', 'full'), 'full'),
                                     ('milestones', ('small', 'wide'), 'small')):
            self.assertEqual(D.DASH_SIZES[kind], sizes)
            self.assertEqual(D.DASH_DEFAULT_SIZE[kind], default)
            self.assertIn(kind, D.FED_BY['insights'])
            if kind != 'milestones':
                self.assertIn('midnight', D.SPEC[kind].feeds)
            self.assertTrue(D.SPEC[kind].hidden)                    # in Add widgets, not in a preset
            self.assertIn(kind, D.DASH_WIDGETS)

    def test_old_layouts_keep_their_shape(self):
        out = nx.dash_normalize([{'id': 'stage'}, {'id': 'week', 'size': 'wide'}])
        ids = [i['id'] for i in out]
        for kind in ('onthisday', 'daily', 'heatmap', 'milestones', 'backlog', 'spacemap', 'facts'):
            self.assertTrue(next(i for i in out if i['id'] == kind)['hidden'])
        self.assertEqual(ids[-7:], ['onthisday', 'daily', 'heatmap', 'milestones',
                                    'backlog', 'spacemap', 'facts'])   # last, hidden
        self.assertEqual(D.DASH_VERSION, 2)

    def test_cinematic_preset_is_unchanged(self):
        shown = [i['id'] for i in nx.dash_default() if not i['hidden']]
        for kind in ('onthisday', 'daily', 'heatmap', 'milestones', 'backlog', 'spacemap', 'facts'):
            self.assertNotIn(kind, shown)


@unittest.skipIf(nx is None, "PyQt6 not available")
class PrefsField(unittest.TestCase):
    """Game of the day's "Another day" is saved in library.json (w-139)."""

    def prefs(self, data=None):
        import json
        import tempfile
        from pathlib import Path
        from nexus.gui.prefs import LibraryPrefs
        tmp = Path(tempfile.mkdtemp()) / 'library.json'
        self.addCleanup(lambda: tmp.exists() and tmp.unlink())
        if data is not None:
            tmp.write_text(json.dumps(data), encoding='utf-8')
        p = mock.patch.object(LibraryPrefs, 'PATH', tmp)
        p.start()
        self.addCleanup(p.stop)
        return LibraryPrefs, tmp

    def test_an_old_file_has_no_re_roll(self):
        cls, _ = self.prefs({'favorites': ['a']})
        self.assertIsNone(cls().daily_rerolled)

    def test_round_trip_and_bad_values(self):
        import json
        cls, tmp = self.prefs({})
        p = cls()
        p.daily_rerolled = '2026-11-16'
        p.save()
        self.assertEqual(json.loads(tmp.read_text(encoding='utf-8'))['daily_rerolled'], '2026-11-16')
        self.assertEqual(cls().daily_rerolled, '2026-11-16')
        for bad in (5, 'yesterday', ['2026-11-16'], {}):
            _, tmp2 = self.prefs({'daily_rerolled': bad})
            self.assertIsNone(cls().daily_rerolled, bad)


@unittest.skipIf(nx is None, "PyQt6 not available")
class OnHome(unittest.TestCase):
    """In the real window: a widget with nothing leaves the grid, and an
    answer arriving later brings it in."""

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
        p = mock.patch.object(nx.MainWindow, '_poll_running', lambda self: None)
        p.start()
        self.addCleanup(p.stop)
        self.win = nx.MainWindow()
        self.win.resize(1300, 850)
        self.win.prefs.close_to_tray_set = True
        self.win.show()
        self.win._on_library({'games': [dict(g, installed=True) for g in games()
                                        if g['slug'] in ('disco', 'tunic', 'hades')],
                              'disks': []}, '')
        items = nx.dash_kind_defaults()
        for it in items:
            if it['id'] in ('onthisday', 'daily', 'heatmap', 'milestones'):
                it['hidden'] = False
        self.win.prefs.dashboard = items
        self.win.dashboard.refresh()
        QApplication.processEvents()

    def tearDown(self):
        from PyQt6.QtCore import QEvent
        stop_leftovers(self.win.jobs)
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

    def test_leave_and_join_the_layout_with_the_answer(self):
        d = self.win.dashboard
        self.assertTrue({'onthisday', 'milestones'} <= d._skip())     # nothing asked yet
        d.feed.data = payload()
        self.win.dash_update('insights')
        self.win._flush_dashboard()
        QApplication.processEvents()
        self.assertFalse({'onthisday', 'daily', 'heatmap', 'milestones'} & d._skip())
        self.assertEqual(d._skipped & {'onthisday', 'milestones'}, set())
        self.assertTrue(d.widgets['onthisday'].isVisible())
        d.feed.data = payload(on_this_day=[], milestones=[])
        self.win.dash_update('insights')
        self.win._flush_dashboard()
        QApplication.processEvents()
        self.assertTrue({'onthisday', 'milestones'} <= d._skipped)
        self.assertFalse(d.widgets['onthisday'].isVisible())

    def test_midnight_refreshes_the_daily_pick(self):
        d = self.win.dashboard
        d.feed.data = payload()
        with mock.patch.object(d.widgets['daily'], 'fill') as fill:
            self.win.dash_update('midnight')
            self.win._flush_dashboard()
        self.assertTrue(fill.called)

    def test_the_insights_are_asked_for_when_a_widget_shows(self):
        d = self.win.dashboard
        self.assertTrue({'onthisday', 'daily', 'heatmap', 'milestones'} <= d.insights_consumers())


if __name__ == '__main__':
    unittest.main()
