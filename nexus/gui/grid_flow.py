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
when it is the last card in all of them. A hole no card can reach stays.

One column is plain list order with no evening, as Home was before.
Exploration - Home grid fill has the measurements behind this."""

from typing import NamedTuple


class Card(NamedTuple):
    id: str
    span: int          # columns wide; clamped to the grid
    h: int             # height for its width, px
    grows: bool = False    # can show more when given more height
    min_h: int = 0     # a floor (Tall)


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
    out: list[list] = []              # [id, col, span, y, h, grows], in list order
    tops = [0] * cols                 # the next free y in each column
    run: list[int] = []               # indices in `out` of the current run

    for card in cards:
        span = max(1, min(int(card.span), cols))
        h = max(int(card.h), int(card.min_h or 0))
        if cols == 1:
            out.append([card.id, 0, 1, tops[0], h, card.grows])
            tops[0] += h + gap_v
            continue
        if span >= cols:
            _even(out, run, cols)
            y = _next_top(out, run, tops, gap_v)
            out.append([card.id, 0, cols, y, h, card.grows])
            tops = [y + h + gap_v] * cols
            run = []
            continue
        starts = [max(tops[s:s + span]) for s in range(cols - span + 1)]
        low = min(starts)
        col = next(s for s, t in enumerate(starts) if t <= low + tol)
        y = starts[col]
        out.append([card.id, col, span, y, h, card.grows])
        run.append(len(out) - 1)
        for k in range(col, col + span):
            tops[k] = y + h + gap_v
    if cols > 1:
        _even(out, run, cols)
    return [Placed(i, c, s, y, h) for i, c, s, y, h, _g in out]


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
    stacks: list[list[int]] = [[] for _ in range(cols)]     # run cards per column, top down
    for i in run:
        for k in range(out[i][1], out[i][1] + out[i][2]):
            stacks[k].append(i)
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
