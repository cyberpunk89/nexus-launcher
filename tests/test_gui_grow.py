"""Cards that grow (w-155): given more height than they need, News, Wishlist
deals, Upcoming, Next milestones, Friends and Where your space goes show more
rows (a taller treemap). The extra rows never change the card's natural
height (the grid would grow it, measure again and loop), a row shows only
when it fits whole, and a refit at the same height builds nothing."""

import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from test_gui_layout import nx
except ImportError:             # run as tests.<module>
    from tests.test_gui_layout import nx
from tests.patching import set_gui  # noqa: F401  (imported for the sandbox's order)

if nx is not None:
    from PyQt6.QtWidgets import QApplication, QLabel
    from nexus.gui import dashboard as D
    from nexus.gui import insight_widgets as W
    from nexus.gui import stats_widgets as S
    from nexus.gui.widget_spec import SPEC
    try:
        from test_gui_insight_widgets import FakeHub as InsightHub, payload as insight_payload
        from test_gui_stats_widgets import FakeHub as StatsHub, payload as stats_payload
    except ImportError:
        from tests.test_gui_insight_widgets import FakeHub as InsightHub, payload as insight_payload
        from tests.test_gui_stats_widgets import FakeHub as StatsHub, payload as stats_payload

WIDTH = 520


class Grow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if nx is None:
            raise unittest.SkipTest("PyQt6 not available")

    def settle(self, w, height):
        w.resize(WIDTH, height)
        w.show()
        for _ in range(3):
            QApplication.processEvents()

    def natural(self, w):
        if not w.isVisible():                   # unpolished, it measures nothing
            self.settle(w, 10)
        lay = w.layout()
        return lay.totalHeightForWidth(WIDTH) if lay.hasHeightForWidth() else w.sizeHint().height()

    def shows_more_with_height(self, w, rows=lambda w: w.more.shown):
        """Natural height: no extra rows. A few rows taller: some. Taller
        still: more. The natural height stays the same throughout."""
        nat = self.natural(w)
        self.settle(w, nat)
        self.assertEqual(rows(w), 0)
        self.settle(w, nat + 120)
        some = rows(w)
        self.assertGreater(some, 0)
        self.settle(w, nat + 320)
        self.assertGreater(rows(w), some)
        self.assertEqual(self.natural(w), nat)             # hfw never follows the given height
        self.settle(w, nat)                                # and back: they leave again
        self.assertEqual(rows(w), 0)

    def no_refill_when_the_count_holds(self, w):
        """A few px more or less that keep the count: nothing built, nothing
        reported (the card does not ask for anything)."""
        nat = self.natural(w)
        h = nat + 90
        self.settle(w, h)
        while w.more.shown == 0 or w.more.shown != self.shown_at(w, h + 2):
            h += 1                                # not on a row's edge
            self.settle(w, h)
        host, shown = w.more, w.more.shown
        host.fit()                                # the look-ahead row is built by now
        builds, counts = host.builds, []
        host._on_count = counts.append
        for dh in (1, 2, 0):
            self.settle(w, h + dh)
            host.fit()
        self.assertIs(w.more, host)
        self.assertEqual((host.shown, host.builds), (shown, builds))
        self.assertEqual(counts, [])

    def shown_at(self, w, h):
        self.settle(w, h)
        n = w.more.shown
        self.settle(w, h - 2)
        return n

    def rows_whole(self, w):
        """Every showing extra row sits inside the host, whole."""
        for r in w.more._rows.values():
            if r.isVisibleTo(w.more):
                self.assertLessEqual(r.geometry().bottom() + 1, w.more.height())
                self.assertGreaterEqual(r.height(), r.minimumSizeHint().height())

    def test_specs(self):
        grows = {k for k, s in SPEC.items() if s.grows}
        self.assertEqual(grows, {'news', 'deals', 'upcoming', 'milestones', 'friends', 'spacemap'})

    # ---- News ----
    def news(self, n=12, size='small'):
        hub = mock.Mock(config={'news': True})
        hub.newspage = SimpleNamespace(
            data={'items': [{'title': f"Story {i}", 'source': "Src", 'url': f"https://x/{i}",
                             'ts': 0} for i in range(n)]},
            error=None, refresh=lambda *a, **k: None, _image_for=lambda *a: (None, None))
        w = D.NewsWidget()
        self.addCleanup(w.deleteLater)
        w.size_ = size
        w.fill(hub)
        return w

    def test_news_shows_more_rows_with_more_height(self):
        w = self.news()
        self.shows_more_with_height(w)
        self.rows_whole(w)

    def test_news_no_refill_when_the_count_holds(self):
        self.no_refill_when_the_count_holds(self.news())

    def test_news_with_nothing_more_has_no_host(self):
        self.assertIsNone(self.news(3).more)

    def test_a_card_that_does_not_grow_keeps_its_rows_at_the_top(self):
        w = self.news()
        with mock.patch.object(type(w), 'grows', property(lambda self: False)):
            w.fill(mock.Mock(config={'news': True}, newspage=SimpleNamespace(
                data={'items': [{'title': f"S{i}", 'ts': 0} for i in range(12)]},
                error=None, refresh=lambda *a, **k: None, _image_for=lambda *a: (None, None))))
            self.assertIsNone(w.more)
            self.settle(w, self.natural(w) + 300)
            rows = [i.widget() for i in (w.body.itemAt(k) for k in range(w.body.count()))
                    if i.widget() is not None]
            self.assertEqual(len(rows), 3)
            top = w.body.geometry().y()
            self.assertLess(rows[0].y() - top, 8)
            self.assertLess(rows[-1].geometry().bottom() - top, self.natural(w))

    # ---- News: your games first (w-167) ----
    TODAY = None

    def story(self, i, appid=None, days=1, kind=None):
        from datetime import date, timedelta
        d = date(2026, 11, 19) - timedelta(days=days)
        return {'title': f"Story {i}", 'source': "Src", 'url': f"https://x/{i}", 'ts': 0,
                'kind': kind or ('steam' if appid else 'feed'), 'appid': appid,
                'date': f"{d.isoformat()}T10:00:00"}

    def test_news_your_games_first(self):
        from datetime import date
        from nexus.gui import newspage as N
        items = [self.story(0, days=0), self.story(1, appid=7, days=2), self.story(2, appid=7, days=3),
                 self.story(3, appid=8, days=4), self.story(4, appid=9, days=5),   # 9: not shown
                 self.story(5, appid=8, days=20),                                  # too old
                 self.story(6, days=6), self.story(7, appid=7, days=1, kind='proton')]
        out = N.your_games_first(items, {7, 8}, 4, date(2026, 11, 19))
        self.assertEqual([i['title'] for i in out[:2]], ["Story 1", "Story 3"])   # one per game, newest
        self.assertEqual(len(out), len(items))                                   # nothing dropped
        self.assertEqual([i['title'] for i in out[2:]],
                         ["Story 0", "Story 2", "Story 4", "Story 5", "Story 6", "Story 7"])
        # At most half the rows (rounded up): n=3 allows 2, n=2 allows 1.
        many = [self.story(k, appid=k, days=1) for k in range(1, 6)]
        self.assertEqual(sum(1 for i in N.your_games_first(many, set(range(1, 6)), 3, date(2026, 11, 19))[:3]
                             if i['appid']), 3)       # 2 first, then the rest keep their order
        self.assertEqual([i['title'] for i in N.your_games_first(many, set(range(1, 6)), 2,
                                                                date(2026, 11, 19))[:2]],
                         ["Story 1", "Story 2"])
        self.assertEqual(N.your_games_first([], {1}, 3, date(2026, 11, 19)), [])
        self.assertEqual(N.your_games_first(items, set(), 4, date(2026, 11, 19)), items)

    def news_hub(self, items, games):
        hub = mock.Mock(config={'news': True})
        hub.newspage = SimpleNamespace(
            data={'items': items}, games_by_appid={g['appid']: g for g in games},
            error=None, refresh=lambda *a, **k: None, _image_for=lambda *a: (None, None))
        return hub

    def test_news_widget_orders_and_marks_update_ready(self):
        from datetime import date
        items = [self.story(0, days=0), self.story(1, days=1), self.story(2, days=1),
                 self.story(3, appid=7, days=5)]
        games = [{'appid': 7, 'slug': 'g', 'update_pending': True},
                 {'appid': 8, 'slug': 'h', 'hidden': True}]
        w = D.NewsWidget()
        self.addCleanup(w.deleteLater)
        w.size_ = 'small'
        with mock.patch.object(D, 'today', lambda: date(2026, 11, 19)):
            w.fill(self.news_hub(items, games))
        titles = [lab.text() for lab in w.findChildren(QLabel)
                  if lab.text().startswith("Story")]
        self.assertEqual(titles[0], "Story 3")                        # your game leads
        self.assertTrue(any(lab.text() == "Update ready" for lab in w.findChildren(QLabel)))
        self.assertEqual(sum(lab.text() == "Update ready" for lab in w.findChildren(QLabel)), 1)

    def test_news_without_games_or_dates_keeps_the_order(self):
        w = self.news()
        titles = [lab.text() for lab in w.findChildren(QLabel) if lab.text().startswith("Story")]
        self.assertEqual(titles[:3], ["Story 0", "Story 1", "Story 2"])

    # ---- Wishlist deals ----
    def deals(self, n=10, size='small'):
        wish = [{'title': f"Deal {i}", 'headerImage': None,
                 'price': {'onSale': True, 'currentStr': "$5", 'cutPct': 10 + i}}
                for i in range(n)]
        page = mock.Mock(error=None)
        page._data.return_value = {'wishlist': wish, 'radar': {'onSaleCount': n, 'totalSavings': 9},
                                   'currency': 'USD'}
        hub = mock.Mock(config={'gamedb_enabled': True}, dealspage=page)
        w = D.DealsWidget()
        self.addCleanup(w.deleteLater)
        w.size_ = size
        w.fill(hub)
        return w

    def test_deals_show_more_rows_with_more_height(self):
        w = self.deals()
        self.shows_more_with_height(w)
        self.rows_whole(w)
        self.assertIn("$5", [lab.text() for lab in w.more.findChildren(QLabel)])

    def test_deals_no_refill_when_the_count_holds(self):
        self.no_refill_when_the_count_holds(self.deals())

    # ---- Upcoming ----
    def upcoming(self, n=10, size='small'):
        rows = [{'appid': i, 'title': f"Game {i}", 'headerImage': None, 'owned': False,
                 'releaseDate': f"2027-0{1 + i % 9}-15T00:00:00.000Z",
                 'releaseDateText': f"15 Jan, 2027", 'steamUrl': f"https://s/{i}"}
                for i in range(1, n + 1)]
        page = mock.Mock(_loaded=True, error=None)
        page._data.return_value = {'schema': 1, 'wishlist': [], 'upcoming': rows}
        page._base.return_value = None
        hub = mock.Mock(config={'gamedb_enabled': True}, dealspage=page)
        w = D.UpcomingWidget()
        self.addCleanup(w.deleteLater)
        w.size_ = size
        w.fill(hub)
        return w

    def test_upcoming_shows_more_rows_with_more_height(self):
        w = self.upcoming()
        self.shows_more_with_height(w)
        self.rows_whole(w)
        self.settle(w, self.natural(w) + 120)
        left = [lab.text() for lab in w.findChildren(QLabel) if "Deals page" in lab.text()]
        self.assertEqual(len(left), 1)
        self.assertIn(f"+{10 - 3 - w.more.shown} more", left[0])     # the line counts what is left

    def test_upcoming_no_refill_when_the_count_holds(self):
        self.no_refill_when_the_count_holds(self.upcoming())

    # ---- Next milestones ----
    def milestones(self, size='small'):
        ms = [{'kind': 'hours', 'slug': s, 'name': s.title(), 'have': 90.0 + i, 'goal': 100}
              for i, s in enumerate(('disco', 'tunic', 'hades'))]
        ms += [{'kind': 'finished', 'slug': None, 'name': None, 'have': 5 + i, 'goal': 10}
               for i in range(8)]
        hub = InsightHub(insight_payload(milestones=ms))
        w = W.MilestonesWidget()
        self.addCleanup(w.deleteLater)
        w.size_, w.options = size, {}
        w.bind(hub)
        w.fill(hub)
        return w

    def test_milestones_show_more_goals_with_more_height(self):
        w = self.milestones()
        self.assertEqual(len(w.rows), W.SMALL_ROWS)
        self.shows_more_with_height(w)
        self.rows_whole(w)
        self.assertGreater(len(w.rows), W.SMALL_ROWS)

    def test_milestones_wide_add_whole_pairs(self):
        w = self.milestones('wide')
        self.settle(w, self.natural(w) + 200)
        self.assertGreater(w.more.shown, 0)
        self.assertEqual(w.more.shown % 2, 0)               # two columns: a row is two goals
        self.rows_whole(w)

    def test_milestones_no_refill_when_the_count_holds(self):
        self.no_refill_when_the_count_holds(self.milestones())

    # ---- Friends ----
    def friends(self, size='wide', n=9):
        entries = [{'appid': 100 + i, 'slug': None, 'title': f"Game {i}", 'minutes': 10 + i,
                    'headerImage': None,
                    'people': [{'steamid': str(i), 'name': f"Friend {i}", 'avatar': None}]}
                   for i in range(n)]
        hub = mock.Mock(config={'friends_enabled': True})
        w = D.FriendsWidget()
        self.addCleanup(w.deleteLater)
        w.size_ = size
        for name, val in (('_state_of', lambda *a: 'live'), ('_entries', lambda *a, **k: entries),
                          ('_set_footer', lambda *a, **k: None)):
            p = mock.patch.object(w, name, val)
            p.start()
            self.addCleanup(p.stop)
        w.fill(hub)
        return w

    def test_friends_show_more_games_with_more_height(self):
        w = self.friends()
        self.assertEqual(len(w.cards), D.FriendsWidget.WIDE_ROWS)
        self.shows_more_with_height(w)
        self.rows_whole(w)
        self.assertEqual(len(w.cards), D.FriendsWidget.WIDE_ROWS)    # cards: the base rows
        self.assertGreater(len(w._subs), D.FriendsWidget.WIDE_ROWS)  # grown ones keep their minutes

    def test_friends_no_refill_when_the_count_holds(self):
        self.no_refill_when_the_count_holds(self.friends())

    # ---- Where your space goes ----
    def space(self):
        hub = StatsHub(stats_payload())
        w = S.SpaceWidget()
        self.addCleanup(w.deleteLater)
        w.size_, w.options = 'wide', {}
        w.bind(hub)
        w.fill(hub)
        return w

    def test_space_map_is_taller_with_more_height_and_the_card_is_not(self):
        w = self.space()
        nat = self.natural(w)
        self.settle(w, nat)
        base = w.map.height()
        self.assertEqual(base, w.map.minimumHeight())
        self.settle(w, nat + 100)
        self.assertEqual(w.map.height(), base + 100)
        self.assertEqual(self.natural(w), nat)

    def test_space_map_leaves_the_legend_below_it(self):
        w = self.space()
        self.settle(w, self.natural(w) + 100)
        legend = w.body.itemAt(w.body.count() - 1).layout()
        self.assertGreaterEqual(legend.geometry().y(), w.map.geometry().bottom())


if __name__ == '__main__':
    unittest.main()
