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
        self.assertEqual(C.CAPS, {'for_you': 6, 'genres': 6})
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
