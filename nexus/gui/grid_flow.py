"""Home's Packed grid (w-153): where each card goes, in pure Python (no Qt),
so the placement can be tested with made-up heights and reused for moves.

Cards keep their saved list order. Each one goes to the lowest run of `span`
columns; starts whose top is within `tol` of the lowest count as a tie and the
leftmost wins, so the order you set is mostly the order you read (the "flow
tolerance" of CSS Grid Lanes). A card as wide as the grid closes a run: the
run is evened first, then the card sits under it across the full width.

Evening a run (before each full-width card and at the end): every column ends
on the run's bottom line. In a column that is short, the last card that only
sits in that column grows, preferring one that can show more (`grows`), with
the cards below it moving down; a card spanning several columns grows only
when it is the last card in all of them. Before that, a card stuck under part
of a wide card (so the wide card can't reach the line in its other columns)
moves to a column outside the wide card when that leaves less empty (user,
2026-11-19: "move the card over"). A hole no card can reach stays.

An `anchor` card (the Stage, w-156) is a wide card the cards beside it end on:
one that would stick out well past its bottom goes under it instead, and the
column beside it closes on that bottom (its last card grows), so the Stage is
not stretched to a taller stack. Without an anchor nothing changes.

One column is plain list order with no evening, as Home was before.
Exploration - Home grid fill has the measurements behind this."""

from typing import NamedTuple


class Card(NamedTuple):
    id: str
    span: int          # columns wide; clamped to the grid
    h: int             # height for its width, px
    grows: bool = False    # can show more when given more height
    min_h: int = 0     # a floor (Tall)
    anchor: bool = False   # cards beside it end on its bottom (the Stage, w-156)


class Placed(NamedTuple):
    id: str
    col: int
    span: int
    y: int
    h: int


def flow(cards, cols: int, gap_v: int, tol: int) -> list[Placed]:
    """Place `cards` (in order) on `cols` columns with `gap_v` px between
    cards stacked in a column. Same input, same output."""
    cols = max(1, int(cols))
    out: list[list] = []              # [id, col, span, y, h, grows, anchor], in list order
    tops = [0] * cols                 # the next free y in each column
    run: list[int] = []               # indices in `out` of the current run

    for card in cards:
        span = max(1, min(int(card.span), cols))
        h = max(int(card.h), int(card.min_h or 0))
        if cols == 1:
            out.append([card.id, 0, 1, tops[0], h, card.grows, card.anchor])
            tops[0] += h + gap_v
            continue
        if span >= cols:
            _unstrand(out, run, cols, gap_v, tol)
            _even(out, run, cols)
            y = _next_top(out, run, tops, gap_v)
            out.append([card.id, 0, cols, y, h, card.grows, card.anchor])
            tops = [y + h + gap_v] * cols
            run = []
            continue
        starts = [max(tops[s:s + span]) for s in range(cols - span + 1)]
        low = min(starts)
        col = next(s for s, t in enumerate(starts) if t <= low + tol)
        line = _sticks_out(out, run, tops, gap_v, col, starts[col], h, tol) if span == 1 else None
        if line is not None:
            # beside a wide card (the Stage) it would end well past it: close
            # this column on the wide card's bottom (its last card grows to
            # it) and put the card where the next lowest column is, so the
            # wide card is not stretched to this stack (w-156)
            rest = [(t, s) for s, t in enumerate(starts) if s != col
                    and _sticks_out(out, run, tops, gap_v, s, t, h, tol) is None]
            if rest:
                _close(out, run, col, line)
                tops[col] = starts[col] = line + gap_v
                low = min(rest)[0]
                col = next(s for t, s in sorted(rest, key=lambda r: r[1]) if t <= low + tol)
        y = starts[col]
        if span > 1:
            # a wide card starts under the tallest column it covers: close the
            # shorter ones on the line above it rather than leave a hole there
            stacks = _stacks(out, run, cols)
            for k in range(col, col + span):
                if tops[k] < y and stacks[k] and out[stacks[k][-1]][2] == 1:
                    _close(out, run, k, y - gap_v)
                    tops[k] = y
        out.append([card.id, col, span, y, h, card.grows, card.anchor])
        run.append(len(out) - 1)
        for k in range(col, col + span):
            tops[k] = y + h + gap_v
    if cols > 1:
        _unstrand(out, run, cols, gap_v, tol)
        _even(out, run, cols)
    return [Placed(i, c, s, y, h) for i, c, s, y, h, _g, _a in out]


def _sticks_out(out, run, tops, gap_v, col, y, h, tol) -> int | None:
    """The bottom of a wide card of the run if a single-column card at (col, y)
    would end more than `tol` below it, else None. The wide card is beside the
    card (its columns do not include `col`), started above y, and has a column
    under it still free; y is above its bottom, and the card is shorter than
    the wide one (one as tall stays beside it)."""
    for i in run:
        _id, c0, span, y0, h0, _g, anchor = out[i]
        if not anchor or span < 2 or c0 <= col < c0 + span or not y0 < y < y0 + h0:
            continue
        if all(tops[k] != y0 + h0 + gap_v for k in range(c0, c0 + span)):
            continue                                     # the space under it is taken
        if h < h0 and y + h > y0 + h0 + tol:
            return y0 + h0
    return None


def _close(out, run, col, line) -> None:
    """Grow the last card of single-column `col` (one that can show more
    first) so the column ends on `line`; the cards below it move down."""
    tail = sorted((i for i in run if out[i][1] == col and out[i][2] == 1
                   and out[i][3] < line), key=lambda i: out[i][3])
    if not tail:
        return
    short = line - (out[tail[-1]][3] + out[tail[-1]][4])
    if short <= 0:
        return
    grower = next((i for i in reversed(tail) if out[i][5]), tail[-1])
    out[grower][4] += short
    for i in tail[tail.index(grower) + 1:]:
        out[i][3] += short


def _next_top(out, run, tops, gap_v) -> int:
    """Where a full-width card goes: under everything so far (the run was
    just evened, so this is its bottom line plus the gap)."""
    if not run:
        return max(tops)
    return max(out[i][3] + out[i][4] for i in run) + gap_v


def _even(out, run, cols) -> None:
    """Grow cards so every column of the run ends on its bottom line."""
    if not run:
        return
    line = max(out[i][3] + out[i][4] for i in run)
    stacks = _stacks(out, run, cols)
    done: set[int] = set()
    for k in range(cols):
        stack = stacks[k]
        if not stack:
            continue                                     # nothing here reaches: a hole
        last = stack[-1]
        if last in done:
            continue
        c0, span = out[last][1], out[last][2]
        if span > 1:
            # grows only when it ends every column it spans
            if all(stacks[j] and stacks[j][-1] == last for j in range(c0, c0 + span)):
                out[last][4] = line - out[last][3]
                done.add(last)
            continue
        short = line - (out[last][3] + out[last][4])
        if short <= 0:
            continue
        tail = []                                        # the single-column cards at the bottom
        for i in reversed(stack):
            if out[i][2] != 1:
                break
            tail.append(i)
        tail.reverse()
        grower = next((i for i in reversed(tail) if out[i][5]), last)
        out[grower][4] += short
        for i in tail[tail.index(grower) + 1:]:
            out[i][3] += short
        done.add(grower)


def _stacks(out, run, cols) -> list[list[int]]:
    """The run's cards in each column, top down."""
    stacks: list[list[int]] = [[] for _ in range(cols)]
    for i in run:
        for k in range(out[i][1], out[i][1] + out[i][2]):
            stacks[k].append(i)
    return stacks


def _evened_empty(out, run, cols, gap_v) -> int:
    """Empty px the run would keep after evening (a dry run on a copy)."""
    cp = [list(r) for r in out]
    _even(cp, run, cols)
    top = min(cp[i][3] for i in run)
    box = max(cp[i][3] + cp[i][4] for i in run) + gap_v - top
    covered = [0] * cols
    for i in run:
        for k in range(cp[i][1], cp[i][1] + cp[i][2]):
            covered[k] += cp[i][4] + gap_v
    return sum(box - c for c in covered)


def _unstrand(out, run, cols, gap_v, tol) -> None:
    """Move single-column cards out from under part of a wide card, so the
    wide card ends all its columns and can grow, when that leaves less empty."""
    if not run:
        return
    top = min(out[i][3] for i in run)
    for p in [i for i in run if out[i][2] > 1]:
        mine = range(out[p][1], out[p][1] + out[p][2])
        stacks = _stacks(out, run, cols)
        ends = [stacks[k][-1] == p for k in mine]
        if all(ends) or not any(ends):
            continue
        stuck = [i for i in run if i > p and out[i][3] > out[p][3]
                 and out[i][1] in mine]
        others = [k for k in range(cols) if k not in mine]
        if not others or any(out[i][2] != 1 for i in stuck):
            continue
        cand = [list(r) for r in out]
        tops = [top] * cols
        for i in run:
            if i in stuck:
                continue
            for k in range(cand[i][1], cand[i][1] + cand[i][2]):
                tops[k] = max(tops[k], cand[i][3] + cand[i][4] + gap_v)
        for i in stuck:
            low = min(tops[k] for k in others)
            k = next(k for k in others if tops[k] <= low + tol)
            cand[i][1], cand[i][3] = k, tops[k]
            tops[k] += cand[i][4] + gap_v
        if _evened_empty(cand, run, cols, gap_v) < _evened_empty(out, run, cols, gap_v):
            out[:] = cand


def move_target(placed, wid: str, direction: str) -> str | None:
    """The card an up/down move goes next to (w-156): the nearest card above
    (`up`) or below (`down`) `wid` in a column it covers, from the last
    placement. A tie goes to the one nearest by x. None when there is no such
    card, so the move does nothing."""
    me = next((p for p in placed if p.id == wid), None)
    if me is None or direction not in ('up', 'down'):
        return None
    mine = range(me.col, me.col + me.span)
    best = None
    for p in placed:
        if p.id == wid or not any(k in mine for k in range(p.col, p.col + p.span)):
            continue
        if direction == 'up' and p.y < me.y:
            near = me.y - (p.y + p.h)
        elif direction == 'down' and p.y > me.y:
            near = p.y - (me.y + me.h)
        else:
            continue
        key = (near, abs(p.col - me.col))
        if best is None or key < best[0]:
            best = (key, p.id)
    return best[1] if best else None


def empty_share(placed, cols: int, gap_v: int) -> float:
    """The part of the grid's box (top to the lowest card) that no card
    covers, 0-1. Each card counts with the gap under it, so gutters are not
    holes (the measure Exploration - Home grid fill used)."""
    if not placed:
        return 0.0
    box = max(p.y + p.h for p in placed) + gap_v
    covered = [0] * cols
    for p in placed:
        for k in range(p.col, p.col + p.span):
            covered[k] += p.h + gap_v
    return sum(box - c for c in covered) / (cols * box)
