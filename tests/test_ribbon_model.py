"""Theatre's Ribbon layout, the pure part (w-108): which categories and items
come out of what Theatre holds, the empty states, moving through them, and
stepping a setting. No window."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus.gui import ribbon_model as rm
from nexus.gui.ribbon_model import categories, Feed, find, nav, RibbonState, step_value


def game(slug, played=None, **kw):
    return {'slug': slug, 'display_name': slug.title(), 'last_played': played, **kw}


GAMES = [game('alpha', '2026-11-01T10:00:00'), game('beta', '2026-11-05T10:00:00'),
         game('gamma')]
SWITCH = ((False, "Off"), (True, "On"))
SETTINGS = [('theatre_layout', "Layout", (('tabs', "Tabs"), ('ribbon', "Ribbon")), 'ribbon',
             True),
            ('sounds', "Menu sounds", SWITCH, False, True),
            ('volume', "Volume", tuple((v, f"{v}%") for v in range(0, 101, 10)), 50, False)]


def feed(**kw):
    base = dict(games=GAMES, library=sorted(GAMES, key=lambda g: g['slug']),
                lib_label="Installed  ·  A to Z", settings=SETTINGS,
                fact=lambda g: f"facts of {g['slug']}")
    return Feed(**{**base, **kw})


def cat(cats, key):
    return next(c for c in cats if c.key == key)


class Categories(unittest.TestCase):
    def test_every_category_with_both_gates_on(self):
        cats = categories(feed())
        self.assertEqual([c.key for c in cats], ['home', 'library', 'discover', 'downloads',
                                                 'settings'])
        self.assertEqual([c.title for c in cats], ["Home", "Library", "Discover", "Downloads",
                                                   "Settings"])
        self.assertEqual([it.primary for it in cat(cats, 'discover').items], ['tab:discover'])
        self.assertEqual([it.primary for it in cat(cats, 'downloads').items], ['tab:downloads'])

    def test_discover_and_downloads_follow_the_tab_gates(self):
        """The same gates as Theatre's tabs (sync_tabs): the Windows feature
        set has no repack Downloads unless something is downloading, and no
        Discover without News and Game-db."""
        for discover, downloads in ((False, True), (True, False), (False, False)):
            keys = [c.key for c in categories(feed(discover=discover, downloads=downloads))]
            self.assertEqual('discover' in keys, discover)
            self.assertEqual('downloads' in keys, downloads)
            self.assertEqual((keys[0], keys[1], keys[-1]), ('home', 'library', 'settings'))

    def test_home_recent_first_without_the_running_game_twice(self):
        cats = categories(feed())
        home = cat(cats, 'home')
        self.assertEqual([it.key for it in home.items], ['game:beta', 'game:alpha'])
        self.assertFalse(home.live)
        self.assertEqual(home.items[0].sub, "facts of beta")
        run = dict(GAMES[0], playing=True)
        home = cat(categories(feed(playing=run, playing_kicker="NOW PLAYING  ·  4 MIN")), 'home')
        self.assertTrue(home.live)
        self.assertEqual([it.key for it in home.items], ['game:alpha', 'game:beta'])
        self.assertEqual((home.items[0].kicker, home.items[0].primary),
                         ("NOW PLAYING  ·  4 MIN", 'resume'))
        starting = dict(GAMES[0], launching=True)
        home = cat(categories(feed(playing=starting)), 'home')
        self.assertEqual(home.items[0].primary, 'open')  # nothing to go back to yet
        self.assertFalse(home.live)                      # no live dot until it runs

    def test_home_week_and_friends(self):
        friends = [{'key': 'friend:7', 'title': "Seven", 'sub': "Ana · 1 h", 'slug': None}]
        home = cat(categories(feed(week={'sub': "6 h", 'slug': 'beta'}, friends=friends)), 'home')
        self.assertEqual([it.key for it in home.items][-2:], ['card:week', 'friend:7'])
        self.assertEqual(home.items[-2].kind, 'card')
        self.assertEqual(home.items[-1].sub, "Ana · 1 h")

    def test_jump_back_in_is_capped(self):
        many = [game(f"g{i:02}", f"2026-10-{i + 1:02}T10:00:00") for i in range(20)]
        home = cat(categories(feed(games=many, library=many)), 'home')
        self.assertEqual(len(home.items), rm.HOME_PLAYED)
        self.assertEqual(home.items[0].slug, 'g19')

    def test_library_items_label_and_a_plays(self):
        lib = cat(categories(feed(a_plays=True)), 'library')
        self.assertEqual([it.slug for it in lib.items], ['alpha', 'beta', 'gamma'])
        self.assertEqual(lib.sub, "Installed  ·  A to Z  ·  3 games")
        self.assertEqual({it.primary for it in lib.items}, {'play'})
        one = cat(categories(feed(library=[GAMES[0]])), 'library')
        self.assertTrue(one.sub.endswith("1 game"))

    def test_by_collection_is_flat_with_the_collection_as_kicker(self):
        groups = [("Couch", [GAMES[0], GAMES[1]]), ("Short", [GAMES[0]])]
        lib = cat(categories(feed(lib_groups=groups)), 'library')
        self.assertEqual([it.kicker for it in lib.items], ["COUCH", "COUCH", "SHORT"])
        self.assertEqual(len({it.key for it in lib.items}), 3)          # keys stay unique
        self.assertEqual(lib.sub, "Installed  ·  A to Z  ·  2 games")    # games, not entries
        self.assertEqual(find([lib], 'library', 'game:alpha'), 0)

    def test_settings_items_follow_the_rows_and_end_with_all_settings(self):
        st = cat(categories(feed()), 'settings')
        self.assertEqual([it.key for it in st.items],
                         ['set:theatre_layout', 'set:sounds', 'set:volume',
                          'action:all_settings'])
        layout = st.items[0]
        self.assertEqual((layout.kind, layout.primary, layout.value_text, layout.glyph),
                         ('setting', 'step', "Ribbon", 'grid'))
        self.assertEqual(st.items[2].primary, 'none')                   # disabled: greyed
        self.assertEqual(st.items[-1].primary, 'all_settings')

    def test_settings_match_theatre_settings_rows(self):
        from nexus.gui.theatre_settings import ROWS
        rows = [(k, t, ch, ch[0][0], True) for _g, k, t, ch in ROWS]
        st = cat(categories(feed(settings=rows)), 'settings')
        self.assertEqual([it.key[4:] for it in st.items[:-1]], [k for _g, k, _t, _c in ROWS])
        self.assertEqual([it.title for it in st.items[:-1]], [t for _g, _k, t, _c in ROWS])
        self.assertIn('theatre_layout', [k for _g, k, _t, _c in ROWS])
        self.assertIn('ribbon_motion', [k for _g, k, _t, _c in ROWS])
        self.assertEqual(set(rm.SETTING_GLYPH), {k for _g, k, _t, _c in ROWS})


class EmptyStates(unittest.TestCase):
    def test_no_games(self):
        cats = categories(feed(games=[], library=[]))
        for key in ('home', 'library'):
            items = cat(cats, key).items
            self.assertEqual(len(items), 1)
            self.assertEqual((items[0].kind, items[0].title, items[0].primary),
                             ('empty', "No games yet", 'all_settings'))

    def test_an_empty_filter_points_at_sort_and_filter(self):
        lib = cat(categories(feed(library=[], lib_show_label="Not installed")), 'library')
        it = lib.items[0]
        self.assertEqual((it.kind, it.title, it.primary),
                         ('empty', "No games under Not installed", 'sheet'))
        self.assertEqual(it.sub, "Pick another filter in Sort & filter.")
        self.assertTrue(lib.sub.endswith("0 games"))

    def test_downloads_line(self):
        dl = cat(categories(feed(downloads_sub="2 downloading")), 'downloads')
        self.assertEqual(dl.items[0].sub, "2 downloading")
        self.assertEqual(cat(categories(feed()), 'downloads').items[0].sub, "Nothing downloading.")


class Nav(unittest.TestCase):
    def setUp(self):
        self.cats = categories(feed())

    def test_left_right_stop_at_the_ends(self):
        s = nav(RibbonState(), self.cats, 'left')
        self.assertEqual(s.cat, 'home')
        for _ in range(10):
            s = nav(s, self.cats, 'right')
        self.assertEqual(s.cat, 'settings')

    def test_next_prev_wrap(self):
        s = nav(RibbonState(), self.cats, 'prev')
        self.assertEqual(s.cat, 'settings')
        self.assertEqual(nav(s, self.cats, 'next').cat, 'home')

    def test_up_down_stop_and_each_category_remembers(self):
        s = RibbonState('library')
        s = nav(s, self.cats, 'up')
        self.assertEqual(s.focus['library'], 0)
        for _ in range(5):
            s = nav(s, self.cats, 'down')
        self.assertEqual(s.focus['library'], 2)                 # three games: stops
        s = nav(nav(s, self.cats, 'right'), self.cats, 'left')   # away and back
        self.assertEqual((s.cat, s.focus['library']), ('library', 2))
        self.assertEqual(rm.current(s, self.cats).slug, 'gamma')

    def test_home(self):
        s = RibbonState('settings', {'home': 1, 'settings': 2})
        s = nav(s, self.cats, 'home')
        self.assertEqual((s.cat, s.focus['home'], s.focus['settings']), ('home', 0, 2))

    def test_find(self):
        self.assertEqual(find(self.cats, 'library', 'game:beta'), 1)
        self.assertIsNone(find(self.cats, 'library', 'game:nope'))
        self.assertIsNone(find(self.cats, 'nowhere', 'game:beta'))


class StepValue(unittest.TestCase):
    def test_wraps_or_stops(self):
        st = cat(categories(feed()), 'settings')
        layout, volume = st.items[0], st.items[2]
        self.assertEqual(step_value(layout), 'tabs')
        self.assertEqual(step_value(layout, 1, wrap=False), 'ribbon')     # the end
        self.assertEqual(step_value(volume, -1), 40)
        self.assertEqual(step_value(rm.Item('set:x', 'setting', "X", value=100,
                                            choices=volume.choices)), 0)    # wraps round
        odd = rm.Item('set:x', 'setting', "X", value='junk', choices=SWITCH)
        self.assertEqual(step_value(odd), True)                 # unknown: from the first


if __name__ == '__main__':
    unittest.main()
