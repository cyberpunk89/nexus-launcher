"""Home's up/down moves by column (w-156): grid_flow.move_target picks the
card above or below in the same column from the last placement, and
dash_move puts the moved card before or after it in the list. Pure Python."""

import unittest

import tests.sandbox  # noqa: F401  (before anything from nexus)
from nexus.gui import dashboard as D
from nexus.gui.grid_flow import Card, flow, move_target

GAP, TOL = 12, 32


def c(id, span=1, h=100):
    return Card(id, span, h)


def items(*ids):
    return [{'id': i, 'size': 'small'} for i in ids]


class MoveTarget(unittest.TestCase):
    def grid(self):
        # a b c / d e f, three columns, equal heights
        return flow([c(k) for k in 'abcdef'], 3, GAP, TOL)

    def test_up_is_the_card_above(self):
        self.assertEqual(move_target(self.grid(), 'e', 'up'), 'b')
        self.assertEqual(move_target(self.grid(), 'f', 'up'), 'c')

    def test_down_is_the_card_below(self):
        self.assertEqual(move_target(self.grid(), 'a', 'down'), 'd')

    def test_nothing_above_or_below_is_none(self):
        self.assertIsNone(move_target(self.grid(), 'b', 'up'))
        self.assertIsNone(move_target(self.grid(), 'e', 'down'))

    def test_unknown_card_is_none(self):
        self.assertIsNone(move_target(self.grid(), 'zz', 'up'))

    def test_uneven_columns_use_the_column_not_the_row(self):
        # a is tall: b, d, e stack in column 1 while a keeps column 0
        placed = flow([c('a', h=300), c('b'), c('c'), c('d'), c('e')], 3, GAP, TOL)
        col = {p.id: p.col for p in placed}
        self.assertEqual(col['b'], 1)
        self.assertEqual(col['d'], 1)
        self.assertEqual(move_target(placed, 'd', 'up'), 'b')
        self.assertEqual(move_target(placed, 'b', 'down'), 'd')

    def test_wide_card_goes_to_the_nearest_by_x(self):
        placed = flow([c('a'), c('b'), c('c'), c('w', span=2)], 3, GAP, TOL)
        # w sits under a and b (cols 0-1): nearest above by x is a (col 0)
        w = {p.id: p for p in placed}['w']
        self.assertEqual((w.col, w.span), (0, 2))
        self.assertEqual(move_target(placed, 'w', 'up'), 'a')

    def test_narrow_card_under_a_wide_one_sees_the_wide_one(self):
        placed = flow([c('w', span=2), c('a', h=300), c('b'), c('d')], 3, GAP, TOL)
        # a is beside w (col 2); b and d fill the two columns under w
        by = {p.id: p for p in placed}
        self.assertEqual(by['b'].col, 0)
        self.assertEqual(move_target(placed, 'b', 'up'), 'w')
        self.assertEqual(move_target(placed, 'w', 'down'), 'b')

    def test_full_width_card_sees_both_sides(self):
        placed = flow([c('a'), c('b'), c('c'), c('f', span=3), c('d')], 3, GAP, TOL)
        self.assertEqual(move_target(placed, 'd', 'up'), 'f')
        self.assertEqual(move_target(placed, 'f', 'down'), 'd')
        self.assertEqual(move_target(placed, 'f', 'up'), 'a')       # nearest by x: leftmost


class DashMoveByColumn(unittest.TestCase):
    def test_up_goes_before_the_card_above(self):
        its = items('a', 'b', 'c', 'd', 'e', 'f')
        placed = flow([c(k['id']) for k in its], 3, GAP, TOL)
        out = D.dash_move(its, 'e', 'up', 3, placed=placed)
        self.assertEqual([i['id'] for i in out], ['a', 'e', 'b', 'c', 'd', 'f'])

    def test_down_goes_after_the_card_below(self):
        its = items('a', 'b', 'c', 'd', 'e', 'f')
        placed = flow([c(k['id']) for k in its], 3, GAP, TOL)
        out = D.dash_move(its, 'a', 'down', 3, placed=placed)
        self.assertEqual([i['id'] for i in out], ['b', 'c', 'd', 'a', 'e', 'f'])

    def test_that_cannot_move_is_a_no_op(self):
        its = items('a', 'b', 'c', 'd')
        placed = flow([c(k['id']) for k in its], 3, GAP, TOL)
        for k, d in (('b', 'up'), ('d', 'down'), ('c', 'down')):
            self.assertEqual([i['id'] for i in D.dash_move(its, k, d, 3, placed=placed)],
                             ['a', 'b', 'c', 'd'], (k, d))

    def test_left_right_still_swap_with_the_neighbour(self):
        its = items('a', 'b', 'c')
        placed = flow([c(k['id']) for k in its], 3, GAP, TOL)
        out = D.dash_move(its, 'a', 'right', 3, placed=placed)
        self.assertEqual([i['id'] for i in out], ['b', 'a', 'c'])

    def test_hidden_cards_keep_their_place(self):
        its = items('a', 'x', 'b', 'c', 'd')
        its[1]['hidden'] = True
        vis = [k for k in its if not k.get('hidden')]
        placed = flow([c(k['id']) for k in vis], 3, GAP, TOL)
        out = D.dash_move(its, 'd', 'up', 3, skip=(), placed=placed)
        self.assertEqual([i['id'] for i in out], ['d', 'a', 'x', 'b', 'c'])


if __name__ == '__main__':
    unittest.main()
