"""Home's Packed grid placement (w-153): nexus/gui/grid_flow.py, pure Python.
Cards go to the lowest columns in list order (near-ties go left), full-width
cards split the page into runs, and each run ends on an even bottom line."""

import ast
import unittest
from pathlib import Path

import tests.sandbox  # noqa: F401  (before anything from nexus)
from nexus.gui import grid_flow as G
from nexus.gui.grid_flow import Card, Placed, flow

GAP = 12
TOL = 32


def c(id, span=1, h=100, grows=False, min_h=0, anchor=False):
    return Card(id, span, h, grows, min_h, anchor)


def by_id(placed):
    return {p.id: p for p in placed}


def bottoms(placed, cols):
    """The lowest edge in each column."""
    out = [0] * cols
    for p in placed:
        for k in range(p.col, p.col + p.span):
            out[k] = max(out[k], p.y + p.h)
    return out


class Placement(unittest.TestCase):
    def test_equal_heights_keep_reading_order(self):
        got = flow([c(i) for i in 'abcdef'], 3, GAP, TOL)
        self.assertEqual([(p.id, p.col, p.y) for p in got],
                         [('a', 0, 0), ('b', 1, 0), ('c', 2, 0),
                          ('d', 0, 112), ('e', 1, 112), ('f', 2, 112)])

    def test_card_goes_to_the_lowest_column(self):
        got = by_id(flow([c('a', h=300), c('b', h=100), c('c', h=200), c('d', h=80)], 3, GAP, TOL))
        self.assertEqual((got['d'].col, got['d'].y), (1, 112))      # under the short one

    def test_near_tie_goes_left(self):
        # col 0 ends at 112 + 20 = 132 after a 120-high card, col 1 at 112: within tol
        got = by_id(flow([c('a', h=120), c('b', h=100), c('x', h=50)], 2, GAP, TOL))
        self.assertEqual(got['x'].col, 0)
        far = by_id(flow([c('a', h=200), c('b', h=100), c('x', h=50)], 2, GAP, TOL))
        self.assertEqual(far['x'].col, 1)                           # 100 px apart: not a tie

    def test_wide_card_takes_the_lowest_run_of_columns(self):
        got = by_id(flow([c('a', h=300), c('b', h=100), c('c', h=100), c('w', span=2, h=80)],
                         3, GAP, TOL))
        self.assertEqual((got['w'].col, got['w'].span, got['w'].y), (1, 2, 112))

    def test_span_is_clamped_to_the_columns(self):
        got = flow([c('w', span=3, h=50), c('a')], 2, GAP, TOL)
        self.assertEqual(got[0].span, 2)

    def test_full_width_card_closes_a_run(self):
        got = by_id(flow([c('a', h=300), c('b', h=100), c('f', span=3, h=50), c('d', h=40)],
                         3, GAP, TOL))
        self.assertEqual((got['f'].col, got['f'].y), (0, 312))       # under the run's bottom
        self.assertEqual(got['d'].y, 312 + 50 + GAP)                 # nothing climbs above it

    def test_one_column_is_plain_list_order(self):
        got = flow([c('a', h=10, grows=True), c('b', span=2, h=20), c('c', h=30)], 1, GAP, TOL)
        self.assertEqual([(p.id, p.col, p.span, p.y, p.h) for p in got],
                         [('a', 0, 1, 0, 10), ('b', 0, 1, 22, 20), ('c', 0, 1, 54, 30)])

    def test_min_h_is_a_floor(self):
        got = by_id(flow([c('t', h=100, min_h=250), c('s', h=300)], 2, GAP, TOL))
        self.assertEqual(got['t'].h, 300)            # 250 floor, then evened to the run bottom
        got = by_id(flow([c('t', h=100, min_h=250)], 1, GAP, TOL))
        self.assertEqual(got['t'].h, 250)

    def test_same_input_same_output(self):
        cards = [c('a', h=217, span=3), c('b', h=140), c('c', h=212, grows=True), c('d', h=90)]
        self.assertEqual(flow(cards, 3, GAP, TOL), flow(list(cards), 3, GAP, TOL))

    def test_empty(self):
        self.assertEqual(flow([], 3, GAP, TOL), [])


class EvenBottoms(unittest.TestCase):
    def test_run_ends_on_one_line(self):
        got = flow([c('a', h=300), c('b', h=100), c('c', h=200)], 3, GAP, TOL)
        self.assertEqual(set(bottoms(got, 3)), {300})

    def test_before_a_full_width_card_too(self):
        got = by_id(flow([c('a', h=300), c('b', h=100), c('f', span=2, h=40)], 2, GAP, TOL))
        self.assertEqual(got['b'].h, 300)
        self.assertEqual(got['f'].y, 312)

    def test_card_that_grows_goes_first(self):
        # col 1: g (grows) then s (doesn't); s moves down, g takes the space
        got = by_id(flow([c('a', h=400), c('g', h=100, grows=True), c('x', h=500),
                          c('s', h=100)], 3, GAP, TOL))
        self.assertEqual(got['g'].col, 1)
        self.assertEqual(got['s'].col, 1)
        self.assertEqual(got['s'].h, 100)                          # keeps its own height
        self.assertEqual(got['g'].h, 500 - 100 - GAP)              # took the whole gap
        self.assertEqual(got['s'].y, got['g'].y + got['g'].h + GAP)
        self.assertEqual(got['s'].y + got['s'].h, 500)

    def test_otherwise_the_last_card_grows(self):
        got = by_id(flow([c('a', h=100), c('b', h=300), c('d', h=50)], 2, GAP, TOL))
        self.assertEqual((got['d'].col, got['d'].y), (0, 112))
        self.assertEqual(got['a'].h, 100)
        self.assertEqual(got['d'].h, 300 - 112)

    def test_wide_last_card_grows_over_both_columns(self):
        got = by_id(flow([c('a', h=100), c('b', h=150), c('w', span=2, h=60), c('t', h=40),
                          c('u', h=200)], 3, GAP, TOL))
        self.assertEqual(len(set(bottoms(list(got.values()), 3))), 1)

    def test_card_stuck_under_a_wide_card_moves_over(self):
        # w spans cols 0-1; v would sit under it in col 0 and strand col 1 (user, 2026-11-19:
        # move the card over). v moves to col 2, w then ends both its columns and grows.
        got = by_id(flow([c('w', span=2, h=100), c('s', h=40), c('t', h=300), c('v', h=40)],
                         3, GAP, TOL))
        self.assertEqual((got['v'].col, got['v'].y), (2, 52 + 300 + GAP))
        line = got['v'].y + got['v'].h
        self.assertEqual(got['w'].y + got['w'].h, line)
        self.assertEqual(G.empty_share(list(got.values()), 3, GAP), 0)

    def test_stats_like_pick_moves_beside_the_space_map(self):
        got = by_id(flow([c('a', h=100), c('b', h=100), c('c', h=300),
                          c('space', span=2, h=100), c('pick', h=150)], 3, GAP, TOL))
        self.assertEqual(got['pick'].col, 2)
        self.assertEqual(len(set(bottoms(list(got.values()), 3))), 1)
        self.assertEqual(G.empty_share(list(got.values()), 3, GAP), 0)

    def test_move_only_when_it_shrinks_the_hole(self):
        # nothing is stranded: the order stays as placed
        cards = [c('w', span=2, h=100), c('s', h=100), c('x', h=40), c('y', h=40)]
        got = by_id(flow(cards, 3, GAP, TOL))
        self.assertEqual([(got[k].col, got[k].y) for k in 'wsxy'],
                         [(0, 0), (2, 0), (0, 112), (1, 112)])


class AboveAWide(unittest.TestCase):
    """A wide card starts under the tallest column it covers; the shorter
    column above it closes on that line instead of leaving a hole."""

    def test_short_card_grows_down_to_the_wide_card(self):
        got = by_id(flow([c('week', h=228), c('backlog', span=2, h=361),
                          c('space', span=2, h=300)], 3, GAP, TOL))
        self.assertEqual((got['space'].col, got['space'].y), (0, 373))
        self.assertEqual(got['week'].h, 361)
        self.assertEqual(got['backlog'].h, 361)

    def test_grows_card_goes_first(self):
        got = by_id(flow([c('a', h=100, grows=True), c('b', h=50), c('t', h=400),
                          c('x', h=50), c('w', span=2, h=80)], 3, GAP, TOL))
        # col 0: a (grows) then x; col 1: b; col 2: t. w lands under the taller of its columns
        w = got['w']
        for k in range(w.col, w.col + w.span):
            above = [p for p in got.values() if p.id != 'w' and p.col <= k < p.col + p.span
                     and p.y < w.y]
            self.assertEqual(max(p.y + p.h for p in above), w.y - GAP, k)

    def test_never_grows_a_card_under_another_wide_card(self):
        # col 0 ends with a 2-wide card that also covers col 1: nothing in col 0 can grow alone
        got = by_id(flow([c('t', h=400), c('w1', span=2, h=50), c('z', h=30),
                          c('w2', span=2, h=60)], 3, GAP, TOL))
        ids = sorted(got)
        for i in ids:
            for j in ids:
                a, b = got[i], got[j]
                if i < j and not (a.col + a.span <= b.col or b.col + b.span <= a.col):
                    self.assertFalse(a.y < b.y + b.h and b.y < a.y + a.h, (i, j))


class StatsPreset(unittest.TestCase):
    """The one layout whose measured heights the exploration recorded
    (Exploration - Home grid fill, step 7): 3 columns at 1440 px, compact."""

    CARDS = [c('heatmap', 3, 217, True), c('daily', 1, 140), c('backlog', 1, 212),
             c('spacemap', 2, 304, True), c('milestones', 1, 230, True), c('facts', 1, 155),
             c('pick', 1, 300)]

    def test_matches_the_simulation(self):
        got = by_id(flow(self.CARDS, 3, GAP, TOL))
        self.assertEqual({k: (p.col, p.y) for k, p in got.items()},
                         {'heatmap': (0, 0), 'daily': (0, 229), 'backlog': (1, 229),
                          'spacemap': (0, 453), 'milestones': (2, 229), 'facts': (2, 471),
                          'pick': (2, 638)})
        self.assertEqual(got['spacemap'].h, 485)                   # grown to the line
        self.assertEqual(set(bottoms(list(got.values()), 3)), {938})   # 1069 px with rows

    def test_under_four_percent_empty(self):
        self.assertLess(G.empty_share(flow(self.CARDS, 3, GAP, TOL), 3, GAP), 0.04)

    def test_hole_above_the_space_map_is_closed(self):
        # the exploration's simulation left 72 px under daily, above the 2-wide
        # Space map; daily now grows down to it (2026-11-19)
        got = by_id(flow(self.CARDS, 3, GAP, TOL))
        self.assertEqual(got['daily'].h, 140 + 72)
        self.assertEqual(G.empty_share(list(got.values()), 3, GAP), 0)

    def test_empty_share_counts_a_hole(self):
        placed = [Placed('a', 0, 1, 0, 100), Placed('b', 1, 1, 0, 40)]
        self.assertAlmostEqual(G.empty_share(placed, 2, GAP), 60 / (2 * (100 + GAP)), places=4)


class BesideAWide(unittest.TestCase):
    """w-156: cards beside an anchor (the Stage) end on its bottom; one that
    would stick out past it goes under it instead and the stack beside is
    closed on the anchor's bottom, so the anchor is not stretched."""

    def stage(self, h=686):
        return c('stage', span=2, h=h, anchor=True)

    def test_a_card_that_would_overshoot_goes_under(self):
        cards = [self.stage(), c('a', h=178), c('b', h=184), c('d', h=180),
                 c('e', h=230), c('f', h=230), c('g', h=230)]
        got = by_id(flow(cards, 3, GAP, TOL))
        self.assertEqual(got['stage'].h, 686)                       # not stretched
        self.assertEqual([got[k].col for k in 'abd'], [2, 2, 2])
        self.assertEqual(got['d'].y + got['d'].h, 686)              # the stack ends on its bottom
        for k in 'efg':                                             # under the Stage's row
            self.assertEqual(got[k].y, 686 + GAP)
        self.assertEqual(sorted(got[k].col for k in 'efg'), [0, 1, 2])
        self.assertEqual(G.empty_share(list(got.values()), 3, GAP), 0)

    def test_the_card_that_can_grow_takes_the_slack(self):
        cards = [self.stage(), c('a', h=100), c('b', h=100, grows=True), c('d', h=100),
                 c('e', h=400), c('f', h=400), c('g', h=400)]
        got = by_id(flow(cards, 3, GAP, TOL))
        self.assertEqual(got['b'].h, 100 + 686 - 324)               # b shows more
        self.assertEqual(got['d'].y + got['d'].h, 686)              # d moved down with it
        self.assertEqual(got['a'].h, 100)
        self.assertEqual(got['stage'].h, 686)

    def test_a_card_that_fits_stays_beside(self):
        cards = [self.stage(400), c('a', h=190), c('b', h=190)]
        got = by_id(flow(cards, 3, GAP, TOL))
        self.assertEqual((got['a'].col, got['b'].col), (2, 2))
        self.assertEqual(got['b'].y + got['b'].h, 400)

    def test_a_small_overshoot_stays_beside(self):
        cards = [self.stage(400), c('a', h=190), c('b', h=200)]
        got = by_id(flow(cards, 3, GAP, TOL))                       # ends 2 px past it: within tol
        self.assertEqual(got['b'].col, 2)

    def test_a_card_as_tall_as_the_anchor_stays_beside(self):
        cards = [self.stage(100), c('a', h=40), c('t', h=300)]
        got = by_id(flow(cards, 3, GAP, TOL))
        self.assertEqual((got['a'].col, got['t'].col), (2, 2))

    def test_without_an_anchor_nothing_changes(self):
        plain = [c('stage', span=2, h=686)] + [c(k, h=h) for k, h in
                                               (('a', 178), ('b', 184), ('d', 180), ('e', 230))]
        got = by_id(flow(plain, 3, GAP, TOL))
        self.assertEqual([got[k].col for k in 'abde'], [2, 2, 2, 2])


class Thumbs(unittest.TestCase):
    """w-157: a preset's thumbnail is the real flow with fixed card heights."""

    def test_every_preset_packs_into_three_columns(self):
        import tests.sandbox  # noqa: F401
        from nexus.gui.layouts import dash_preset, PRESETS, preset_thumb
        for key, _name, _tip in PRESETS:
            items = dash_preset(key)
            shown = [i['id'] for i in items if not i['hidden']]
            placed = preset_thumb(items)
            self.assertEqual(sorted(p.id for p in placed), sorted(shown), key)
            for p in placed:
                self.assertTrue(0 <= p.col and p.col + p.span <= 3, (key, p))
                self.assertGreater(p.h, 0)
            for i, a in enumerate(placed):                       # nothing on top of another
                for b in placed[i + 1:]:
                    cols = set(range(a.col, a.col + a.span)) & set(range(b.col, b.col + b.span))
                    if cols:
                        self.assertTrue(a.y + a.h <= b.y or b.y + b.h <= a.y, (key, a, b))
            self.assertEqual(placed, preset_thumb(items), key)   # same input, same output
        stage = {p.id: p for p in preset_thumb(dash_preset('cinematic'))}['stage']
        self.assertEqual((stage.col, stage.span, stage.y), (0, 3, 0))


class Pure(unittest.TestCase):
    def test_no_qt_imports(self):
        src = Path(G.__file__).read_text(encoding='utf-8')
        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or '')
        self.assertFalse([n for n in names if 'PyQt' in n or n.startswith('.')], names)

    def test_placed_is_a_plain_tuple(self):
        self.assertEqual(Placed('a', 0, 1, 0, 10), ('a', 0, 1, 0, 10))


if __name__ == '__main__':
    unittest.main()
