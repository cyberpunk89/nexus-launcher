"""Library categories (nexus/categories.py): the keys, the groups in their
fixed order, the Windows set, the wording, and what a category page shows
(stats, lead game, covers, cuts, More like this, sort). Pure: no Qt."""

import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import categories as C, smart  # noqa: E402

NOW = datetime(2026, 11, 10, 12, 0)


def game(slug, minutes=0, days_ago=None, source='steam', **kw):
    g = {'slug': slug, 'display_name': slug.title(), 'playtime_minutes': minutes,
         'last_played': (NOW - timedelta(days=days_ago)).isoformat(timespec='seconds')
         if days_ago is not None else None, 'installed': True, 'source': source,
         'favorite': False, 'collections': []}
    g.update(kw)
    return g


def fact(main=None, genres=(), modes=('single',)):
    return {'length': {'main': main} if main else None, 'genres': list(genres),
            'modes': list(modes), 'status': 'released'}


def keys(groups):
    return [c.key for g in groups for c in g.cats]


class Keys(unittest.TestCase):
    def test_new_keys_never_collide_with_smart_prefixes(self):
        for key in (C.FAV, C.COL + 'Weekend', C.SRC + 'steam'):
            self.assertFalse(key.startswith(smart.KEY_PREFIXES), key)
        self.assertEqual(C.kind_of('fav'), 'fav')
        self.assertEqual(C.kind_of('col:Weekend'), 'collection')
        self.assertEqual(C.kind_of('src:steam'), 'store')
        self.assertEqual(C.kind_of('smart:never'), 'smart')
        self.assertEqual(C.kind_of('genre:RPG'), 'genre')
        self.assertEqual(C.kind_of('user:a1b2'), 'shelf')
        for bad in ('friends:1:multi', 'col:', 'src:', 'genre:', 'nonsense', '', None, 3):
            self.assertIsNone(C.kind_of(bad), bad)
            self.assertFalse(C.is_key(bad), bad)
        self.assertTrue(C.is_key('fav'))

    def test_collection_name_with_colons(self):
        key = C.COL + 'RPG: the long ones'
        self.assertEqual(C.kind_of(key), 'collection')
        self.assertEqual(C.collection_name(key), 'RPG: the long ones')
        self.assertIsNone(C.collection_name('fav'))

    def test_from_chip(self):
        self.assertEqual(C.from_chip('favorite'), 'fav')
        self.assertEqual(C.from_chip('col:A: b'), 'col:A: b')
        for store in ('nexus', 'steam', 'heroic', 'epic'):
            self.assertEqual(C.from_chip(store), 'src:' + store)
        for chip in ('all', 'owned', 'friends', '', None, 'bogus'):
            self.assertIsNone(C.from_chip(chip), chip)


def library():
    """A small library: three RPGs (one a favourite in a collection), a
    racing game, a Repacks game, a Heroic game."""
    games = [game('rpg1', 600, 2, favorite=True, collections=['Couch: co-op']),
             game('rpg2', 120, 40, disk_bytes=10 * 2**30),
             game('rpg3', 0, None, added_at=(NOW - timedelta(days=1)).isoformat()),
             game('race', 30, 1, collections=['Couch: co-op']),
             game('pack', 0, None, source='nexus'),
             game('wine', 45, 3, source='heroic')]
    facts = {'rpg1': fact(10, ('RPG',), ('single', 'coop')), 'rpg2': fact(20, ('RPG',)),
             'rpg3': fact(5, ('RPG', 'Indie')), 'race': fact(2, ('Racing',), ('coop',))}
    return games, facts


def prefs(**kw):
    base = {'collections': ('Couch: co-op', 'Empty one'), 'looks': {'Couch: co-op': {
        'icon': 'gamepad-2', 'color': 'gold'}}, 'shelves': (), 'repacks': True, 'heroic': True}
    base.update(kw)
    return C.PrefsView(**base)


SHELF = {'id': 'ab12', 'name': "Long ones", 'match': 'all',
         'rules': [{'field': 'playtime', 'op': 'atleast', 'value': 1}], 'removed': False}
EMPTY_SHELF = {'id': 'cd34', 'name': "Nothing yet", 'match': 'all',
               'rules': [{'field': 'playtime', 'op': 'atleast', 'value': 100}],
               'removed': False}


class Groups(unittest.TestCase):
    def groups(self, linux=True, **kw):
        games, facts = library()
        return C.categories(games, facts, prefs(**kw), linux=linux, now=NOW)

    def test_fixed_group_order(self):
        groups = self.groups(shelves=(SHELF,))
        self.assertEqual([g.key for g in groups], ['for_you', 'yours', 'genres', 'stores'])
        self.assertEqual([g.title for g in groups], ["For you", "Yours", "Genres", "Stores"])
        yours = [c.key for c in groups[1].cats]
        self.assertEqual(yours, ['fav', 'col:Couch: co-op', 'user:ab12'])
        genres = [c.key for c in groups[2].cats]
        self.assertEqual(genres, ['genre:RPG', 'genre:Racing', 'genre:Indie'])
        self.assertEqual([c.key for c in groups[3].cats], ['src:nexus', 'src:steam', 'src:heroic'])
        rules = [c.key for c in groups[0].cats]
        self.assertEqual(rules, [k for k in smart.rule_keys(True) if k in rules])
        col = C.lookup(groups, 'col:Couch: co-op')
        self.assertEqual((col.kind, col.color), ('collection', 'gold'))
        self.assertEqual([g['slug'] for g in col.games], ['race', 'rpg1'])   # last played

    def test_one_game_makes_a_page_empty_key_is_none(self):
        games, facts = library()
        cat = C.category('genre:Indie', games, facts, prefs(), now=NOW)
        self.assertEqual([g['slug'] for g in cat.games], ['rpg3'])
        self.assertEqual((cat.kind, cat.title), ('genre', 'Indie'))
        for key in ('genre:Horror', 'col:Empty one', 'col:Missing', 'src:epic', 'smart:almost',
                    'user:zz99', 'friends:1', 'bogus'):
            self.assertIsNone(C.category(key, games, facts, prefs(), now=NOW), key)
        self.assertIsNone(C.category('genre:RPG', games, {}, prefs(), now=NOW))  # no facts yet
        kept = C.category('genre:RPG', games, {}, prefs(), now=NOW, keep_empty=True)
        self.assertEqual((kept.title, kept.games), ('RPG', ()))     # an open page keeps it
        self.assertIsNone(C.category('col:Missing', games, facts, prefs(), keep_empty=True))

    def test_empty_user_shelf_kept(self):
        games, facts = library()
        cat = C.category('user:cd34', games, facts, prefs(shelves=(EMPTY_SHELF,)), now=NOW)
        self.assertEqual((cat.kind, cat.title, cat.games), ('shelf', "Nothing yet", ()))
        self.assertIn('user:cd34', keys(self.groups(shelves=(EMPTY_SHELF,))))
        gone = dict(EMPTY_SHELF, removed=True)
        self.assertIsNone(C.category('user:cd34', games, facts, prefs(shelves=(gone,))))

    def test_windows_set_has_no_linux_rule_or_heroic_store(self):
        games, facts = library()
        games[0]['protondb'] = games[1]['protondb'] = 'platinum'
        lin = keys(C.categories(games, facts, prefs(), linux=True, now=NOW))
        self.assertIn('smart:linux', lin)
        self.assertIn('src:heroic', lin)
        win = keys(C.categories(games, facts, prefs(heroic=False), linux=False, now=NOW))
        self.assertNotIn('smart:linux', win)
        self.assertNotIn('src:heroic', win)
        self.assertIsNone(C.category('smart:linux', games, facts, prefs(), linux=False))
        self.assertIsNone(C.category('src:heroic', games, facts, prefs(heroic=False)))

    def test_no_repack_wording(self):
        groups = self.groups(shelves=(SHELF,))
        for cat in (c for g in groups for c in g.cats):
            self.assertNotIn('repack', cat.tip.lower(), cat.key)
            if cat.key != 'src:nexus':
                self.assertNotIn('repack', cat.title.lower(), cat.key)
        self.assertEqual(C.lookup(groups, 'src:nexus').title, "Repacks")   # the existing label
        for g in groups:
            self.assertNotIn('repack', g.title.lower())

    def test_repacks_off_drops_the_store(self):
        self.assertNotIn('src:nexus', keys(self.groups(repacks=False)))
        games, facts = library()
        self.assertIsNone(C.category('src:nexus', games, facts, prefs(repacks=False)))

    def test_no_qt_import(self):
        code = ("import sys; import nexus.categories; "
                "bad = [m for m in sys.modules if m.startswith(('PyQt6', 'nexus.gui'))]; "
                "print(bad); sys.exit(1 if bad else 0)")
        r = subprocess.run([sys.executable, '-c', code], cwd=REPO, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class Page(unittest.TestCase):
    def setUp(self):
        self.games, self.facts = library()
        self.groups = C.categories(self.games, self.facts, prefs(shelves=(SHELF,)),
                                   linux=True, now=NOW)

    def test_stats_and_lead(self):
        cat = C.lookup(self.groups, 'genre:RPG')
        st = C.stats(cat, self.facts)
        self.assertEqual((st.count, st.minutes, st.finished, st.bytes), (3, 720, 1, 10 * 2**30))
        self.assertEqual(C.lead(cat)['slug'], 'rpg1')
        never = C.category('smart:never', self.games, self.facts, prefs(), now=NOW)
        self.assertIsNone(C.lead(never))

    def test_covers_by_last_played(self):
        cat = C.lookup(self.groups, 'src:steam')
        self.assertEqual([g['slug'] for g in C.covers(cat)], ['race', 'rpg1', 'rpg2', 'rpg3'])
        self.assertEqual(len(C.covers(cat, 2)), 2)

    def test_cuts_skip_self_and_whole(self):
        cat = C.lookup(self.groups, 'genre:RPG')
        cuts = C.cuts(cat, self.facts, linux=True, now=NOW)
        by = {c.key: c.count for c in cuts}
        self.assertEqual(by.get('smart:unfinished'), 1)
        self.assertEqual(by.get('smart:never'), 1)
        self.assertNotIn('smart:continue', by)              # rpg1 is through its story
        self.assertEqual(by.get('smart:coop'), 1)
        self.assertNotIn('smart:short', by)                  # none under 5 h in it
        for c in cuts:
            self.assertTrue(c.title and c.glyph)
            self.assertLess(c.count, len(cat.games))
        coop = C.lookup(self.groups, 'smart:coop')
        self.assertNotIn('smart:coop', [c.key for c in C.cuts(coop, self.facts, True, NOW)])
        racing = C.lookup(self.groups, 'genre:Racing')         # one game: every cut is whole
        self.assertEqual(C.cuts(racing, self.facts, True, NOW), [])

    def test_related_overlap_other_kinds_first(self):
        rpg = C.lookup(self.groups, 'genre:RPG')
        rel = [c.key for c in C.related(rpg, self.groups, n=10)]
        self.assertNotIn('genre:RPG', rel)
        self.assertFalse([k for k in rel if k.startswith(C.SRC)])        # stores left out
        self.assertEqual(rel[0], 'user:ab12')            # rpg1, rpg2: 2 of 3 games
        self.assertNotIn('genre:Racing', rel)             # nothing in common
        self.assertEqual(len(C.related(rpg, self.groups)), 4)
        # Co-op (rpg1, race): the collection holds both; Racing and Short
        # games both share one game of two, and the other kind goes first.
        coop = C.lookup(self.groups, 'smart:coop')
        rel = [c.key for c in C.related(coop, self.groups, n=10)]
        self.assertEqual(rel[0], 'col:Couch: co-op')
        self.assertLess(rel.index('genre:Racing'), rel.index('smart:short'))

    def test_sort_games_matches_library_sort(self):
        gs = self.games
        self.assertEqual([g['slug'] for g in C.sort_games(gs, 'name')],
                         ['pack', 'race', 'rpg1', 'rpg2', 'rpg3', 'wine'])
        self.assertEqual([g['slug'] for g in C.sort_games(gs, 'played')][:3],
                         ['race', 'rpg1', 'wine'])
        self.assertEqual([g['slug'] for g in C.sort_games(gs, 'most')][:2], ['rpg1', 'rpg2'])
        self.assertEqual(C.sort_games(gs, 'size')[0]['slug'], 'rpg2')
        self.assertEqual(C.sort_games(gs, 'added')[0]['slug'], 'rpg3')
        self.assertEqual([g['slug'] for g in C.sort_games(gs, 'bogus')],
                         [g['slug'] for g in C.sort_games(gs, 'name')])
        self.assertEqual([g['slug'] for g in gs][0], 'rpg1')            # a copy: input kept


class CoverTag(unittest.TestCase):
    """w-113: the line on a cover in a smart row (cover_tag)."""
    def test_almost_left(self):
        g = game('a', 480)                               # 8 h of 10
        self.assertEqual(C.cover_tag('smart:almost', g, fact(10)), ("2 h left", 0.8))
        g = game('b', 570)                               # 9.5 of 10: never "0 h left"
        text, frac = C.cover_tag('smart:almost', g, fact(10))
        self.assertEqual(text, "1 h left")
        self.assertAlmostEqual(frac, 0.95)
        g = game('c', 1200)                              # past the story: a full bar
        self.assertEqual(C.cover_tag('smart:almost', g, fact(10))[1], 1.0)

    def test_never_story(self):
        self.assertEqual(C.cover_tag('smart:never', game('a'), fact(12.4)), ("~12 h story", None))

    def test_no_length_or_online_no_tag(self):
        self.assertIsNone(C.cover_tag('smart:almost', game('a', 60), fact()))
        self.assertIsNone(C.cover_tag('smart:never', game('a'), None))
        self.assertIsNone(C.cover_tag('smart:never', game('a'), fact(10, modes=('online',))))
        self.assertIsNone(C.cover_tag('smart:continue', game('a', 60), fact(10)))
        self.assertIsNone(C.cover_tag(None, game('a', 60), fact(10)))


class StudiosFor(unittest.TestCase):
    """w-113: studios_for reads a group with key 'studios' (w-112 adds it)."""
    def test_none_without_the_group(self):
        games, facts = library()
        groups = C.categories(games, facts, prefs(), linux=True, now=NOW)
        rpg = C.lookup(groups, 'genre:RPG')
        self.assertEqual(C.studios_for(rpg, groups), [])

    def test_ranked_by_shared_games(self):
        games, facts = library()
        groups = C.categories(games, facts, prefs(), linux=True, now=NOW)
        rpg = C.lookup(groups, 'genre:RPG')
        by = {g['slug']: g for g in games}

        def studio(key, *slugs):
            return C.Category(key, 'studio', key, '', '*', None, tuple(by[s] for s in slugs))
        one = studio('dev:One', 'rpg1')
        two = studio('dev:Two', 'rpg1', 'rpg2')
        none = studio('dev:None', 'race')
        extra = C.Group('studios', "Studios", (one, two, none))
        got = C.studios_for(rpg, list(groups) + [extra])
        self.assertEqual([(c.key, n) for c, n in got], [('dev:Two', 2), ('dev:One', 1)])
        self.assertEqual(len(C.studios_for(rpg, list(groups) + [extra], n=1)), 1)
        self.assertEqual(C.studios_for(two, list(groups) + [extra]), [(one, 1)])   # not itself


def cat(key, n):
    """A category of `n` made-up games (browse() looks only at keys,
    kinds and counts)."""
    return C.Category(key, C.kind_of(key), key, '', '*', None,
                      tuple({'slug': f'{key}-{i}'} for i in range(n)))


def group(gkey, *cats):
    return C.Group(gkey, dict(C.GROUPS)[gkey], tuple(cats))


def rules(n, games=5):
    keys = smart.rule_keys(True)
    return [cat(k, games) for k in keys[:n]]


def genres(names, games=5):
    return [cat(f'genre:{n}', games) for n in names]


GENRES = ('RPG', 'Action', 'Adventure', 'Indie', 'Strategy', 'Racing', 'Puzzle', 'Horror')


class Browse(unittest.TestCase):
    """browse(): what Browse shows of the groups (the hub shows them all)."""

    def full(self):
        return [group('for_you', *rules(8)),
                group('yours', cat('fav', 2), cat('col:A: b', 1), cat('user:ab12', 0)),
                group('genres', *genres(GENRES)),
                group('stores', cat('src:nexus', 4), cat('src:steam', 9))]

    def test_caps_and_more(self):
        out = C.browse(self.full())
        self.assertEqual([g.key for g in out], ['for_you', 'yours', 'genres', 'stores'])
        by = {g.key: g for g in out}
        self.assertEqual(len(by['for_you'].cats), C.CAPS['for_you'])
        self.assertEqual(by['for_you'].more, 2)
        self.assertEqual([c.key for c in by['genres'].cats],
                         [f'genre:{n}' for n in GENRES[:6]])
        self.assertEqual(by['genres'].more, 2)
        self.assertEqual((len(by['yours'].cats), by['yours'].more), (3, 0))   # uncapped
        self.assertEqual((len(by['stores'].cats), by['stores'].more), (2, 0))
        self.assertEqual(C.CAPS, {'for_you': 6, 'genres': 6, 'studios': 6})
        none = C.browse(self.full(), caps={})
        self.assertEqual(len(none[0].cats), 8)

    def test_hidden_leaves_browse_only(self):
        groups = self.full()
        before = [[c.key for c in g.cats] for g in groups]
        out = C.browse(groups, hidden={'fav', 'src:steam', 'genre:RPG'})
        shown = [c.key for g in out for c in g.cats]
        for k in ('fav', 'src:steam', 'genre:RPG'):
            self.assertNotIn(k, shown)
        self.assertEqual([[c.key for c in g.cats] for g in groups], before)   # untouched
        self.assertEqual(C.lookup(groups, 'fav').key, 'fav')        # the hub still has it

    def test_hidden_row_lets_the_next_in(self):
        keys = smart.rule_keys(True)
        out = C.browse(self.full(), hidden={keys[0]})
        mine = [c.key for c in out[0].cats]
        self.assertEqual(mine, list(keys[1:7]))
        self.assertEqual(out[0].more, 1)

    def test_min_games_rules_and_genres_only(self):
        few = smart.MIN_GAMES - 1
        groups = [group('for_you', cat('smart:continue', few), cat('smart:never', 5)),
                  group('yours', cat('fav', 1), cat('col:One', 1)),
                  group('genres', cat('genre:RPG', few), cat('genre:Indie', smart.MIN_GAMES)),
                  group('stores', cat('src:epic', 1))]
        shown = [c.key for g in C.browse(groups) for c in g.cats]
        self.assertEqual(shown, ['smart:never', 'fav', 'col:One', 'genre:Indie', 'src:epic'])

    def test_empty_user_shelf_stays(self):
        out = C.browse([group('yours', cat('user:cd34', 0))])
        self.assertEqual([c.key for c in out[0].cats], ['user:cd34'])

    def test_empty_groups_dropped(self):
        groups = [group('for_you', cat('smart:continue', 1)),
                  group('yours', cat('fav', 2)),
                  group('genres', cat('genre:RPG', 5))]
        out = C.browse(groups, hidden={'genre:RPG'})
        self.assertEqual([g.key for g in out], ['yours'])
        self.assertEqual(C.browse([]), [])

    def test_hub_key_is_not_a_category(self):
        self.assertEqual(C.HUB, 'all')
        self.assertFalse(C.is_key(C.HUB))
        self.assertIsNone(C.from_chip(C.HUB))
        self.assertIsNone(C.category(C.HUB, [], {}, prefs()))
        self.assertTrue(C.is_page_key(C.HUB))
        for k in ('fav', 'genre:RPG', 'src:steam', 'user:ab12'):
            self.assertTrue(C.is_page_key(k), k)
        for k in ('friends:1', 'bogus', None, ''):
            self.assertFalse(C.is_page_key(k), k)
        for gkey, _t in C.GROUPS:
            self.assertTrue(C.GROUP_GLYPH[gkey])
        self.assertEqual(C.Group('x', 'X').more, 0)


class Pins(unittest.TestCase):
    """Pinned categories (category_pins): at most PIN_MAX, any category key,
    the order you pinned them in; one missing from the library stays saved."""

    def test_clean_drops_bad_and_dupes_caps_six(self):
        raw = ['fav', 'fav', 'bogus', None, 3, 'friends:1', 'col:A: b', 'genre:RPG',
               'src:steam', 'smart:never', 'user:ab12', 'genre:Indie', 'src:epic']
        out = C.clean_pins(raw)
        self.assertEqual(C.PIN_MAX, 6)
        self.assertEqual(out, ['fav', 'col:A: b', 'genre:RPG', 'src:steam', 'smart:never',
                               'user:ab12'])
        for bad in (None, 'fav', {'fav': 1}, 7):
            self.assertEqual(C.clean_pins(bad), [], bad)
        self.assertEqual(C.clean_pins(('fav', C.HUB)), ['fav'])      # the hub isn't one

    def test_toggle_adds_removes_and_refuses_seventh(self):
        pins = C.toggle_pin([], 'fav')
        self.assertEqual(pins, ['fav'])
        pins = C.toggle_pin(pins, 'genre:RPG')
        self.assertEqual(pins, ['fav', 'genre:RPG'])
        self.assertEqual(C.toggle_pin(pins, 'fav'), ['genre:RPG'])
        self.assertEqual(pins, ['fav', 'genre:RPG'])                    # a new list
        full = [f'genre:{n}' for n in GENRES[:6]]
        self.assertEqual(C.toggle_pin(full, 'fav'), full)                # full: unchanged
        self.assertEqual(C.toggle_pin(full, 'genre:RPG'), full[1:])      # unpin still works
        self.assertEqual(C.toggle_pin(['fav'], 'bogus'), ['fav'])

    def test_pin_state_full(self):
        full = [f'genre:{n}' for n in GENRES[:6]]
        self.assertEqual(C.pin_state([], 'fav'), 'off')
        self.assertEqual(C.pin_state(['fav'], 'fav'), 'on')
        self.assertEqual(C.pin_state(full, 'fav'), 'full')
        self.assertEqual(C.pin_state(full, 'genre:RPG'), 'on')

    def test_only_live_pins_count_toward_full(self):
        """A pin with no category now (a deleted collection, a store that's
        off) stays saved but doesn't take a place: Pin isn't greyed out,
        and pinning at six drops such pins first."""
        pins = ['col:Gone', 'genre:RPG', 'genre:Action', 'src:heroic', 'genre:Indie',
                'genre:Racing']
        live = set(pins) - {'col:Gone', 'src:heroic'} | {'fav'}
        self.assertEqual(C.pin_state(pins, 'fav', live), 'off')
        self.assertEqual(C.pin_state(pins, 'fav'), 'full')         # no live set: all count
        out = C.toggle_pin(pins, 'fav', live)
        self.assertEqual(out, ['genre:RPG', 'genre:Action', 'src:heroic', 'genre:Indie',
                               'genre:Racing', 'fav'])              # the first dead one went
        all_live = set(pins) | {'fav'}
        self.assertEqual(C.toggle_pin(pins, 'fav', all_live), pins)  # full of live ones
        self.assertEqual(C.pin_state(pins, 'fav', all_live), 'full')

    def test_pinned_keeps_empty_skips_missing_keeps_order(self):
        """pinned(): each pin as a category over the games, in pin order;
        one with no games now is kept (its page says so), one that isn't a
        category here (a deleted collection, Heroic off) is skipped."""
        games, facts = library()
        out = C.pinned(['genre:Indie', 'col:Gone', 'fav', 'col:Empty one', 'src:heroic',
                        'genre:Horror', 'genre:Gone'], games, facts, prefs(heroic=False),
                       now=NOW)
        self.assertEqual([c.key for c in out], ['genre:Indie', 'fav', 'col:Empty one',
                                                'genre:Horror'])
        self.assertEqual(len(out[2].games), 0)
        self.assertEqual(len(out[0].games), 1)
        self.assertEqual(C.pinned([], games, facts, prefs()), [])

    def test_hidden_row_still_pinnable(self):
        """A row hidden from Browse (or past its cap) is still a pin."""
        games, facts = library()
        out = C.pinned(['genre:RPG', 'smart:never'], games, facts, prefs(), now=NOW)
        self.assertEqual([c.key for c in out], ['genre:RPG', 'smart:never'])
        shown = keys(C.browse(C.categories(games, facts, prefs(), now=NOW),
                              hidden={'genre:RPG'}))
        self.assertNotIn('genre:RPG', shown)

if __name__ == '__main__':
    unittest.main()


# ---- studios (w-112) ----
def studio_library():
    """Five games with studio credits: Capcom spelled three ways (developer),
    Warner Bros. under both names and Monolith with and without Inc.
    (publisher / developer), one studio with a single game."""
    games = [game('re4', 900, 1), game('dmc', 300, 5), game('mhw', 60, 30),
             game('sow', 400, 2), game('som', 100, 50), game('lone', 10, 3)]
    facts = {'re4': dict(fact(), developers=['CAPCOM Co., Ltd.'], publishers=['CAPCOM Co., Ltd.']),
             'dmc': dict(fact(), developers=['Capcom'], publishers=['Capcom']),
             'mhw': dict(fact(), developers=['CAPCOM'], publishers=None),   # Steam v1: no publishers
             'sow': dict(fact(), developers=['Monolith Productions, Inc.'],
                         publishers=['Warner Bros. Interactive Entertainment']),
             'som': dict(fact(), developers=['Monolith Productions'],
                         publishers=['Warner Bros. Games']),
             'lone': dict(fact(), developers=['Tiny Studio LLC'], publishers=['Big Pub S.A.'])}
    return games, facts


class Studios(unittest.TestCase):
    def test_studio_name_merges(self):
        for raw in ('CAPCOM Co., Ltd.', 'Capcom', 'CAPCOM', 'capcom co., ltd'):
            self.assertEqual(C.studio_name(raw), 'Capcom', raw)
        for raw in ('Warner Bros. Games', 'Warner Bros. Interactive Entertainment',
                    'WB Games'):
            self.assertEqual(C.studio_name(raw), 'Warner Bros. Games', raw)
        for raw in ('Monolith Productions', 'Monolith Productions, Inc.'):
            self.assertEqual(C.studio_name(raw), 'Monolith Productions', raw)
        self.assertEqual(C.studio_name('11 bit studios S.A.'), '11 bit studios')
        self.assertEqual(C.studio_name('Tiny Studio LLC'), 'Tiny Studio')
        self.assertEqual(C.studio_name('  SEGA  '), 'SEGA')
        self.assertEqual(C.studio_name('Warner Bros.'), 'Warner Bros.')   # Bros. is no suffix
        self.assertEqual(C.studio_name('Co.'), 'Co.')                     # never stripped to nothing
        for bad in ('', '   ', None, 3):
            self.assertEqual(C.studio_name(bad), '', bad)
        self.assertEqual(C.studio_id('CAPCOM Co., Ltd.'), C.studio_id('capcom'))
        self.assertEqual(C.studio_id('Ubisoft'), C.studio_id('UBISOFT'))

    def test_keys_and_kinds(self):
        dev, pub = C.studio_key('dev', 'CAPCOM Co., Ltd.'), C.studio_key('pub', 'Capcom')
        self.assertEqual((dev, pub), ('dev:capcom', 'pub:capcom'))
        self.assertIsNone(C.studio_key('dev', '  '))
        self.assertIsNone(C.studio_key('bogus', 'Capcom'))
        for key in (dev, pub, C.DEV + 'a: b'):
            self.assertFalse(key.startswith(smart.KEY_PREFIXES), key)
            self.assertTrue(C.is_key(key), key)
        self.assertEqual((C.kind_of(dev), C.kind_of(pub)), ('developer', 'publisher'))
        self.assertEqual(C.STUDIO_KINDS, ('developer', 'publisher'))
        for bad in ('dev:', 'pub:'):
            self.assertIsNone(C.kind_of(bad), bad)
        self.assertEqual(C.studio_switch(dev), pub)
        self.assertEqual(C.studio_switch(pub), dev)
        self.assertIsNone(C.studio_switch('genre:RPG'))
        self.assertEqual(C.STUDIO_DEFAULT, 'dev')

    def test_group_after_genres_developer_by_default(self):
        games, facts = studio_library()
        facts['re4']['genres'] = facts['dmc']['genres'] = ['Action']
        groups = C.categories(games, facts, prefs(), linux=True, now=NOW)
        order = [g.key for g in groups]
        self.assertLess(order.index('genres'), order.index('studios'))
        self.assertEqual(order.index('studios') + 1, order.index('stores'))
        studios = next(g for g in groups if g.key == 'studios')
        self.assertEqual(studios.title, "Studios")
        # Developers, most games first; one-game studios left out.
        self.assertEqual([c.key for c in studios.cats], ['dev:capcom', 'dev:monolith productions'])
        cap = studios.cats[0]
        self.assertEqual((cap.kind, cap.title), ('developer', 'Capcom'))
        self.assertEqual([g['slug'] for g in cap.games], ['re4', 'dmc', 'mhw'])   # last played
        self.assertIn('Capcom', cap.tip)
        pubs = C.categories(games, facts, prefs(), linux=True, now=NOW, studios='pub')
        pkeys = [c.key for g in pubs if g.key == 'studios' for c in g.cats]
        self.assertEqual(pkeys, ['pub:capcom', 'pub:warner bros. games'])  # mhw has no publisher
        self.assertEqual(C.lookup(pubs, 'pub:warner bros. games').title, 'Warner Bros. Games')

    def test_two_games_for_a_page_min_games_for_browse(self):
        games, facts = studio_library()
        p = prefs()
        self.assertIsNone(C.category('dev:tiny studio', games, facts, p, now=NOW))
        kept = C.category('dev:tiny studio', games, facts, p, now=NOW, keep_empty=True)
        self.assertEqual(len(kept.games), 1)                       # an open page / a pin keeps it
        self.assertIsNone(C.category('dev:nobody', games, facts, p, now=NOW))
        mono = C.category('dev:monolith productions', games, facts, p, now=NOW)
        self.assertEqual(len(mono.games), 2)                       # 2 makes the hub and a page
        rows = keys(C.browse(C.categories(games, facts, p, now=NOW)))
        self.assertIn('dev:capcom', rows)                          # 3 = smart.MIN_GAMES
        self.assertNotIn('dev:monolith productions', rows)
        self.assertEqual(smart.MIN_GAMES, 3)

    def test_capped_like_genres(self):
        self.assertEqual(C.CAPS['studios'], C.CAPS['genres'])
        games, facts = [], {}
        for i in range(8):
            for j in range(3):
                slug = f's{i}g{j}'
                games.append(game(slug, 10, i + j))
                facts[slug] = dict(fact(), developers=[f'Studio {i}'], publishers=[])
        b = C.browse(C.categories(games, facts, prefs(), now=NOW))
        studios = next(g for g in b if g.key == 'studios')
        self.assertEqual((len(studios.cats), studios.more), (6, 2))

    def test_pins_work(self):
        games, facts = studio_library()
        pins = C.clean_pins(['dev:capcom', 'pub:capcom', 'dev:', 'dev:capcom'])
        self.assertEqual(pins, ['dev:capcom', 'pub:capcom'])
        self.assertEqual(C.toggle_pin([], 'dev:monolith productions'), ['dev:monolith productions'])
        got = C.pinned(pins + ['dev:gone studio'], games, facts, prefs(), now=NOW)
        self.assertEqual([c.key for c in got], ['dev:capcom', 'pub:capcom', 'dev:gone studio'])
        # Like a genre before the facts fill: the tab stays, its page says it is empty.
        self.assertEqual((got[2].title, got[2].games), ('gone studio', ()))

    def test_raw_names_for_merged_line(self):
        games, facts = studio_library()
        cat = C.category('dev:capcom', games, facts, prefs(), now=NOW)
        self.assertEqual(C.raw_names(cat, facts), ['CAPCOM', 'CAPCOM Co., Ltd.', 'Capcom'])

    def test_windows_set_and_wording(self):
        games, facts = studio_library()
        games.append(game('pk1', 0, None, source='nexus'))
        games.append(game('pk2', 0, None, source='nexus'))
        facts['pk1'] = dict(fact(), developers=['Capcom'])
        facts['pk2'] = dict(fact(), developers=['Capcom'])
        win = C.categories(games, facts, prefs(heroic=False), linux=False, now=NOW)
        self.assertIn('dev:capcom', keys(win))
        for g in win:
            self.assertNotIn('repack', g.title.lower())
        for cat in (c for g in win if g.key == 'studios' for c in g.cats):
            self.assertNotIn('repack', (cat.title + cat.tip).lower(), cat.key)
        self.assertEqual(len(C.lookup(win, 'dev:capcom').games), 5)  # any store counts
        self.assertNotIn(C.GROUP_GLYPH.get('studios'), (None, ''))

    def test_related_and_stats_on_a_studio(self):
        games, facts = studio_library()
        for s in ('re4', 'dmc', 'mhw'):
            facts[s]['genres'] = ['Action']
        groups = C.categories(games, facts, prefs(), now=NOW)
        cap = C.lookup(groups, 'dev:capcom')
        self.assertEqual(C.stats(cap, facts).count, 3)
        self.assertEqual(C.related(cap, groups)[0].key, 'genre:Action')


class StudioPage(unittest.TestCase):
    """w-114: what a studio's page shows (facts, years, other studios, the
    credit line on a game card)."""
    def lib(self):
        games, facts = studio_library()
        facts['re4']['year'], facts['dmc']['year'], facts['sow']['year'] = 2023, 2019, 2017
        return games, facts

    def test_facts_count_both_roles_and_years(self):
        games, facts = self.lib()
        cap = C.category('dev:capcom', games, facts, prefs(), now=NOW)
        sf = C.studio_facts(cap, games, facts)
        self.assertEqual((sf.developed, sf.published), (3, 2))   # mhw has no publisher
        self.assertEqual(sf.years, (2019, 2023))
        self.assertEqual(sf.names, ('CAPCOM', 'CAPCOM Co., Ltd.', 'Capcom'))
        pub = C.category('pub:capcom', games, facts, prefs(), now=NOW)
        self.assertEqual(C.studio_facts(pub, games, facts).developed, 3)
        mono = C.category('dev:monolith productions', games, facts, prefs(), now=NOW)
        sf = C.studio_facts(mono, games, facts)
        self.assertEqual((sf.developed, sf.published, sf.years), (2, 0, (2017, 2017)))
        facts['sow']['year'] = None
        self.assertEqual(C.studio_facts(mono, games, facts).years, ())

    def test_years_group_and_skip_unknown(self):
        games, facts = self.lib()
        facts['dmc']['year'] = 2023
        cap = C.category('dev:capcom', games, facts, prefs(), now=NOW)
        got = [(y, [g['slug'] for g in gs]) for y, gs in C.studio_years(cap, facts)]
        self.assertEqual(got, [(2023, ['re4', 'dmc'])])           # most played first; mhw: no year
        facts['mhw']['year'] = 2018
        facts['dmc']['year'] = True                                # no year either
        got = [(y, [g['slug'] for g in gs]) for y, gs in C.studio_years(cap, facts)]
        self.assertEqual(got, [(2018, ['mhw']), (2023, ['re4'])])
        self.assertEqual(C.studio_years(C.Category('x', 'genre', 'x', '', '*', None), facts), [])

    def test_others_ranked_by_minutes_min2_not_self(self):
        games, facts = self.lib()
        games.append(game('mono3', 5000, 9))
        facts['mono3'] = dict(fact(), developers=['Monolith Productions'], publishers=[])
        cap = C.category('dev:capcom', games, facts, prefs(), now=NOW)
        got = [(c.key, m) for c, m in C.other_studios(cap, games, facts)]
        self.assertEqual(got, [('dev:monolith productions', 5500)])   # Tiny Studio: one game
        mono = C.category('dev:monolith productions', games, facts, prefs(), now=NOW)
        got = [(c.key, m) for c, m in C.other_studios(mono, games, facts)]
        self.assertEqual(got, [('dev:capcom', 1260)])
        pub = C.category('pub:capcom', games, facts, prefs(), now=NOW)
        got = [(c.key, c.title, m) for c, m in C.other_studios(pub, games, facts)]
        self.assertEqual(got, [('pub:warner bros. games', 'Warner Bros. Games', 500)])
        self.assertEqual(C.other_studios(cap, games, facts, n=0), [])

    def test_credit_line(self):
        self.assertEqual(C.credit_line({'developers': ['CAPCOM Co., Ltd.', 'Capcom', 'Acquire'],
                                        'publishers': ['Capcom'], 'year': 2023}),
                         "Developed by Capcom, Acquire · 2023")
        self.assertEqual(C.credit_line({'developers': [], 'publishers': ['Big Pub S.A.']}),
                         "Published by Big Pub")
        self.assertEqual(C.credit_line({'year': 2020}), "2020")
        self.assertEqual(C.credit_line(None), "")
        self.assertEqual(C.credit_line({'developers': None, 'year': 'soon'}), "")


class StudioCounts(unittest.TestCase):
    def test_both_roles_one_pass(self):
        games, facts = studio_library()
        got = C.studio_counts(games, facts)
        self.assertEqual(got['dev:capcom'], 3)
        self.assertEqual(got['pub:capcom'], 2)
        self.assertEqual(got['pub:warner bros. games'], 2)
        self.assertEqual(got['dev:tiny studio'], 1)
        self.assertNotIn('dev:', got)
