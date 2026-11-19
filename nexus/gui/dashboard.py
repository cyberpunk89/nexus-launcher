"""The Home dashboard: its layout model and widgets."""

import html
import random
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QEvent, QRectF, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QFontMetrics, QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap, QRadialGradient)
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QProgressBar, QPushButton, QRadioButton, QSizePolicy, QStyle, QToolButton, QVBoxLayout, QWidget)

from nexus import gamedb, smart, storeinfo
from nexus.features import has

from .common import alive, COLLECTION_NAMES
from .layouts import (dash_preset, HomeHistory, HomeSnap, layout_for, MAX_LAYOUTS, MY_HOME,
    OLD_HOME, PRESET_DENSITY, PRESET_LABELS, PROMPT_MAX)
from .fmt import fmt_ago, fmt_hours, fmt_relative, fmt_size
from .library import _pickable, Dwell, FollowHero, pick_kicker, VIBE_LABELS, vibe_pools
from .moments import DISK_FULL
from .grid_flow import drop_before, move_target
from .home_feed import HomeFeed, today  # noqa: F401
from .pick_wheel import PickWheel, SEGMENTS
from .widget_spec import dash_tables, OPTION_BAR, SPEC, SPECS, WidgetSpec  # noqa: F401
from .theme import FONTS, FS, G, GLYPH_H, GRAD, P, PERSON_H, PILL_H, R, scaled, SP, STAGE_FACT_H
from .theme import collection_look


DASH_SPAN = {'small': 1, 'tall': 1, 'wide': 2, 'full': 99, 'cinematic': 99, 'banner': 99}
DOCKED_3 = 1050                         # least Home width for 3 columns beside the panel (w-157)
DASH_ROWS = {'tall': 2}                 # grid rows a size covers (default 1)
# Sizes, options, labels, placement and For you feeds come from one WidgetSpec
# per widget (widget_spec.py, w-136); KINDS keeps the default order.
_T = dash_tables(SPECS)
DASH_KINDS = _T['KINDS']
DASH_SIZES = _T['SIZES']
DASH_DEFAULT_SIZE = _T['DEFAULT_SIZE']
# Widgets folded into another: a saved layout's first one becomes the new
# widget in its place (so it doesn't jump to the end).
DASH_MERGED = {'activity': 'system', 'storage': 'system', 'health': 'system',
               'proton': 'system', 'jump': 'stage', 'playtime': 'week', 'surprise': 'pick',
               'daily': 'pick'}
# `owned` (In your library) became the Smart shelf rule 'owned' and `facts`
# (Library facts) was dropped (w-166): dash_normalize handles both
# (_fold_owned; unknown ids are dropped), so neither has an entry above.
# Merged widgets that are shown even when the old one was hidden: the Stage
# takes the hero's place, which was always there (w-85).
DASH_MERGED_SHOWN = {'stage'}
# Widgets added later that belong next to a related one rather than last.
DASH_PLACE_AFTER = _T['PLACE_AFTER']
# Per-widget options checked like sizes: the first value is the default and
# a bad one falls back to it (w-85, w-90). The Stage's Pinned art also keeps
# a `pin` (the game's slug) on its item, checked in dash_normalize.
DASH_OPTIONS = _T['OPTIONS']
# The edit bar's "Style:" button names (a kind listed here gets the button).
DASH_STYLE_LABELS = _T['STYLE_LABELS']
# The edit bar's "Background:" button names (w-90); 'surface' is shown as Plain.
DASH_BG_LABELS = _T['BG_LABELS']
PIN_MAX = 80                         # a pinned game's slug, at most this long
# Home's spacing (Customise → Density, w-90): Home only, on top of Appearance →
# Size. Comfortable is how Home always looked.
DASH_DENSITY = {
    'comfortable': {'grid_h': SP['4'], 'grid_v': SP['5'], 'card_pad': SP['4'],
                    'art_side': int(SP['8'] * 1.5), 'art_tb': SP['5'], 'row_gap': SP['3'],
                    'clean_gap': SP['4'], 'home_side': SP['5'], 'home_top': SP['4']},
    'compact': {'grid_h': SP['3'], 'grid_v': SP['3'], 'card_pad': SP['3'],
                'art_side': SP['8'], 'art_tb': SP['4'], 'row_gap': SP['2'],
                'clean_gap': SP['3'], 'home_side': SP['4'], 'home_top': SP['3']},
}
DENSITY_LABELS = {'comfortable': "Comfortable", 'compact': "Compact"}
# {event or kind: the kinds it refreshes}: a spec's `feeds` (w-138).
FED_BY = _T['FED_BY']
# Refreshes of these mean For you now's moments may have moved too.
FORYOU_FEEDS = set(SPEC['foryou'].feeds)


def fed(kinds) -> set:
    """`kinds` plus every kind fed by any of them."""
    out = set(kinds)
    for k in kinds:
        out |= FED_BY.get(k, set())
    return out


def _dash_shelf(value) -> str:
    """The smart shelf the Home widget shows (an item's `shelf` key): a
    rule, or `user:<id>` for a shelf you built (w-51; checked when shown)."""
    ok = value in smart.WIDGET_RULES or (smart.is_user(value) and len(value) <= 40)
    return value if ok else smart.WIDGET_DEFAULT


def _user_shelf(hub, key) -> dict | None:
    """A shelf you built (w-51) by its `user:<id>` key, unless removed."""
    s = smart.find_user(getattr(getattr(hub, 'prefs', None), 'smart_shelves', ()), key)
    return s if s and not s['removed'] else None


def dash_shelf_label(hub, value) -> str:
    """The Home widget's shelf as Customise names it."""
    mine = _user_shelf(hub, value)
    if mine:
        return smart.title_of(mine)
    rule = _dash_shelf(value)
    return smart.label(f'smart:{smart.WIDGET_DEFAULT if smart.is_user(rule) else rule}')


def _dash_options(wid: str, it: dict) -> dict:
    """The widget's DASH_OPTIONS keys from a saved item, each checked."""
    return {key: (it.get(key) if it.get(key) in ok else ok[0])
            for key, ok in DASH_OPTIONS.get(wid, {}).items()}


def dash_kind_defaults() -> list[dict]:
    """Every widget with its DASH_KINDS size and hidden flag, in DASH_KINDS
    order: what a saved layout gains for a widget it doesn't have yet."""
    return [{'id': k, 'size': d, 'hidden': h, **({'shelf': _dash_shelf(None)} if k == 'smart'
                                                 else {}), **_dash_options(k, {})}
            for k, _s, d, h in DASH_KINDS]


def dash_default() -> list[dict]:
    """Home with nothing saved, and Reset layout: the Cinematic preset (w-92)."""
    return dash_preset('cinematic')


def stage_height(size: str, viewport_h: int, style: str = 'row', bg: str = 'art') -> int:
    """The Stage's height for a size in a Home viewport this tall: tall but
    never the whole screen, so the next row always peeks. Clean (w-90) has
    no row on the art: shorter, so the Continue shelf under it shows whole
    at 850 px; off the art (gradient, Plain) shorter still (the caller keeps
    it at least as tall as its layout needs)."""
    if size == 'banner':
        return 250
    if size == 'wide':
        return max(320, min(420, viewport_h - 360))
    if style == 'clean':
        return 320 if bg in ('gradient', 'surface') else max(400, min(540, viewport_h - 320))
    return max(480, min(610, viewport_h - 290))


def stage_pin(value) -> str | None:
    """A pinned game's slug as saved, or None when it isn't one."""
    if isinstance(value, str) and 0 < len(value) <= PIN_MAX and value.isprintable() \
            and value.strip() == value:
        return value
    return None


SAVE_EMPTY = "Nothing recorded for this game yet."


def save_rows(g: dict, facts: dict | None, sessions: int | None = None, owners=(),
              last: dict | None = None) -> list[tuple]:
    """Your save's rows for one game (w-91), each left out when it has
    nothing to say: ('big', play time), ('sub', 'Hades · 24 sessions'),
    ('bar', caption, value, share, tooltip) for Steam achievements and the
    HowLongToBeat main story (the bar is your play time against it, at most
    full), ('owners', friends, 'Ana, Ben +1 own it too') and ('last', 'Last
    unlock: … · 3 days ago') from an achievement {title, time}. With none of
    them: ('sub', name) and ('empty', SAVE_EMPTY)."""
    name = g.get('display_name') or g.get('slug') or ''
    minutes = g.get('playtime_minutes') or 0
    ach = g.get('achievements') if g.get('source') == 'steam' else None
    ach = ach if isinstance(ach, (list, tuple)) and len(ach) == 2 and ach[1] else None
    main = smart.main_hours((facts or {}).get(g.get('slug')))
    owners = list(owners or ())
    last_text = None
    if isinstance(last, dict) and last.get('title'):
        when = fmt_ago(last.get('time')) if last.get('time') else ''
        last_text = f"Last unlock: {last['title']}" + (f" · {when}" if when else '')
    sessions = sessions if isinstance(sessions, int) and sessions > 0 else None
    if not (minutes or sessions or ach or main or owners or last_text):
        return [('sub', name), ('empty', SAVE_EMPTY)]
    rows: list[tuple] = []
    if minutes:
        rows.append(('big', fmt_hours(minutes)))
    rows.append(('sub', " · ".join(
        [name] + ([f"{sessions} session{'s' if sessions != 1 else ''}"] if sessions else []))))
    if ach:
        rows.append(('bar', "Achievements", f"{ach[0]} of {ach[1]}",
                     max(0.0, min(1.0, ach[0] / ach[1])), "Unlocked on Steam"))
    if main:
        rows.append(('bar', "Main story", f"{main:g} h", min(1.0, minutes / 60 / main),
                     "HowLongToBeat's main story; the bar is your play time"))
    if owners:
        n = len(owners)
        rows.append(('owners', owners, f"{names_text([o.get('name') or '' for o in owners])} "
                                       f"own{'s' if n == 1 else ''} it too"))
    if last_text:
        rows.append(('last', last_text))
    return rows


def last_unlock(data) -> dict | None:
    """The newest unlocked achievement in an `--achievements <id> --json`
    answer (items carry unlocked and time), or None."""
    items = (data or {}).get('items') if isinstance(data, dict) else None
    got = [a for a in items or () if isinstance(a, dict) and a.get('unlocked')
           and isinstance(a.get('time'), (int, float)) and a.get('title')]
    return max(got, key=lambda a: a['time']) if got else None


def _fold_owned(items: list[dict]) -> list[dict]:
    """In your library became the Smart shelf rule 'owned' (w-166). A saved
    `owned` goes. When it was shown and the layout has no shown Smart shelf,
    the Smart shelf takes its place showing that rule; with a shown Smart
    shelf already, that one stays as it is (a widget cannot show two rules).
    Idempotent."""
    if not any(it.get('id') == 'owned' for it in items):
        return items
    shown = any(it.get('id') == 'owned' and not it.get('hidden') for it in items)
    take = shown and not any(it.get('id') == 'smart' and not it.get('hidden') for it in items)
    out, placed = [], False
    for it in items:
        if it.get('id') == 'owned':
            if take and not placed:
                placed = True
                out.append({'id': 'smart', 'size': 'full', 'hidden': False, 'shelf': 'owned'})
        elif not (take and it.get('id') == 'smart'):
            out.append(it)
    return out


def _fold_daily(items: list[dict]) -> list[dict]:
    """Game of the day became Pick for me's style 'today' (w-164). The first
    `daily` becomes `pick` where it stood (size small, hidden when every
    Game of the day was, style 'today'). A layout that already has a Pick keeps
    it as it is, and its place: the saved Game of the day only goes, except that
    a shown one turns a hidden Pick on, as today's pick. Idempotent."""
    dailies = [it for it in items if it.get('id') == 'daily']
    if not dailies:
        return items
    shown = any(not d.get('hidden') for d in dailies)
    has_pick = any(it.get('id') == 'pick' for it in items)
    out, placed = [], False
    for it in items:
        if it.get('id') == 'daily':
            if not has_pick and not placed:
                out.append({'id': 'pick', 'size': 'small', 'hidden': not shown, 'style': 'today'})
                placed = True
        elif it.get('id') == 'pick' and shown and it.get('hidden') and not placed:
            placed = True
            out.append({**it, 'hidden': False, 'style': 'today'})
        else:
            out.append(it)
    return out


def _fold_wishlist(items: list[dict]) -> list[dict]:
    """Wishlist deals and Upcoming became one Wishlist card, kind `deals`
    (w-163). The first of the two in the layout stays where it is with its
    size (an Upcoming turns into `deals`); the other goes. Hidden only when
    every one of them was. Idempotent."""
    olds = [it for it in items if it.get('id') in ('deals', 'upcoming')]
    if not any(it.get('id') == 'upcoming' for it in olds):
        return items
    first = olds[0]
    out = []
    for it in items:
        if it is first:
            out.append({**it, 'id': 'deals', 'hidden': all(o.get('hidden') for o in olds)})
        elif it.get('id') not in ('deals', 'upcoming'):
            out.append(it)
    return out


DASH_VERSION = 2   # 2: the Stage moved to the top (w-86); renames to new kinds are not gated


def dash_normalize(items, version: int = DASH_VERSION) -> list[dict]:
    """A saved layout made safe: unknown widgets dropped, bad sizes fixed,
    merged widgets (DASH_MERGED) replaced in place by the one they became,
    and widgets added since it was saved put in (next to their DASH_PLACE_AFTER
    anchor, else at the end) with their defaults. A layout saved before version 2
    also gets the Stage moved to the top; every other widget keeps its place."""
    items = [it for it in (items if isinstance(items, list) else []) if isinstance(it, dict)]
    if not items:
        return dash_default()                 # nothing saved: the Cinematic preset
    items = _fold_wishlist(_fold_owned(_fold_daily(items)))
    legacy: dict[str, list[dict]] = {}
    for it in items:
        if it.get('id') in DASH_MERGED:
            legacy.setdefault(DASH_MERGED[it['id']], []).append(it)
    out, seen = [], set()
    for it in items:
        wid = it.get('id')
        if wid in DASH_MERGED:
            wid = DASH_MERGED[wid]
            if wid in seen:
                continue
            olds = legacy[wid]
            it = {'id': wid, 'size': None, 'hidden': wid not in DASH_MERGED_SHOWN
                  and all(o.get('hidden') for o in olds)}
        if wid not in DASH_SIZES or wid in seen:
            continue
        seen.add(wid)
        size = it.get('size')
        out.append({'id': wid,
                    'size': size if size in DASH_SIZES[wid] else DASH_DEFAULT_SIZE[wid],
                    'hidden': bool(it.get('hidden'))})
        if wid == 'smart':
            out[-1]['shelf'] = _dash_shelf(it.get('shelf'))
        out[-1].update(_dash_options(wid, it))
        if wid == 'stage' and out[-1]['bg'] == 'pinned':
            pin = stage_pin(it.get('pin'))
            if pin is None:
                out[-1]['bg'] = 'art'            # Pinned art without a game: Game art
            else:
                out[-1]['pin'] = pin
    last: dict[str, str] = {}             # anchor -> the newest widget put after it
    for d in dash_kind_defaults():
        if d['id'] in seen:
            continue
        anchor = DASH_PLACE_AFTER.get(d['id'])
        # After the anchor, and after the new ones already put there, so two
        # new widgets for one anchor keep their DASH_KINDS order.
        after = last.get(anchor, anchor)
        at = next((i for i, o in enumerate(out) if o['id'] == after), None)
        if at is None:
            out.append(d)
        else:
            out.insert(at + 1, d)
            last[anchor] = d['id']
        seen.add(d['id'])
    if version < 2:
        at = next((i for i, o in enumerate(out) if o['id'] == 'stage'), None)
        if at:
            out.insert(0, out.pop(at))
    return out


def system_badge(active: int, problems: int, fails: int) -> tuple[str, str]:
    """The System icon's badge: running jobs first (accent), else health
    problems (danger when anything failed, else warning), else none."""
    if active:
        return str(active), 'accent'
    if problems:
        return str(problems), 'danger' if fails else 'warning'
    return '', 'accent'


FAIL_SHOWN_HOURS = 24      # a failed job is "the one thing" for this long (w-165)


def _notes(n: int) -> str:
    return f"{n} note{'s' if n != 1 else ''}"


def system_status(jobs: list[dict], disks: list[dict] | None, checks: list[dict] | None,
                  proton: dict | None, crash: dict | None = None) -> dict:
    """The Home System widget from plain data (w-165): `line`, the one quiet
    line, while nothing needs you; else `attention`, the one thing with its one
    action, above the four `tiles`. `jobs` are {title, state, step, progress
    (pct|None), active} and, for a failed one, {name, kind, detail, age (s),
    retry (nothing newer ran the same command)}; `disks` as from --library;
    `checks` from --doctor; `proton` the Proton page's info ({} while loading,
    None if the check never ran); `crash` a --report-pending row. One thing at
    a time, in this order: a native crash (Nexus itself went down last time),
    a running job, a failed job, a disk over 90 %, a failing check, a Proton
    update. Warnings (a warn check, a disk over 80 %) stay in the line, as
    notes. `covers` is what For you now must then leave out (say it once)."""
    tiles, attention = [], None
    running = [j for j in jobs if j.get('active')]
    failed = [j for j in jobs if j.get('state') == 'failed']
    again = [j for j in failed if j.get('retry') and j.get('age') is not None
             and j['age'] <= FAIL_SHOWN_HOURS * 3600]
    if running:
        tiles.append(('activity', 'activity', f"{len(running)} running",
                      running[0].get('title') or '', 'accent'))
    elif failed:
        tiles.append(('activity', 'activity', f"{len(failed)} failed",
                      failed[-1].get('title') or '', 'danger'))
    else:
        tiles.append(('activity', 'activity', "Idle", "Nothing running", 'muted'))
    real = [d for d in disks or [] if d.get('total')]
    fullest = max(real, key=lambda d: d['used'] / d['total']) if real else None
    if fullest:
        used = fullest['used'] / fullest['total']
        name = Path(fullest.get('path') or '/').name or fullest.get('path') or '/'
        tiles.append(('storage', 'storage', f"{fmt_size(fullest['free'])} free",
                      f"{name} · {int(used * 100)}% used",
                      'danger' if used > 0.9 else 'warning' if used > DISK_FULL else 'text'))
    else:
        tiles.append(('storage', 'storage', "Measuring…", "Disk space", 'muted'))
    if not checks:
        tiles.append(('health', 'health', "Checking…", "Health", 'muted'))
        bad = []
    else:
        bad = sorted((c for c in checks if c.get('status') in ('fail', 'warn')),
                     key=lambda c: c['status'] != 'fail')
        if bad:
            fails = any(c['status'] == 'fail' for c in bad)
            tiles.append(('health', 'health', f"{len(bad)} issue{'s' if len(bad) != 1 else ''}",
                          bad[0].get('title') or '', 'danger' if fails else 'warning'))
        else:
            tiles.append(('health', 'health', "All good", f"{len(checks)} checks passed",
                          'success'))
    ge = ((proton or {}).get('families') or {}).get('ge') or {}
    if not has('proton'):               # no Proton page on Windows
        ge = {}
    elif not proton:
        tiles.append(('proton', 'proton', "Checking…", "Proton", 'muted'))
    elif ge.get('update'):
        tiles.append(('proton', 'proton', "Update", f"{ge.get('latest')} is out", 'warning'))
    else:
        tiles.append(('proton', 'proton', "Up to date", proton.get('default') or 'Proton',
                      'success'))
    if crash and crash.get('kind') == 'native-crash' and crash.get('id'):
        n = int(crash.get('count') or 1)
        attention = {'key': 'report', 'glyph': 'fail', 'role': 'danger',
                     'text': ("Nexus stopped answering" if crash.get('freeze') else
                              "Nexus closed unexpectedly") + (f" ({n} times)" if n > 1 else ""),
                     'sub': "Report it: you see every line before anything is sent",
                     'crash': crash['id'], 'label': "Report", 'covers': ()}
    elif running:
        j = running[0]
        pct = j.get('progress')
        attention = {'key': 'activity', 'glyph': 'spinner', 'role': 'accent',
                     'text': j.get('title') or '',
                     'sub': " · ".join(x for x in (f"{pct}%" if pct is not None else '',
                                                   j.get('step') or '') if x),
                     'label': "Show", 'covers': ('running',), 'job': j.get('job')}
    elif again:
        j = again[-1]
        what = j.get('name') or j.get('title') or ''
        why = j.get('detail') or str(j.get('step') or '').removeprefix("Failed \u2014 ")
        attention = {'key': 'activity', 'glyph': 'fail', 'role': 'danger',
                     'text': f"Install failed: {what}" if j.get('kind') == 'install'
                     else f"{j.get('title') or what} failed",
                     'sub': why, 'action': 'retry', 'label': "Retry", 'covers': (),
                     'job': j.get('job')}
    elif fullest and fullest['used'] / fullest['total'] > 0.9:
        attention = {'key': 'storage', 'glyph': 'storage', 'role': 'danger',
                     'text': f"The '{tiles[1][3].split(' · ')[0]}' drive is almost full",
                     'sub': f"{fmt_size(fullest['free'])} left — see what can go",
                     'label': "Review", 'covers': ('disk',)}
    elif bad and bad[0]['status'] == 'fail':
        attention = {'key': 'health', 'glyph': 'fail', 'role': 'danger',
                     'text': bad[0].get('title') or '', 'sub': bad[0].get('fix') or '',
                     'label': "Open Health", 'covers': ('health',)}
    elif ge.get('update'):
        attention = {'key': 'proton', 'glyph': 'download', 'role': 'warning',
                     'text': f"{ge.get('latest')} is out",
                     'sub': "Installs next to your current builds", 'action': 'install-ge',
                     'latest': ge.get('latest'), 'label': f"Install {ge.get('latest')}",
                     'covers': ('proton',)}
    line = None
    if attention is None:
        free = f" · {fmt_size(fullest['free'])} free" if fullest else ''
        if not checks:
            line = {'text': "Checking your setup…", 'role': 'muted', 'glyph': 'activity',
                    'extra': ''}
        else:
            notes = len(bad) + (1 if fullest and fullest['used'] / fullest['total'] > DISK_FULL
                                else 0)
            deft = proton.get('default') if has('proton') and proton else None
            line = {'text': "All good" + (f" · {_notes(notes)}" if notes else '') + free,
                    'role': 'warning' if notes else 'success',
                    'glyph': 'warn' if notes else 'check',
                    'extra': " · ".join(x for x in (deft, f"{len(checks) - len(bad)} checks "
                                                           "passed") if x)}
    return {'tiles': [dict(zip(('key', 'glyph', 'value', 'caption', 'role'), t))
                      for t in tiles],
            'attention': attention, 'line': line}


def system_state(hub) -> dict:
    """system_status() from what the window holds now (no refresh, no ask)."""
    proton = getattr(getattr(hub, 'protonpage', None), 'info', None)
    return system_status(job_facts(hub.jobs.jobs), hub._disks,
                         getattr(hub.health, 'checks', []), proton,
                         getattr(hub, '_native_crash', None))


def system_covers(hub) -> tuple:
    """What the System card says on Home right now, for For you now to leave
    out (say it once, w-165): () when the card is hidden or all is fine."""
    item = next((i for i in getattr(hub.prefs, 'dashboard', None) or ()
                 if i.get('id') == 'system'), None)
    if item is None or item.get('hidden'):
        return ()
    att = system_state(hub)['attention']
    return tuple(att['covers']) if att else ()


def dash_cols(width: int) -> int:
    # Three at most: cards stay readable and the default layout packs evenly.
    return 3 if width >= 1100 else 2 if width >= 700 else 1


def dash_grid(items, cols: int, skip=()) -> list[tuple[str, int, int, int, int]]:
    """Row-wise flow of the visible widgets: (id, row, col, span, rows). A
    widget that doesn't fit what's left of a row starts the next one, so the
    order you set is the order you read. A Tall one covers two rows; the
    cursor skips the cells under it and never goes back to fill holes. Tall
    is one row in a single column, and beside the Stage (w-91 fixes that
    row's height to the art)."""
    out, taken, row, col = [], set(), 0, 0
    srow = None
    for it in items:
        if it.get('hidden') or it['id'] in skip:
            continue
        span = min(DASH_SPAN.get(it['size'], 1), cols)
        while True:
            if col and col + span > cols:
                row, col = row + 1, 0
            if any((row, c) in taken for c in range(col, col + span)):
                col += 1
                if col >= cols:
                    row, col = row + 1, 0
                continue
            break
        if it['id'] == 'stage':
            srow = row
        rows = DASH_ROWS.get(it['size'], 1)
        if cols == 1 or (srow is not None and row == srow and it['id'] != 'stage'):
            rows = 1
        out.append((it['id'], row, col, span, rows))
        for r in range(row, row + rows):
            taken.update((r, c) for c in range(col, col + span))
        col += span
        if col >= cols:
            row, col = row + 1, 0
    return out


def dash_pack(items, cols: int, skip=()) -> list[tuple[str, int, int, int]]:
    """dash_grid without the row span: (id, row, col, span)."""
    return [(k, r, c, s) for k, r, c, s, _n in dash_grid(items, cols, skip)]


def dash_move(items, wid: str, direction: str, cols: int, skip=(), placed=None) -> list[dict]:
    """Move one widget: left/right swap it with its visible neighbour. Up/down
    with the last `placed` (the Packed grid, w-156) go before the card above it
    or after the card below it in its column; without it, to the row above/below
    (dash_grid), under the same column."""
    items = [dict(it) for it in items]
    vis = [i for i, it in enumerate(items) if not it.get('hidden') and it['id'] not in skip]
    pos = next((i for i in vis if items[i]['id'] == wid), None)
    if pos is None:
        return items
    k = vis.index(pos)
    if direction in ('left', 'right'):
        j = k - 1 if direction == 'left' else k + 1
        if 0 <= j < len(vis):
            a, b = vis[k], vis[j]
            items[a], items[b] = items[b], items[a]
        return items
    if placed:
        tid = move_target(placed, wid, direction)
        if tid is None or not any(it['id'] == tid for it in items):
            return items
        me = items.pop(pos)
        t = next(i for i, it in enumerate(items) if it['id'] == tid)
        items.insert(t if direction == 'up' else t + 1, me)
        return items
    packed = dash_grid(items, cols, skip)
    where = {i: (r, c, n) for i, r, c, _s, n in packed}
    r, c, n = where[wid]
    target_row = r - 1 if direction == 'up' else r + n
    # What covers that row (a tall from above counts), apart from my own row.
    row_items = [(i, cc, ss) for i, rr, cc, ss, nn in packed
                 if rr != r and rr <= target_row < rr + nn]
    if not row_items:
        return items
    # The widget in that row under our column (or the nearest one).
    tid = min(row_items, key=lambda t: 0 if t[1] <= c < t[1] + t[2] else
              min(abs(t[1] - c), abs(t[1] + t[2] - 1 - c)) + 1)[0]
    me = items.pop(pos)
    t = next(i for i, it in enumerate(items) if it['id'] == tid)
    items.insert(t if direction == 'up' else t + 1, me)
    return items


ADD_KEEP, ADD_END = None, '\x00end'     # dash_add's `before`: its saved place, or last


def dash_add(items, kind: str, size: str, before=ADD_KEEP) -> list[dict]:
    """The Customise gallery adds `kind` (w-158): shown, at `size` when its
    spec allows it (else the size it had, else its default), its other
    options kept. `before` an id on the list puts it there, ADD_END last;
    ADD_KEEP (or an id not on the list) leaves it where it was saved. A kind
    missing from the list joins it."""
    items = [dict(i) for i in items]
    pos = next((n for n, i in enumerate(items) if i['id'] == kind), None)
    if pos is None:
        it = next((dict(i) for i in dash_kind_defaults() if i['id'] == kind), None)
        if it is None:
            return items
        items.append(it)
        pos = len(items) - 1
    it = items[pos]
    sizes = DASH_SIZES.get(kind, ('small',))
    it['size'] = size if size in sizes else it.get('size') if it.get('size') in sizes \
        else DASH_DEFAULT_SIZE.get(kind, sizes[0])
    it['hidden'] = False
    if before == ADD_END:
        items.append(items.pop(pos))
    elif before is not None and before != kind and any(i['id'] == before for i in items):
        me = items.pop(pos)
        items.insert(next(n for n, i in enumerate(items) if i['id'] == before), me)
    return items


# --------------------------------------------------------------------------
# Dashboard — the Stage and glanceable widgets on Home, laid out by the user
# --------------------------------------------------------------------------

class ClickRow(QFrame):
    """A focusable, clickable row with a real layout (a QPushButton ignores
    its child layout's height, so wrapped text in it collapses)."""
    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("DashRow")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(ev)


class GlyphBadge(QWidget):
    """A widget's icon on a small gradient tile; round_ makes it a disc, for
    a capsule (For you now's pill) whose corners it must follow."""

    def __init__(self, glyph: str, grad: str, size: int = 28, round_: bool = False):
        super().__init__()
        self.glyph, self.grad, self.round = glyph, grad, round_
        self.setFixedSize(size, size)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        a, b = (QColor(c) for c in GRAD[self.grad])
        g.setColorAt(0.0, a)
        g.setColorAt(1.0, b)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QBrush(g))
        rad = r.height() / 2 if self.round else min(float(R['control']), r.height() / 2)
        pt.drawRoundedRect(r, rad, rad)
        f = QFont(FONTS.icon)
        f.setPixelSize(int(self.height() * 0.5))
        pt.setFont(f)
        pt.setPen(QColor(P['on_accent']))
        pt.drawText(r, Qt.AlignmentFlag.AlignCenter, self.glyph)
        pt.end()


SIZE_LABELS = {'small': "Small", 'tall': "Tall", 'wide': "Wide", 'full': "Full width",
               'cinematic': "Cinematic", 'banner': "Banner"}


class DashWidget(QFrame):
    """One dashboard card. Subclasses set the class attributes and fill
    `self.body` in fill(hub); `hub` is the main window (games, jobs, pages).
    Shelf widgets are unframed rows of tiles instead of cards."""
    KIND, TITLE, GLYPH, GRAD, LINK = '', '', '', 'dusk', None
    TITLES: dict = {}                     # a title of its own at some sizes (Friends at Small)
    FRAMED = True
    HEAD = True                           # a framed card's badge and title row
    go = pyqtSignal(str)                  # open a page
    edit = pyqtSignal(str, str)           # (kind, action) from the edit bar
    pressed = pyqtSignal(str)             # clicked in Customise: this card is the selected one (w-159)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DashCard" if self.FRAMED else "DashShelf")
        self.size_ = 'small'
        self.options: dict = {}           # its DASH_OPTIONS, set with the size by refresh
        self.more = None                  # a growing card's GrowHost (w-155)
        self.selected = False             # Customise: the card whose bar shows (w-159)
        self.editing = False
        self.shelf_label: str | None = None
        lay = QVBoxLayout(self)
        pad = SP['4'] if self.FRAMED else 0
        lay.setContentsMargins(pad, pad - 2 if pad else 0, pad, pad)
        lay.setSpacing(SP['2_5'])
        self.editbar = self._edit_bar()
        self.editbar.setVisible(False)
        lay.addWidget(self.editbar)
        self.title_label: QLabel | None = None
        self.head_lay: QHBoxLayout | None = None
        if self.FRAMED and self.HEAD:
            head = self.head_lay = QHBoxLayout()
            head.setSpacing(SP['2'])
            head.addWidget(GlyphBadge(self.GLYPH, self.GRAD))
            t = self.title_label = label(self.TITLE, "DashTitle")
            head.addWidget(t)
            head.addStretch()
            if self.LINK:
                more = self.open_btn = QPushButton("Open")
                more.setObjectName("Ghost")
                more.setCursor(Qt.CursorShape.PointingHandCursor)
                more.clicked.connect(lambda: self.go.emit(self.LINK))
                head.addWidget(more)
            lay.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(SP['1_5'])
        lay.addLayout(self.body)
        if self.FRAMED:
            lay.addStretch()

    def show_head(self, on: bool):
        """The badge, title and Open of a framed card (a quiet card has none)."""
        for i in range(self.head_lay.count() if self.head_lay is not None else 0):
            w = self.head_lay.itemAt(i).widget()
            if w is not None:
                w.setVisible(on)

    def _edit_bar(self):
        bar = QFrame()
        bar.setObjectName("DashEditBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(SP['2'], SP['1'], SP['1'], SP['1'])
        bl.setSpacing(SP['0_5'])                 # tight: three cards fit beside the panel (w-157)
        self.edit_name = label(self.TITLE, "StatLabel")
        # The name is there for room it finds: it never sets the card's least
        # width (the panel docked at Home's right leaves cards ~340 px, w-157).
        self.edit_name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bl.addWidget(self.edit_name, 1)
        for glyph, act, tip in ((G['arrow_left'], 'left', "Move left"), (G['arrow_up'], 'up', "Move up"),
                                (G['arrow_down'], 'down', "Move down"),
                                (G['arrow_right'], 'right', "Move right")):
            b = icon_button(glyph, tip, 28, "Ghost", round_=True)
            b.clicked.connect(lambda _=False, a=act: self.edit.emit(self.KIND, a))
            bl.addWidget(b)
        self.btn_size = QPushButton()             # cycles the sizes its spec allows
        self.btn_size.setObjectName("Ghost")
        self.btn_size.setToolTip("Change size")
        self.btn_size.clicked.connect(lambda: self.edit.emit(self.KIND, 'size'))
        bl.addWidget(self.btn_size)
        # More opens the Style, Size and Hide menu (a popover, Dashboard.more_menu).
        self.btn_more = QPushButton("More")
        self.btn_more.setObjectName("Ghost")
        self.btn_more.setToolTip("Style, size and hide")
        set_glyph_icon(self.btn_more, G['chevron_down'], 14)
        self.btn_more.clicked.connect(lambda: self.edit.emit(self.KIND, 'more'))
        bl.addWidget(self.btn_more)
        return bar

    def set_editing(self, on: bool, size: str, shelf: str | None = None):
        self.size_ = size
        self.editing = on
        self.editbar.setVisible(on and self.selected)
        title = self.title_for(size)
        self.edit_name.setText(title)
        if self.title_label is not None:
            self.title_label.setText(title)
        self.shelf_label = shelf
        sizes = DASH_SIZES.get(self.KIND, ('small',))
        self.btn_size.setVisible(len(sizes) > 1)
        self.btn_size.setText(SIZE_LABELS[size])
        self.setProperty('editing', on)
        self.setProperty('selected', on and self.selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def title_for(self, size: str) -> str:
        return self.TITLES.get(size, self.TITLE)

    def mousePressEvent(self, ev):
        if self.editing:
            self.pressed.emit(self.KIND)
        super().mousePressEvent(ev)

    def set_density(self, d: dict):
        """Home's density (DASH_DENSITY): a card's padding."""
        if self.FRAMED:
            pad = d['card_pad']
            self.layout().setContentsMargins(pad, pad - 2, pad, pad)

    def bind(self, hub):
        """Wire to the window once, when the dashboard is built (the Stage
        hooks into Home's view); most widgets need nothing."""

    def has_content(self, hub) -> bool:
        """Shelf widgets with nothing to show stay out of the layout."""
        return True

    def fill(self, hub):
        pass

    def why(self, hub) -> str:
        """Why the Customise gallery recommends this widget (w-158), from
        data already in hand (never an ask): '' when there is no reason."""
        return ''

    def needs_note(self, hub) -> str:
        """What the Customise gallery says instead of the note while this card
        cannot show (w-163: "Needs Game-db · Open Settings"); '' when nothing is
        missing."""
        return ''

    def preview_seed(self, hub):
        """The Customise gallery's preview (w-158): before fill, put in what
        fill would otherwise ask the CLI for, from `hub.sample`. Most widgets
        read only the hub and need nothing."""

    def clear(self):
        clear_layout(self.body)
        self.more = None
        self.layout().setStretchFactor(self.body, 0)

    @property
    def grows(self) -> bool:
        """Shows more rows when the grid gives it more height (w-155)."""
        spec = SPEC[self.KIND]
        return spec.grows and (not spec.grow_sizes or self.size_ in spec.grow_sizes)

    def add_more(self, pool, build, cols: int = 1, col_gap: int = 0, on_count=None):
        """After the base rows: the rest of `pool` (built by `build(item)`)
        as far as the card's given height lets whole rows show. A GrowHost
        has no size of its own, so this never changes the card's natural
        height (the grid would measure, grow and loop)."""
        if not (self.grows and pool):
            return None
        host = self.more = GrowHost(pool, build, cols, self.body.spacing(), col_gap, on_count)
        self.body.addWidget(host, 1)
        self.layout().setStretchFactor(self.body, 1)       # the body takes the spare height
        return host

    def _error(self, message: str, retry) -> bool:
        """A failed load: say why, with Retry (instead of "Loading…" that
        never ends). False when there's no error to show."""
        if not message:
            return False
        self.body.addWidget(label(f"Couldn't load — {message}", "Muted", wrap=True))
        b = QPushButton("Retry")
        b.clicked.connect(retry)
        self.body.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        return True

    # small row helpers shared by the card widgets
    def row(self, glyph: str, text: str, sub: str = '', color: str | None = None,
            on_click=None) -> QWidget:
        w = ClickRow() if on_click else QWidget()
        if on_click:
            w.clicked.connect(on_click)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        lay.setSpacing(SP['2'])
        ic = icon_label(glyph, 13, color or P['muted'])
        ic.setFixedWidth(18)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = label(text, None, wrap=True)
        t.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
        t.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        col.addWidget(t)
        if sub:
            sl = label(sub, "Muted", wrap=True)
            sl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            col.addWidget(sl)
        lay.addLayout(col, 1)
        return w

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if not self.FRAMED:
            return
        # A soft glow of the widget's gradient in its top-left corner.
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), R['card'], R['card'])
        pt.setClipPath(clip)
        g = QRadialGradient(0.0, 0.0, float(max(160, self.width() * 0.6)))
        c = QColor(GRAD[self.GRAD][0]); c.setAlphaF(0.10)
        e = QColor(c); e.setAlphaF(0.0)
        g.setColorAt(0.0, c)
        g.setColorAt(1.0, e)
        pt.fillRect(self.rect(), QBrush(g))
        pt.end()


class ShelfWidget(DashWidget):
    """A row of game tiles (Recently installed, Favourites, …)."""
    FRAMED = False
    KIND_TILE = 'cover'
    LINK_CHIP = None                      # (chip, sort) for "See all"

    def games(self, hub) -> list[dict]:
        return []

    def has_content(self, hub):
        return bool(self.games(hub))

    def fill(self, hub):
        view = hub.homeview
        for t in getattr(self, '_tiles', ()):           # forget the old row's tiles
            ts = view.tiles.get(t.game.get('slug'), [])
            if t in ts:
                ts.remove(t)
        self.clear()
        self._tiles = []
        self.row = None
        games = self.games(hub)
        if games:
            row = self.row = view.make_shelf(self.TITLE, games, self.KIND_TILE, self.LINK_CHIP)
            self._tiles = row.tiles
            self.body.addWidget(row)


def _visible_games(hub):
    """What Home and its widgets are about: installed, not hidden, and one
    copy of a game you have in several stores."""
    return [g for g in hub._games if not g.get('hidden') and is_installed(g)
            and not g.get('dup_of')]


DUP_ORDER = ('nexus', 'steam', 'heroic', 'epic')


def _game_store_id(g: dict) -> int | None:
    if g.get('source') == 'steam' and str(g.get('appid', '')).isdigit():
        return int(g['appid'])
    # A store source (nexus/sources) that knows the game's Steam appid.
    if str(g.get('store_appid') or '').isdigit() and 0 < int(g['store_appid']) < 0x80000000:
        return int(g['store_appid'])
    v = str(g.get('umu_gameid') or '').removeprefix('umu-')
    return int(v) if v.isdigit() and 0 < int(v) < 0x80000000 else None


def mark_duplicates(games: list[dict], enabled: bool = True) -> None:
    """The same game from more than one source (a repack and a Steam copy,
    an Epic and a Steam one…) keeps one tile: installed beats owned, then
    repack > Steam > Heroic > Epic, then more play time. The others get
    `dup_of` (the kept slug); the kept one lists the others' sources in
    `dup_also`. Matched by Steam store id (a source's `store_appid` counts),
    else by name."""
    for g in games:
        g.pop('dup_of', None)
        g.pop('dup_also', None)
    if not enabled:
        return
    groups: dict = {}
    for g in games:
        sid = _game_store_id(g)
        name = _search_norm(g.get('store_name') or g.get('display_name') or '')
        key = ('id', sid) if sid else ('name', name) if name else None
        if key:
            groups.setdefault(key, []).append(g)
    # A store id and a name can describe the same game: fold name-only
    # groups into the id group with that name.
    by_name = {}
    for key, members in list(groups.items()):
        if key[0] == 'id':
            for m in members:
                by_name.setdefault(_search_norm(m.get('store_name') or m.get('display_name')
                                                or ''), key)
    for key in [k for k in groups if k[0] == 'name']:
        if key[1] in by_name and by_name[key[1]] in groups:
            groups[by_name[key[1]]] += groups.pop(key)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda g: (not is_installed(g),
                                    DUP_ORDER.index(g.get('source', 'nexus'))
                                    if g.get('source', 'nexus') in DUP_ORDER else 9,
                                    -(g.get('playtime_minutes') or 0)))
        keep = members[0]
        keep['dup_also'] = sorted({m.get('source', 'nexus') for m in members[1:]}
                                  - {keep.get('source', 'nexus')})
        for m in members[1:]:
            m['dup_of'] = keep['slug']


class StageArt(FollowHero):
    """The Stage's art (w-85): one game's art, its logo, facts and Play,
    moved only by the Continue row (focus, or the mouse resting on a tile).
    A running or starting game is pinned with "Now playing". Its size sets
    the logo's height; Banner has no fact pills. Its background (w-90) is the
    game's art, the accent gradient or Plain (the `bgmode` property: art,
    gradient, surface); off the art the ink follows (theme.py) and the title
    shows in words instead of the logo."""
    LOGO_H = {'cinematic': 96, 'wide': 96, 'banner': 80}

    def __init__(self):
        super().__init__()
        self.setObjectName("StageArt")
        self.setProperty('bgmode', 'art')
        self.setMinimumHeight(0)
        # Cinematic holds the Continue row too: less air above, so it all
        # fits the 480 px floor (stage_height) without squeezing the row.
        m = self.layout().contentsMargins()
        self.layout().setContentsMargins(m.left(), SP['5'], m.right(), SP['5'])
        self.size_ = 'cinematic'
        self.pin_slug: str | None = None         # Pinned art's game (its kicker says so)
        self.pick_slug: str | None = None        # Pick for me's game (w-91) and its kicker
        self.pick_text = ''
        self.kicker.setTextFormat(Qt.TextFormat.RichText)   # live_kicker's &nbsp;

    def set_size(self, size: str):
        if size == self.size_:
            return
        self.size_ = size
        if self.games:
            self._show(animate=False)

    def on_art(self) -> bool:
        return self.property('bgmode') == 'art'

    def set_bg(self, bg: str):
        """Game art and Pinned art paint the art; 'gradient' and 'surface'
        paint a fill. The ink is in the stylesheet (#StageArt[bgmode=…]): an
        ancestor property, so every child is polished again (w-55)."""
        mode = bg if bg in ('gradient', 'surface') else 'art'
        if self.property('bgmode') == mode:
            return
        self.setProperty('bgmode', mode)
        for w in (self, *self.findChildren(QWidget)):
            w.style().unpolish(w)
            w.style().polish(w)
        set_glyph_icon(self.btn, G['play'], int(20 * self.scale),
                       'accent' if mode == 'gradient' else 'on_accent', hover_role=None)
        if self.games:
            self._show(animate=False)
        self.update()

    def _show(self, animate: bool):
        g = self.game
        # Disabling the focused Play (its game just started) would throw focus
        # somewhere else on the page: Details takes it first.
        if self.btn.hasFocus() and not play_enabled(g):
            self.btn_details.setFocus(Qt.FocusReason.OtherFocusReason)
        super()._show(animate)
        if self.is_pinned():
            self.kicker.setText(live_kicker(g))
        elif self.pick_slug and g.get('slug') == self.pick_slug:
            self.kicker.setText(html.escape(self.pick_text))     # rich text: a title may hold <
        elif self.pin_slug and g.get('slug') == self.pin_slug:
            self.kicker.setText(f"PINNED  ·  {self.kicker.text()}")
        if not self.on_art():
            # A logo is drawn for art (white, mostly): on a light Plain it
            # would vanish, so off the art the title shows in words.
            self.logo.setVisible(False)
            self.title.setVisible(True)

    def _tint(self, path):
        return super()._tint(path) if self.on_art() else None

    def tick(self):
        """The minute changed: "NOW PLAYING · 42 MIN" moves on."""
        if self.is_pinned():
            self.kicker.setText(live_kicker(self.game))

    def _fill_facts(self, g: dict):
        if self.size_ == 'banner':
            clear_layout(self.facts)
            return
        super()._fill_facts(g)

    def _fact(self, text: str, color: str | None) -> QLabel:
        """A QLabel#StageFact: its colours follow the background (theme.py);
        a coloured fact (ProtonDB tier, Update ready) sets only its text colour."""
        lab = label(text, "StageFact")
        if color:
            lab.setStyleSheet(f"color: {color};")
        lab.setFixedHeight(scaled(STAGE_FACT_H))
        return lab

    def _logo(self, path):
        h = self.LOGO_H.get(self.size_, 96)
        try:
            key = (path, Path(path).stat().st_mtime, self.scale, h)
        except OSError:
            return QPixmap()
        if key not in self._logos:
            pm = QPixmap(path)
            if not pm.isNull():
                pm = pm.scaled(h * 3, h, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            if len(self._logos) > 8:
                self._logos.clear()
            self._logos[key] = pm
        return self._logos[key]

    def paintEvent(self, ev):
        mode = self.property('bgmode')
        if mode == 'art':
            super().paintEvent(ev)
            return
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, R['panel'], R['panel'])
        if mode == 'gradient':
            g = QLinearGradient(0.0, 0.0, r.width(), 0.0)
            a, b = (QColor(x) for x in GRAD['dusk'])
            g.setColorAt(0.0, a)
            g.setColorAt(1.0, b)
            pt.fillPath(path, QBrush(g))
        else:
            pt.fillPath(path, QColor(P['surface']))
            pt.setPen(QPen(QColor(P['border_card']), 1))
            pt.drawPath(path)
        pt.end()


class StageWidget(DashWidget):
    """The top of Home (w-85): one game's art at Cinematic, Wide or Banner
    size, and a Continue row of the most recently played games. Focus on a
    tile (pad, keyboard) or the mouse resting on one moves the stage there;
    nothing moves on its own. The row lies on the art at Cinematic with the
    Row on art style and under it otherwise (Clean, Wide, Banner). It's kept
    across fills, so moving the stage only repaints the art and never takes
    focus off the row. Pinned art (w-90) keeps one game on it: the row stops
    moving it while that game is in the library, and a running game still
    wins."""
    KIND, TITLE = 'stage', "Stage"
    FRAMED = False
    moved = pyqtSignal()                  # the stage shows another game (For you now asks again)
    FOCUS_DWELL, HOVER_DWELL = 140, 350
    RECENT = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.art = StageArt()
        self.body.addWidget(self.art)
        self.row: 'ShelfRow | None' = None
        self._hub = None
        self._view = None
        self._vh = 850                           # the Home viewport's height
        self._placed = None                      # what the parts are laid out for
        self.style_, self.bg = 'row', 'art'
        self.pin_slug: str | None = None         # Pinned art's slug, as saved
        self._pin_game: dict | None = None       # ... while that game is in the library
        self.picked: dict | None = None          # Pick for me's game, while it's on the stage
        self.pick_vibe = 'any'
        self.on_height = None                    # called when the art's height changes
        self.density = DASH_DENSITY['comfortable']
        self._dwell = Dwell(self.FOCUS_DWELL, self._follow, self)
        self._clock = QTimer(self)               # Now playing's minutes, while pinned
        self._clock.setInterval(20000)
        self._clock.timeout.connect(self.art.tick)

    def bind(self, hub):
        self._hub = hub
        view = self._view = hub.homeview
        self.art.play.connect(view.play)
        self.art.open.connect(view.activated)
        self.art.menu.connect(self._menu)
        row = self.row = view.make_shelf("Continue", [], 'wide')
        self.art.layout().addWidget(row)         # parented before anything shows it
        row.focused.connect(self._row_focus)
        row.hovered.connect(self._row_hover)
        row.unhovered.connect(self._row_unhover)
        view.viewport().installEventFilter(self)
        self.art.installEventFilter(self)
        self._place(self.size_ if self.size_ in DASH_SIZES['stage'] else 'cinematic')

    def has_content(self, hub):
        return bool(_visible_games(hub))

    def fill(self, hub):
        games = _visible_games(hub)
        self.style_ = self.options.get('style', 'row')
        self.bg = self.options.get('bg', 'art')
        it = next((i for i in hub.prefs.dashboard if i.get('id') == 'stage'), {})
        self.pin_slug = stage_pin(it.get('pin')) if self.bg == 'pinned' else None
        self._pin_game = next((g for g in games if g.get('slug') == self.pin_slug),
                              None) if self.pin_slug else None
        # A pinned game that's gone keeps its pin; the row moves the stage again.
        self.art.pin_slug = self.pin_slug if self._pin_game is not None else None
        self._place(self.size_)
        self.art.set_bg(self.bg)
        shown = featured_games(games, max(1, len(games)))
        if self.picked is not None:
            # A live pick stays (and beats the pin) until it starts or is gone.
            fresh = next((g for g in games if g.get('slug') == self.picked.get('slug')), None)
            if fresh is None or fresh.get('playing') or fresh.get('launching'):
                self._drop_pick()
            else:
                self.picked = fresh
        top = self.picked or self._pin_game
        if top is not None:
            shown = [top] + [g for g in shown if g is not top]
        self.art.set_games(shown)
        if top is not None:
            self.art.show_game(top)              # nothing while a game runs (Now playing)
        self.art.setVisible(bool(self.art.games))
        if self.art.is_pinned():
            if not self._clock.isActive():
                self._clock.start()
        else:
            self._clock.stop()
        if self.row is None:
            return
        recent = sorted((g for g in games if g.get('last_played')),
                        key=lambda g: g['last_played'], reverse=True)[:self.RECENT]
        view, row = hub.homeview, self.row
        view._untrack(row)
        row.set_games(recent, lambda g: view._tile(g, row.tile_w, row.kind))
        view._track(row)
        row.setVisible(bool(recent))

    def set_editing(self, on: bool, size: str, shelf: str | None = None):
        super().set_editing(on, size, shelf)
        self._place(size)

    def set_density(self, d: dict):
        self.density = d
        self.art.layout().setContentsMargins(d['art_side'], d['art_tb'], d['art_side'],
                                             d['art_tb'])
        self._placed = None
        self._place(self.size_)

    # ---- Pick for me (w-91) ----
    def show_pick(self, g: dict, vibe: str = 'any') -> bool:
        """Spin: the pick takes the stage, even over Pinned art (whose pin
        is never written and comes back when you leave Home); a running game
        still wins. It launches nothing. False when it can't show."""
        if not g.get('slug') or self.art.pinned is not None:
            return False
        before = self.art.game.get('slug')
        self.picked, self.pick_vibe = g, vibe if vibe in VIBE_LABELS else 'any'
        pin = self._pin_game
        self.art.pick_slug = g['slug']
        self.art.pick_text = pick_kicker(self.pick_vibe, (pin.get('display_name') or pin['slug'])
                                         if pin is not None else None)
        if g['slug'] == before:
            self.art._show(animate=False)        # the kicker says it's a pick now
        else:
            self.art.show_game(g)
        self.moved.emit()                        # For you now drops it; Your save follows
        return True

    def pin_title(self) -> str | None:
        """The pinned game's title while a pick covers it."""
        pin = self._pin_game
        return (pin.get('display_name') or pin['slug']) if pin is not None and self.picked \
            else None

    def _drop_pick(self) -> bool:
        had = self.picked is not None
        self.picked, self.art.pick_slug, self.art.pick_text = None, None, ''
        return had

    def clear_pick(self):
        """The pick ends (leaving Home): the stage goes back to what it
        showed, a pin included."""
        if not self._drop_pick():
            return
        if self._hub is not None:
            self.art.games, self.art.i = [], 0   # not "keep the game shown": start over
            self.fill(self._hub)
        self.moved.emit()

    # ---- pinned art (w-90) ----
    def pinned(self) -> bool:
        """Pinned art and its game shown: the row doesn't move the stage."""
        return self._pin_game is not None

    def pin(self, g: dict):
        """Pin to Stage (the ⋮ menu): Pinned art with this game."""
        if g.get('slug'):
            self._set_bg('pinned', g['slug'])

    def unpin(self):
        """Unpin from Stage: back to Game art, following the row."""
        self._set_bg('art', None)

    def _set_bg(self, bg: str, pin: str | None):
        hub = self._hub
        if hub is None:
            return
        items = [dict(i) for i in hub.prefs.dashboard]
        it = next((i for i in items if i.get('id') == 'stage'), None)
        if it is None:
            return
        it['bg'] = bg
        if pin:
            it['pin'] = pin
        else:
            it.pop('pin', None)
        hub.prefs.dashboard = items
        hub.prefs.save()
        hub.dashboard.refresh(['stage'])

    def menu_stage(self, game: dict) -> str | None:
        """What the ⋮ menu offers for the Stage: 'unpin' while the pinned
        game shows, else 'pin'; nothing on the gradient or Plain."""
        if self.bg not in ('art', 'pinned') or not game.get('slug'):
            return None
        return 'unpin' if self.pinned() and game.get('slug') == self.pin_slug else 'pin'

    def _menu(self, game: dict, pos):
        hub = self._hub
        if hub is not None:
            hub._show_menu(game, pos, stage=self.menu_stage(game))

    # ---- size, style ----
    def on_art(self, size: str | None = None) -> bool:
        """The Continue row lies on the art: Cinematic with Row on art."""
        return (size or self.size_) == 'cinematic' and self.style_ == 'row'

    def _place(self, size: str):
        """The row on the art or under it, its fades and title ink; the
        art's height. Moving the row keeps pad focus on it (w-85)."""
        self.size_ = size
        on_art = self.on_art(size)
        key = (size, on_art, self.style_, self.bg, id(self.density))
        if key != self._placed and self.row is not None:
            self._placed = key
            row, d = self.row, self.density
            f = QApplication.focusWidget()
            had = f if f is not None and row.isAncestorOf(f) else None
            if row.parentWidget() is not (self.art if on_art else self):
                (self.body if on_art else self.art.layout()).removeWidget(row)
                (self.art.layout() if on_art else self.body).addWidget(row)
                row.show()                       # now, not a pass later (w-57)
            clean = size == 'cinematic' and not on_art
            gap = d['row_gap'] if on_art else \
                max(0, d['clean_gap'] - self.body.spacing()) if clean else SP['2']
            row.layout().setContentsMargins(0, gap, 0, 0)
            for side, fade in row.fades.items():
                fade.grad = None
                if not on_art:
                    fade.role = 'bg'             # a shelf on the page
                elif self.bg == 'gradient':
                    fade.role, fade.grad = 'accent', ('dusk', 0 if side == 'left' else 1)
                else:
                    fade.role = 'surface' if self.bg == 'surface' else 'scrim'
                fade.update()
            for lab in (row.name_label, row.count_label):
                if lab is not None:
                    lab.style().unpolish(lab)
                    lab.style().polish(lab)
            if had is not None and alive(had):
                had.setFocus(Qt.FocusReason.OtherFocusReason)
        self.art.set_size(size)
        self._sync_height()

    def _sync_height(self):
        h = stage_height(self.size_, self._vh, self.style_, self.bg)
        lay = self.art.layout()
        h = max(h, lay.minimumSize().height())
        if h != self.art.minimumHeight() or h != self.art.maximumHeight():
            self.art.setFixedHeight(h)
            if self.on_height is not None:
                self.on_height()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and self._view is not None \
                and obj is self._view.viewport():
            self._vh = ev.size().height()
            self._sync_height()
        elif ev.type() == QEvent.Type.LayoutRequest and obj is self.art:
            # A logo that arrives later (or Appearance → Size) can need more
            # than the height it has: grow rather than squeeze the row.
            self._sync_height()
        return False

    # ---- the row moves the stage ----
    def _row_focus(self, tile):
        if not self.pinned():
            self._dwell.push(tile.game, self.FOCUS_DWELL)

    def _row_hover(self, tile):
        if not self.pinned():
            self._dwell.push(tile.game, self.HOVER_DWELL)

    def _row_unhover(self, tile):
        if self._dwell.timer.isActive() and self._dwell.value is tile.game:
            self._dwell.cancel()

    def _follow(self, g):
        if g is not None and alive(self) and not self.pinned():
            before = self.art.game.get('slug')
            picked = self._drop_pick()           # the row moving the stage ends a pick
            self.art.show_game(g)
            if picked and self.art.game.get('slug') == before:
                self.art._show(animate=False)    # same game: just its kicker
            if picked or self.art.game.get('slug') != before:
                self.moved.emit()


class InstalledWidget(ShelfWidget):
    KIND, TITLE, LINK_CHIP = 'installed', "Recently installed", ('all', 'added')

    def games(self, hub):
        return sorted((g for g in _visible_games(hub) if g.get('added_at')),
                      key=lambda g: g['added_at'], reverse=True)[:12]

    def fill(self, hub):
        super().fill(hub)
        for t in self._tiles:            # never played yet: what "Not played yet" was for
            t.mark_new = not t.game.get('last_played')
            t.update()


class FavouritesWidget(ShelfWidget):
    KIND, TITLE, LINK_CHIP = 'favourites', "Favourites", ('favorite', 'name')

    def games(self, hub):
        return [g for g in _visible_games(hub) if g.get('favorite')]


class CollectionsWidget(ShelfWidget):
    """One row per collection that has an installed game in it."""
    KIND, TITLE = 'collections', "Collections"

    def games(self, hub):
        return [g for g in _visible_games(hub) if g.get('collections')]

    def fill(self, hub):
        view = hub.homeview
        for t in getattr(self, '_tiles', ()):
            ts = view.tiles.get(t.game.get('slug'), [])
            if t in ts:
                ts.remove(t)
        self.clear()
        self._tiles = []
        games = _visible_games(hub)
        for name in COLLECTION_NAMES:
            items = [g for g in games if name in (g.get('collections') or ())]
            if items:
                row = view.make_shelf(name, items, self.KIND_TILE, (f'col:{name}', 'name'),
                                      look=collection_look(name))
                self._tiles += row.tiles
                self.body.addWidget(row)


class SmartShelfWidget(ShelfWidget):
    """One smart shelf on Home (nexus/smart.py), "Unfinished" unless chosen
    otherwise in Customise. Gone while that shelf is empty."""
    KIND, TITLE = 'smart', "Smart shelf"

    def key(self, hub) -> str:
        """`smart:<rule>`, or `user:<id>` while that shelf of yours exists."""
        it = next((i for i in hub.prefs.dashboard if i['id'] == self.KIND), None)
        shelf = _dash_shelf((it or {}).get('shelf'))
        if smart.is_user(shelf):
            if _user_shelf(hub, shelf):
                return shelf
            shelf = smart.WIDGET_DEFAULT
        return f'smart:{shelf}'

    def games(self, hub):
        get = getattr(hub, '_smart_facts', None)
        facts, key = get() if get else {}, self.key(hub)
        if smart.is_user(key):
            return smart.user_games(_user_shelf(hub, key), _visible_games(hub), facts,
                                    friends=FRIENDS.public_ids())[:24]
        if key == 'smart:owned':              # the games you do not have installed (w-166)
            pool = [g for g in hub._games if not g.get('hidden') and not g.get('dup_of')]
        else:
            pool = _visible_games(hub)
        return smart.games_for(key, pool, facts, linux=has('proton'))[:24]

    def fill(self, hub):
        view = hub.homeview
        for t in getattr(self, '_tiles', ()):
            ts = view.tiles.get(t.game.get('slug'), [])
            if t in ts:
                ts.remove(t)
        self.clear()
        self._tiles = []
        games = self.games(hub)
        if games:
            key = self.key(hub)
            mine = _user_shelf(hub, key)
            row = view.make_shelf(smart.title_of(mine) if mine else smart.label(key), games,
                                  self.KIND_TILE, smart=key,
                                  tip=smart.describe(mine, names=FRIENDS.names()) if mine
                                  else smart.tip(key))
            row.btn_menu.hide()                # hide/save live in the Library; Customise here
            self._tiles = row.tiles
            self.body.addWidget(row)


class PickWidget(DashWidget):
    """Pick for me (w-91, replaced Surprise me): vibe chips (only the vibes
    that have games; Any alone hides the row) and Spin, which puts a pick on
    the Stage with a "PICKED FOR YOU" kicker, so Play is one more press. The
    chip is the item's `vibe`. Without the Stage, Spin opens a game page as
    Surprise me did (MainWindow.spin)."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'pick', "Pick for me", G['dice'], 'dawn', None
    spun = pyqtSignal(str)                # the vibe to spin

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hub = None
        self.chips = QWidget()
        self.chips.setObjectName("PickVibes")
        self.flow = FlowLayout(self.chips, margin=0, spacing=SP['1_5'])
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.body.addWidget(self.chips)
        self.body.addSpacing(SP['1'])
        self.spinner = QWidget()          # Spin and its note (not in style Today's pick)
        row = QHBoxLayout(self.spinner)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SP['3'])
        self.btn = QPushButton("Spin")
        self.btn.setObjectName("Primary")
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        set_glyph_icon(self.btn, G['dice'], 16, 'on_accent', hover_role=None)
        self.btn.clicked.connect(self._spin)
        row.addWidget(self.btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.note = plain_label("", "Muted", wrap=True)
        row.addWidget(self.note, 1, Qt.AlignmentFlag.AlignVCenter)
        self.body.addWidget(self.spinner)
        from .insight_widgets import TodayBlock     # (it builds on DashWidget: late import)
        self.today = TodayBlock()         # style 'today': Game of the day (w-164)
        self.today.setVisible(False)
        self.body.addWidget(self.today)
        self.vibe = 'any'
        self.pools: dict[str, list[dict]] = {}
        self._sig = None                  # the chips drawn (the vibes that have games)
        self.wheel: PickWheel | None = None     # Wheel style (w-142): built on first use
        self._wheel_sig = None            # the pool the wheel shows

    def bind(self, hub):
        self._hub = hub
        spin = getattr(hub, 'spin', None)
        if spin is not None:
            self.spun.connect(spin)

    @staticmethod
    def pools_of(hub) -> dict[str, list[dict]]:
        get = getattr(hub, '_smart_facts', None)
        return vibe_pools(_visible_games(hub), get() if get else {}, linux=has('proton'))

    def has_content(self, hub):
        if self.options.get('style') == 'today':
            return self.today.has_content(hub)
        return bool(_pickable(_visible_games(hub)))     # cheap: asked on every layout

    def chip(self, vibe: str) -> QPushButton | None:
        return next((b for b in self.group.buttons() if b.property('vibe') == vibe), None)

    def fill(self, hub):
        today = self.options.get('style') == 'today'
        self.today.setVisible(today)
        self.spinner.setVisible(not today)
        if today:
            self.chips.setVisible(False)
            if self.wheel is not None:
                self.wheel.setVisible(False)
            self.today.render_for(hub)
            return
        self.pools = pools = self.pools_of(hub)
        vibe = self.options.get('vibe', 'any')
        self.vibe = vibe if vibe in pools else 'any'
        self._fill_wheel()
        sig = tuple(pools)
        if sig != self._sig:              # same vibes: keep the chips (and pad focus)
            self._sig = sig
            f = QApplication.focusWidget()
            had = f.property('vibe') if f is not None and self.chips.isAncestorOf(f) else None
            if had is not None:
                self.btn.setFocus(Qt.FocusReason.OtherFocusReason)   # before the chip goes
            while self.flow.count():
                w = self.flow.takeAt(0).widget()
                if w is not None:
                    self.group.removeButton(w)
                    w.hide()
                    w.deleteLater()
            for v in pools:
                c = QPushButton(VIBE_LABELS[v])
                c.setObjectName("Chip")
                c.setCheckable(True)
                c.setCursor(Qt.CursorShape.PointingHandCursor)
                c.setProperty('vibe', v)
                c.setToolTip("Any game you have installed" if v == 'any'
                             else smart.tip(f'smart:{v}'))
                c.clicked.connect(lambda _=False, v=v: self._choose(v))
                self.group.addButton(c)
                self.flow.addWidget(c)
                c.show()
            back = self.chip(had) if had is not None else None
            if back is not None:
                back.setFocus(Qt.FocusReason.OtherFocusReason)
        self.chips.setVisible(len(pools) > 1)
        for b in self.group.buttons():
            b.setChecked(b.property('vibe') == self.vibe)
        stage = getattr(getattr(hub, 'dashboard', None), 'stage', None)
        picked = stage.picked if stage is not None else None
        run = next((g for g in getattr(hub, '_games', ()) if g.get('playing')
                    or g.get('launching')), None)
        if run is not None:
            name = run.get('display_name') or run.get('slug') or ''
            # A pad can't focus a disabled button, so the note says it too.
            text = f"Spin is back when {name} closes."
            if self.btn.hasFocus():
                (self.chip(self.vibe) or self.btn).setFocus(Qt.FocusReason.OtherFocusReason)
            self.btn.setEnabled(False)
            self.btn.setToolTip(text)
            self.btn.setText("Spin")
            self.note.setText(text)
            return
        self.btn.setEnabled(bool(pools) and not (self.wheel and self.wheel.spinning))
        self.btn.setToolTip("Put a game on the Stage")
        self.btn.setText("Spin again" if picked else "Spin")
        if picked:
            text = f"On the Stage: {picked.get('display_name') or picked.get('slug')}"
            pin = stage.pin_title()
            self.note.setText(text + (f". {pin} is back when you leave Home." if pin else ''))
        else:
            self.note.setText(pick_note(self.vibe, len(pools.get(self.vibe, ()))))

    def _choose(self, vibe: str):
        """A chip: remember the vibe (the item's `vibe`) and say how many."""
        hub = self._hub
        if hub is None:
            return
        items = [dict(i) for i in hub.prefs.dashboard]
        it = next((i for i in items if i.get('id') == self.KIND), None)
        if it is None or it.get('vibe') == vibe:
            return
        with hub.dashboard._undoable():       # Customise's Undo covers it (w-159)
            it['vibe'] = vibe
            hub.prefs.dashboard = items
        hub.prefs.save()
        hub.dashboard.refresh([self.KIND])

    def _fill_wheel(self):
        """Wheel style: the wheel shows the vibe's games; Chips: no wheel."""
        if self.options.get('style') != 'wheel':
            if self.wheel is not None:
                self.wheel.setVisible(False)
            return
        if self.wheel is None:
            self.wheel = PickWheel()
            self.body.insertWidget(0, self.wheel, 0, Qt.AlignmentFlag.AlignHCenter)
        self.wheel.setVisible(True)
        pool = self.pools.get(self.vibe) or self.pools.get('any') or []
        sig = (self.vibe, tuple(g.get('slug') for g in pool[:SEGMENTS]))
        if sig != self._wheel_sig:        # a landed wheel stays until the pool moves
            self._wheel_sig = sig
            self.wheel.set_names([_name_of(g) for g in pool[:SEGMENTS]])

    def _spin(self):
        pick_for = getattr(self._hub, 'pick_for', None)
        if self.options.get('style') != 'wheel' or self.wheel is None or pick_for is None:
            self.spun.emit(self.vibe)
            return
        if self.wheel.spinning:
            return
        g = pick_for(self.vibe)           # decided first; the wheel only animates to it
        if g is None:
            return
        pool = self.pools.get(self.vibe) or self.pools.get('any') or []
        rest = [x for x in pool if x.get('slug') != g.get('slug')]
        random.shuffle(rest)
        segs = rest[:SEGMENTS - 1]
        at = random.randrange(len(segs) + 1)
        segs.insert(at, g)
        self._wheel_sig = None
        self.wheel.set_names([_name_of(x) for x in segs])
        self.btn.setEnabled(False)
        self.wheel.spin_to(at, lambda: self._landed(g))

    def _landed(self, g: dict):
        self.btn.setEnabled(True)
        hub = self._hub
        if hub is not None and getattr(hub, 'spin', None) is not None:
            hub.spin(self.vibe, g)

    def keyPressEvent(self, ev):
        """Enter on a chip or Spin (a button outside a dialog ignores it)."""
        f = QApplication.focusWidget()
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and isinstance(f, QPushButton) \
                and (f is self.btn or self.chips.isAncestorOf(f)) and f.isEnabled():
            f.click()
            return
        super().keyPressEvent(ev)


def _name_of(g: dict) -> str:
    return g.get('display_name') or g.get('slug') or ''


# How Pick for me's note names a pool: (one, many).
PICK_NOUNS = {'any': ("game", "games"), 'short': ("quick game", "quick games"),
              'coop': ("co-op game", "co-op games"),
              'unfinished': ("unfinished game", "unfinished games"),
              'almost': ("game almost done", "games almost done"),
              'never': ("game never played", "games never played")}


def pick_note(vibe: str, n: int) -> str:
    """'From 21 games', 'From 7 co-op games', 'From 1 game almost done'."""
    one, many = PICK_NOUNS.get(vibe, PICK_NOUNS['any'])
    return f"From {n} {one if n == 1 else many}"


class SaveWidget(DashWidget):
    """Your save (w-91, hidden by default): the Stage's game as numbers.
    Play time, sessions (--history's by_game), Steam achievements, the
    HowLongToBeat main story, friends who own it and the last unlock; each
    row left out when missing (save_rows). Follows the Stage (moved, a
    pick). Beside a Wide Stage it is as tall as the art (Dashboard.relayout)."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'save', "Your save", G['save'], 'tide', None
    ASK_AGAIN = 300                          # s: history and last unlocks asked again

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filling = False
        self._hist: dict | None = None
        self._asked = None                   # monotonic; None: never
        self._last: dict = {}                # appid -> newest unlock (None: none)
        self._ach_asked: dict = {}           # appid -> monotonic
        self._sig = None

    @staticmethod
    def game_of(hub) -> dict:
        """The Stage's game, while the Stage is shown and has one."""
        stage = getattr(getattr(hub, 'dashboard', None), 'stage', None)
        if stage is None or not stage.art.games:
            return {}
        it = next((i for i in hub.prefs.dashboard if i.get('id') == 'stage'), None)
        return {} if it is None or it.get('hidden') else stage.art.game

    def has_content(self, hub):
        return bool(self.game_of(hub).get('slug'))

    def fill(self, hub):
        if self._filling:
            return
        self._filling = True
        try:
            g = self.game_of(hub)
            now = time.monotonic()
            # Ask first: a cached answer comes back right here, inside fill,
            # and the callbacks only store while a fill runs.
            if self._hist is None or now - self._asked > self.ASK_AGAIN:
                self._asked = now
                cli_json(['--history', '--json'],
                         lambda d, _e, hub=hub: self._got_history(hub, d), 30000, ttl=60)
            appid = str(g['appid']) if g.get('source') == 'steam' and \
                str(g.get('appid') or '').isdigit() else None
            if appid and now - self._ach_asked.get(appid, -1e9) > self.ASK_AGAIN:
                self._ach_asked[appid] = now
                cli_json(['--achievements', appid, '--json'],
                         lambda d, _e, hub=hub, a=appid: self._got_unlock(hub, a, d),
                         30000, ttl=120)
            self._draw(hub, g, appid)
        finally:
            self._filling = False

    def preview_seed(self, hub):
        self._hist, self._asked = hub.sample['history'], time.monotonic()

    def _got_history(self, hub, data):
        if data is None and self._hist is not None:
            return
        self._hist = data or {}
        if not self._filling and alive(self):
            self.fill(hub)

    def _got_unlock(self, hub, appid, data):
        self._last[appid] = last_unlock(data)
        if not self._filling and alive(self):
            self.fill(hub)

    def _draw(self, hub, g: dict, appid: str | None):
        if not g.get('slug'):
            self._sig = None
            self.clear()
            return
        n = (((self._hist or {}).get('by_game') or {}).get(g['slug']) or {}).get('sessions')
        owners = FRIENDS.owners_of(g) if hub.config.get('friends_enabled') else []
        rows = save_rows(g, hub._smart_facts(), n, owners,
                         self._last.get(appid) if appid else None)
        sig = (g['slug'], repr([r if r[0] != 'owners' else
                                (r[0], [(o.get('steamid'), o.get('name'), o.get('avatar'))
                                        for o in r[1]], r[2]) for r in rows]))
        if sig == self._sig and self.body.count():
            return                           # nothing new: keep the rows
        self._sig = sig
        self.clear()
        spaced = False
        d = scaled(GLYPH_H)
        for row in rows:
            kind = row[0]
            if kind == 'big':
                self.body.addWidget(label(row[1], "DashBig"))
            elif kind == 'sub':
                self.body.addWidget(ElideLabel(row[1], "Muted"))
            elif kind == 'empty':
                self.body.addWidget(label(row[1], "Muted", wrap=True))
            elif kind == 'bar':
                if not spaced:
                    self.body.addSpacing(SP['1'])
                    spaced = True
                # As Play history's bar rows: caption, muted value, a 4 px bar.
                line = QHBoxLayout()
                cap = plain_label(row[1], "DashSub")
                cap.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                line.addWidget(cap, 1)
                line.addWidget(label(row[2], "Muted"))
                self.body.addLayout(line)
                bar = QProgressBar()
                bar.setObjectName("CardProgress")
                bar.setTextVisible(False)
                bar.setFixedHeight(4)
                bar.setRange(0, 1000)
                bar.setValue(int(1000 * row[3]))
                bar.setToolTip(row[4])
                self.body.addWidget(bar)
            else:                            # owners, last unlock: a line with a mark
                if spaced is not None:
                    self.body.addSpacing(SP['1'])
                    spaced = None
                line = QHBoxLayout()
                line.setSpacing(SP['2'])
                if kind == 'owners':
                    line.addWidget(AvatarStack(row[1], d, 3, ring='surface'))
                    text = row[2]
                else:
                    ic = icon_label(G['trophy'], 13, P['muted'])
                    ic.setFixedWidth(d)
                    line.addWidget(ic)
                    text = row[1]
                line.addWidget(ElideLabel(text, "DashSub"), 1)
                self.body.addLayout(line)


class UpdatesWidget(ShelfWidget):
    KIND, TITLE = 'updates', "Updates ready"

    def games(self, hub):
        return [g for g in _visible_games(hub) if g.get('update_pending')]

    def why(self, hub):
        n = len(self.games(hub))
        return f"{n} game{'s' if n != 1 else ''} with an update ready" if n else ''

    def fill(self, hub):
        super().fill(hub)
        # One button for every repack update the CLI can install (w-167); Steam
        # updates and repacks found only online have no installer to run here.
        todo = [g for g in self.games(hub) if g.get('repack_update')]
        free = hub.update_split(todo)[0] if todo else []     # not busy, not playing: what Update all does
        if free and self.row is not None:
            b = QPushButton(f"Update all ({len(free)})")
            b.setObjectName("Ghost")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, hub=hub, todo=todo: hub.update_all(todo))
            self.row.head.layout().addWidget(b)


RETRY_KINDS = ('install', 'install-proton', 'task', 'tool')


def job_facts(jobs) -> list[dict]:
    """JobManager jobs as the plain dicts system_status() reads. A failed job
    is `retry`able when it is a kind Retry can run again and nothing newer ran
    the same command (it was retried, or fixed another way)."""
    out = []
    now = time.monotonic()
    jobs = list(jobs)
    for i, j in enumerate(jobs):
        prog = getattr(j, 'progress', None)
        pct = (int(100 * min(prog[0], prog[1]) / prog[1])
               if j.state == 'running' and prog and prog[1] else None)
        ended = getattr(j, 'ended', None)
        argv = list(getattr(j, 'argv', None) or ())
        retry = (j.state == 'failed' and getattr(j, 'kind', None) in RETRY_KINDS
                 and '--update-fetch' not in argv
                 and not any(list(getattr(k, 'argv', None) or ()) == argv for k in jobs[i + 1:]))
        out.append({'title': j.title, 'state': j.state, 'step': j.step, 'progress': pct,
                    'active': j.active, 'job': j, 'kind': getattr(j, 'kind', None),
                    'name': getattr(j, 'name', None), 'detail': getattr(j, 'detail', ''),
                    'age': now - ended if ended is not None else None, 'retry': retry})
    return out


class SysTile(ClickRow):
    """One clickable status tile in the System widget: icon, value, caption."""

    def __init__(self, t: dict):
        super().__init__()
        self.setObjectName("SysTile")
        self.key = t['key']
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['2_5'], SP['2'], SP['2_5'], SP['2'])
        lay.setSpacing(SP['2_5'])
        role = t['role']
        color = P['muted'] if role in ('muted', 'text') else P[role]
        ic = icon_label(G[t['glyph']], 18, color)
        ic.setFixedWidth(22)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.value = ElideLabel(t['value'], "StatValue")
        if role not in ('muted', 'text'):
            self.value.setStyleSheet(f"color: {P[role]};")
        col.addWidget(self.value)
        self.caption = ElideLabel(t['caption'], "StatLabel")
        col.addWidget(self.caption)
        lay.addLayout(col, 1)
        for w in (ic, self.value, self.caption):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setToolTip(f"{t['value']} — {t['caption']}")


class QuietLine(QLabel):
    """System's one line: the main text in `color`, then a muted extra after
    it; the whole ends in … when the card is narrow (the extra goes first)."""

    def __init__(self, main: str = '', more: str = '', color: str | None = None):
        super().__init__()
        self.main, self.more, self.color = main, more, color or P['text']
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(10)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def _fonts(self):
        a, b = QFont(self.font()), QFont(self.font())
        a.setPixelSize(FS['sm'])
        b.setPixelSize(FS['xs'])
        return a, b

    def sizeHint(self):
        a, b = self._fonts()
        return QSize(QFontMetrics(a).horizontalAdvance(self.main), QFontMetrics(a).height())

    def minimumSizeHint(self):
        return QSize(10, self.sizeHint().height())

    def paintEvent(self, ev):
        a, b = self._fonts()
        fa, fb = QFontMetrics(a), QFontMetrics(b)
        pt = QPainter(self)
        flags = int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        w = self.width()
        pt.setFont(a)
        pt.setPen(QColor(self.color))
        shown = fa.elidedText(self.main, Qt.TextElideMode.ElideRight, w)
        pt.drawText(self.rect(), flags, shown)
        left = w - fa.horizontalAdvance(self.main)
        if self.more and left > 0:
            pt.setFont(b)
            pt.setPen(QColor(P['muted']))
            x = fa.horizontalAdvance(self.main)
            pt.drawText(self.rect().adjusted(x, 0, 0, 0), flags,
                        fb.elidedText(" " + self.more, Qt.TextElideMode.ElideRight, left))
        pt.end()


class SystemWidget(DashWidget):
    """Setup at a glance (w-165). While nothing needs you: one quiet line
    ("All good · 412 GB free"; Wide adds the Proton build and checks passed),
    in the warning colour when there are only notes. When something does: a
    row for the one thing and its one action, above the four tiles (jobs,
    disk space, health, Proton), each opening its part of the System page
    (Proton: the Proton page)."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'system', "System", G['system'], 'tide', 'system'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tiles: list = []
        self.quiet_row = self.action = self.line = None
        self.first_stop = None
        self._quiet = False
        self._pad = SP['4']

    def status(self, hub) -> dict:
        return system_state(hub)

    def set_density(self, d: dict):
        self._pad = d['card_pad']
        self._margins()

    def _margins(self):
        pad = self._pad
        if self._quiet:                  # one line: a slimmer card (w-165)
            self.layout().setContentsMargins(pad, min(pad, SP['3']), pad, min(pad, SP['3']))
        else:
            self.layout().setContentsMargins(pad, pad - 2, pad, pad)

    def _set_quiet(self, on: bool):
        self._quiet = on
        self.show_head(not on)
        self._margins()

    def fill(self, hub):
        st = self.status(hub)
        sig = (self.size_, repr(st['line']),
               repr([(t['value'], t['caption'], t['role']) for t in st['tiles']]),
               repr({k: v for k, v in (st['attention'] or {}).items() if k != 'job'}))
        if sig == getattr(self, '_sig', None):
            return                  # unchanged: keep the widgets (and pad focus) as they are
        self._sig = sig
        self.clear()
        self.tiles, self.quiet_row, self.action, self.line = [], None, None, None
        if st['attention'] is None:
            self._set_quiet(True)
            self.open_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.first_stop = self.quiet_row = self._quiet_row(st['line'], hub)
            self.body.addWidget(self.quiet_row)
            return
        self._set_quiet(False)
        att = st['attention']
        grid = QGridLayout()
        grid.setSpacing(SP['2'])
        cols = 4 if self.size_ == 'wide' else 2
        for i, t in enumerate(st['tiles']):
            tile = SysTile(t)
            tile.clicked.connect(lambda k=t['key']: hub.go(k))
            self.tiles.append(tile)
            grid.addWidget(tile, i // cols, i % cols)
        for c in range(cols):
            grid.setColumnStretch(c, 1)
        self.body.addWidget(self._one_thing(att, hub))
        self.body.addLayout(grid)
        # The action is the first stop; the head's Open only repeats the tiles.
        self.open_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.first_stop = self.action

    def _quiet_row(self, line: dict, hub) -> ClickRow:
        row = ClickRow()
        row.clicked.connect(lambda: hub.go('system'))
        lay = QHBoxLayout(row)
        lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        lay.setSpacing(SP['2_5'])
        role = line['role']
        ic = icon_label(G[line['glyph']], 16, P['muted'] if role == 'muted' else P[role])
        ic.setFixedWidth(20)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignVCenter)
        self.line = QuietLine(line['text'], line['extra'] if self.size_ == 'wide' else '',
                              P['warning'] if role == 'warning' else P['text'])
        self.line.setStyleSheet(f"color: {self.line.color};")
        lay.addWidget(self.line, 1)
        link = label("System", None)
        link.setStyleSheet(f"color: {P['subtext']}; font-size: {FS['xs']}px;")
        link.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(link, 0, Qt.AlignmentFlag.AlignVCenter)
        row.setToolTip(" \u00b7 ".join(x for x in (line['text'], line['extra']) if x))
        return row

    def _one_thing(self, att: dict, hub) -> QWidget:
        """The icon, what it is, why, and the one action (colour is on the
        icon: no side bar, cards keep one border)."""
        key, job = att['key'], att.get('job')
        if key == 'report':
            go = lambda: hub.open_report(att['crash'])
        elif att.get('action') == 'retry':
            go = lambda: hub._resubmit(job, job.argv)
        elif att.get('action') == 'install-ge':
            go = lambda: hub.protonpage._install('latest')
        elif key == 'activity':
            go = (lambda: hub._open_job(job)) if job else (lambda: hub.go('activity'))
        else:
            go = lambda: hub.go(key)
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(SP['1'], 0, SP['1'], 0)
        lay.setSpacing(SP['2_5'])
        ic = icon_label(G[att['glyph']], 18, P[att['role']])
        ic.setFixedWidth(22)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = ElideLabel(att['text'])
        t.setStyleSheet(f"color: {P['text']}; font-size: {FS['sm']}px;")
        col.addWidget(t)
        if att.get('sub'):
            col.addWidget(ElideLabel(att['sub'], "StatLabel"))
        lay.addLayout(col, 1)
        self.action = QPushButton(att['label'])
        self.action.setObjectName("Primary")
        self.action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action.clicked.connect(lambda _=False: go())
        lay.addWidget(self.action, 0, Qt.AlignmentFlag.AlignVCenter)
        w.setToolTip(f"{att['text']}\n{att.get('sub') or ''}".strip())
        return w


class NewsWidget(DashWidget):
    KIND, TITLE, GLYPH, GRAD, LINK = 'news', "News", G['news'], 'aurora', 'news'

    def why(self, hub):
        page = getattr(hub, 'newspage', None)
        items = (getattr(page, 'data', None) or {}).get('items') or []
        by_app = getattr(page, 'games_by_appid', None)
        mine = {a for a, g in (by_app if isinstance(by_app, dict) else {}).items()
                if not g.get('hidden') and not g.get('dup_of')}
        n = sum(1 for it in items if it.get('kind') == 'steam' and it.get('appid') in mine)
        return f"{n} stor{'ies' if n != 1 else 'y'} about your games" if n else ''

    def fill(self, hub):
        self.clear()
        if not hub.config.get('news', True):
            self.body.addWidget(label("The News page is off (Settings).", "Muted", wrap=True))
            return
        items = (getattr(hub.newspage, 'data', {}) or {}).get('items', [])
        if not items:
            if not self._error(hub.newspage.error, lambda: hub.newspage.refresh(force=True)):
                self.body.addWidget(label("Loading headlines…", "Muted"))
                hub.newspage.refresh()            # once; cached by the CLI
            return
        n = {'small': 3, 'tall': 6, 'wide': 4, 'full': 6}[self.size_]
        by_app = getattr(hub.newspage, 'games_by_appid', None)
        by_app = by_app if isinstance(by_app, dict) else {}
        mine = {a for a, g in by_app.items() if not g.get('hidden') and not g.get('dup_of')}
        items = your_games_first(items, mine, n, today())      # w-167
        ready = {a for a in mine if by_app[a].get('update_pending')}
        for it in items[:n]:
            self.body.addWidget(self._story_row(it, hub, ready))
        self.add_more(items[n:], lambda it: self._story_row(it, hub, ready))

    def _story_row(self, it, hub, ready=frozenset()):
        """Thumbnail, headline, source · time; opens the story in the News
        page's reader."""
        w = ClickRow()
        w.clicked.connect(lambda: (hub.go(hub.NEWS), hub.newspage.open_item(it)))
        lay = QHBoxLayout(w)
        lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
        lay.setSpacing(SP['2_5'])
        art = NewsArt(it, 64, 36, R['sm'] - 2, hub.newspage._image_for)
        art.load()                       # a handful of rows: fine to load now
        lay.addWidget(art, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = plain_label(it.get('title', ''), None, wrap=True)
        t.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
        col.addWidget(t)
        role = news_style(it)[0]
        sub = label(" · ".join(x for x in (it.get('source'), fmt_relative(it.get('date')))
                               if x), None)
        sub.setStyleSheet(f"color: {P[role]}; font-size: {FS['2xs']}px;")
        extra = [t, sub]
        if it.get('kind') == 'steam' and it.get('appid') in ready:
            line = QHBoxLayout()                 # "· Update ready", in the warning colour
            line.setSpacing(0)
            line.addWidget(sub)
            tag = label(" \u00b7 ", None)
            tag.setStyleSheet(f"color: {P[role]}; font-size: {FS['2xs']}px;")
            note = label("Update ready", None)
            note.setStyleSheet(f"color: {P['warning']}; font-size: {FS['2xs']}px;")
            for x in (tag, note):
                line.addWidget(x)
                extra.append(x)
            line.addStretch()
            col.addLayout(line)
        else:
            col.addWidget(sub)
        lay.addLayout(col, 1)
        for x in extra:
            x.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        return w


class WishTile(ClickRow):
    """One Wishlist game as store art (Wide and Full): the 'why' pill on the
    art (-40%, in 12 days), the title and the price line."""

    def __init__(self, row: dict, on_click, friend: str = ''):
        super().__init__()
        self.row = row
        self.clicked.connect(on_click)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['1'])
        self.art = FlexArt(R['sm'])
        self.art.round_all = True
        self.art.top_left = [(row['pill'], row['role'])]
        self.art.set_url(row['g'].get('headerImage'))
        lay.addWidget(self.art)
        self.title = plain_label(row['g'].get('title') or '', None)
        self.title.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
        self.sub = plain_label(" · ".join(x for x in (row['line'], friend) if x), None)
        self.sub.setStyleSheet(f"color: {P['muted']}; font-size: {FS['2xs']}px;")
        for x in (self.title, self.sub):
            x.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            x.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            lay.addWidget(x)
        self.setToolTip(" · ".join(x for x in (row['g'].get('title'), row['why']) if x))


class TileGrid(QWidget):
    """Tiles in as many columns as fit (at most `max_cols`, at least two), placed
    by hand so the card's height follows its width. Tiles keep Steam's art
    shape, so a tile's height is a function of its width."""
    MIN_W = 132

    def __init__(self, tiles, max_cols: int):
        super().__init__()
        self.tiles = list(tiles)
        self.max_cols = max_cols
        self.gap_x, self.gap_y = SP['3'], SP['4']
        for t in self.tiles:
            t.setParent(self)
        pol = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        pol.setHeightForWidth(True)
        self.setSizePolicy(pol)

    def columns(self, width: int) -> int:
        fit = (width + self.gap_x) // (scaled(self.MIN_W) + self.gap_x)
        return max(2, min(self.max_cols, fit))

    def _rows(self, width: int):
        cols = self.columns(width)
        tw = max(1, (width - (cols - 1) * self.gap_x) // cols)
        heights = []
        for i in range(0, len(self.tiles), cols):
            heights.append(max(t.heightForWidth(tw) for t in self.tiles[i:i + cols]))
        return cols, tw, heights

    def heightForWidth(self, w: int) -> int:
        _c, _tw, heights = self._rows(w)
        return sum(heights) + self.gap_y * max(len(heights) - 1, 0)

    def sizeHint(self) -> QSize:
        w = 2 * scaled(self.MIN_W) + self.gap_x
        return QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self) -> QSize:
        # No height floor: at its narrowest the grid is many rows tall, and the
        # card (whose minimum is summed from it) would be stretched that far.
        return QSize(2 * scaled(self.MIN_W) + self.gap_x, 0)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.place()

    def place(self):
        cols, tw, heights = self._rows(self.width())
        y = 0
        for r, h in enumerate(heights):
            for c, t in enumerate(self.tiles[r * cols:(r + 1) * cols]):
                t.setGeometry(c * (tw + self.gap_x), y, tw, h)
                t.show()
            y += h + self.gap_y


class DealsWidget(DashWidget):
    """Wishlist (w-163; it was Wishlist deals and Upcoming): one ranked list of
    the wishlist games on sale and the unreleased ones (gamedb.wishlist_rows),
    each saying why it is there ("-40% today", "Out in 12 days · pre-order").
    Small and Tall are rows; Wide and Full are store-art tiles, four and six
    across, with the why as a pill on the art. A friend line ("Cara owns it")
    only while Friends is on. From the Game-db summary the Deals page already
    holds: no calls of its own. Without Game-db, or with nothing on sale and
    nothing coming, the card is not on Home (Customise says what it needs)."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'deals', "Wishlist", G['deals'], 'dawn', 'deals'
    ROWS = {'small': 5, 'tall': 9}
    TILES = {'wide': 8, 'full': 12}
    ACROSS = {'wide': 4, 'full': 6}
    NEEDS = "Needs Game-db · Open Settings"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tiles: TileGrid | None = None
        self.count = label("", "Muted")                  # "5 on sale · 4 coming"
        if self.head_lay is not None:
            at = self.head_lay.indexOf(self.open_btn) if self.LINK else self.head_lay.count()
            self.head_lay.insertWidget(at, self.count)

    def has_content(self, hub):
        if not hub.config.get('gamedb_enabled', False):
            return False
        page = hub.dealspage
        return not page._loaded or bool(gamedb.wishlist_rows(page._data()))

    def needs_note(self, hub):
        return "" if hub.config.get('gamedb_enabled', False) else self.NEEDS

    def clear(self):
        self.tiles = None
        super().clear()

    @staticmethod
    def _friend(hub, g: dict) -> str:
        if not hub.config.get('friends_enabled') or not g.get('appid'):
            return ''
        names = [r.get('name') for r in FRIENDS.owners(g['appid']) if r.get('name')]
        return f"{_and(names, 2)} {'owns' if len(names) == 1 else 'own'} it" if names else ''

    def _open(self, hub, row: dict):
        g = row['g']
        if row['kind'] == 'sale':
            hub.go(hub.DEALS)
            return
        page = hub.dealspage
        base = page._base()
        url = g.get('steamUrl') or (gamedb.detail_url(base, g['appid'])
                                    if base and g.get('appid') else '')
        page._open(url)

    def _row(self, hub, row: dict) -> ClickRow:
        why = " · ".join(x for x in (row['why'], self._friend(hub, row['g'])) if x)
        right = []
        if row['price']:
            price = label(row['price'], None)
            hot = row['kind'] == 'sale'
            price.setStyleSheet(f"color: {P['text' if hot else 'subtext']}; "
                                f"font-size: {FS['xs']}px;" + (" font-weight: 600;" if hot else ""))
            right.append(price)
        w = game_row(row['g'], right, lambda _=False, r=row: self._open(hub, r), sub=why)
        w.sub_label.setStyleSheet(f"color: {P[row['role']]}; font-size: {FS['2xs']}px;")
        w.setToolTip(" · ".join(x for x in (row['g'].get('releaseDateText'), row['line'],
                                            row['why']) if x))
        return w

    def _tile(self, hub, row: dict) -> WishTile:
        return WishTile(row, lambda _=False, r=row: self._open(hub, r),
                        self._friend(hub, row['g']))

    def _counts(self, rows, d) -> str:
        sales = sum(r['kind'] == 'sale' for r in rows)
        parts = [f"{sales} on sale"] * bool(sales) + \
            [f"{len(rows) - sales} coming"] * bool(len(rows) - sales)
        radar = d.get('radar') or {}
        if self.size_ == 'full' and sales and radar.get('totalSavings'):
            parts.append(f"save ~{radar['totalSavings']:.0f} {d.get('currency') or ''}".strip())
        return " · ".join(parts)

    def fill(self, hub):
        self.clear()
        self.count.setText("")
        if not hub.config.get('gamedb_enabled', False):
            # Off Home (has_content); Customise still shows the card, so say why.
            self.body.addWidget(label(self.NEEDS, "Muted", wrap=True))
            return
        page = hub.dealspage
        d = page._data()
        if not d:
            if not self._error(page.error, page.retry):
                self.body.addWidget(label("Loading your wishlist…", "Muted"))
                page.refresh()
            return
        rows = gamedb.wishlist_rows(d)
        if not rows:
            self.body.addWidget(label("Nothing on your wishlist is on sale or coming soon.",
                                      "Muted", wrap=True))
            return
        self.count.setText(self._counts(rows, d))
        if self.size_ in self.TILES:
            n = self.TILES[self.size_]
            self.tiles = TileGrid([self._tile(hub, r) for r in rows[:n]],
                                  self.ACROSS[self.size_])
            self.body.addWidget(self.tiles)
            if len(rows) > n:
                self.body.addWidget(label(f"+{len(rows) - n} more on the Deals page", "Muted"))
            return
        n = self.ROWS.get(self.size_, 5)
        for r in rows[:n]:
            self.body.addWidget(self._row(hub, r))
        tail = None
        if len(rows) > n:
            tail = label(f"+{len(rows) - n} more on the Deals page", "Muted")

        def counted(k):                                 # the line says what is left
            tail.setText(f"+{len(rows) - n - k} more on the Deals page" if len(rows) - n - k
                         else "All on the Deals page")
        self.add_more(rows[n:], lambda r: self._row(hub, r), on_count=counted)
        if tail is not None:
            self.body.addWidget(tail)


def game_row(g: dict, right: list, on_click, sub: str = '') -> ClickRow:
    """A dashboard row for a store game: header thumbnail (fetched once,
    cached), title (and an optional muted line), then `right` widgets."""
    w = ClickRow()
    w.clicked.connect(on_click)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
    lay.setSpacing(SP['2_5'])
    thumb = QLabel()
    thumb.setFixedSize(64, 30)
    thumb.setStyleSheet(f"background: {P['surface_2']}; border-radius: {R['sm']}px;")
    url = gamedb.safe_url(g.get('headerImage'))

    def show(pm, thumb=thumb):
        try:
            thumb.setPixmap(pm)
            thumb.setStyleSheet("")
        except RuntimeError:                     # the row is gone
            pass

    def put(path):
        ART.pixmap_async(path, 64, 30, R['sm'], show)
    if url:
        cached = ART.cached(url)
        if cached:
            put(cached)
        else:
            ART.fetch(url, put)
    lay.addWidget(thumb)
    col = QVBoxLayout()
    col.setSpacing(0)
    t = plain_label(g.get('title') or '', None)
    t.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
    t.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    col.addWidget(t)
    parts = [thumb, t]
    w.sub_label = None
    if sub:
        sl = w.sub_label = label(sub, "Muted")
        sl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(sl)
        parts.append(sl)
    lay.addLayout(col, 1)
    for x in right:
        lay.addWidget(x)
        parts.append(x)
    for x in parts:
        x.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    return w


class PresenceCard(ClickRow):
    """One game friends are in right now (Friends playing now, full size):
    the Steam header art with their avatars over its corner, the game and
    an "In your library" pill, then who and how long."""
    W = 248

    def __init__(self, entry: dict):
        super().__init__()
        self.appid = entry['appid']
        self.setFixedWidth(self.W)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['1_5'], SP['1_5'], SP['1_5'], SP['2'])
        lay.setSpacing(SP['1'])
        aw = self.W - 2 * SP['1_5']
        ah = int(aw * 215 / 460)                   # Steam's header is 460 x 215
        self.art = DealArt(aw, ah, R['sm'])
        self.art.round_all = True
        self.art.set_url(entry.get('headerImage'))
        lay.addWidget(self.art)
        d = 30
        self.stack = AvatarStack(entry['people'], d, 3, ring='surface', parent=self.art)
        self.stack.move(SP['2'], ah - d - SP['2'])
        row = QHBoxLayout()
        row.setSpacing(SP['1_5'])
        self.title = plain_label(entry['title'], None)
        self.title.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px; "
                                 f"font-weight: 600;")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setFixedHeight(PILL_H)             # the same with or without the pill
        row.addWidget(self.title, 1)
        self.pill = _pill("In your library", 'accent') if entry.get('slug') else None
        if self.pill is not None:
            row.addWidget(self.pill)
        lay.addLayout(row)
        self.sub_label = label(who_text(entry), "Muted")
        self.sub_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.sub_label)
        for x in (self.title, self.sub_label, *([self.pill] if self.pill else [])):
            x.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setToolTip("Open its page" if entry.get('slug') else
                        "Not in your library · opens its Steam page ↗")


class FriendsWidget(DashWidget):
    """Friends playing now (w-82): one card per game a Steam friend is in,
    the most friends first (full: a strip of cards, wide: rows). In the
    layout only while somebody plays (or Steam refused the key); Customise
    shows its other states: set up, looking, nobody in a game, all
    private. Presence is asked by the window (_sync_presence); this only
    reads FRIENDS.

    w-161: the option `lead` 'auto' (default) draws the digest's ladder
    (friends_ladder: Now, Play together, Friends' picks, quiet); 'now' keeps
    the card above: only games friends are in."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'friends', "Friends", G['friends'], 'aurora', None
    PICK_ROWS = 3
    WIDE_ROWS = 4
    SMALL_ROWS = 2
    PRIVATE_TIP = ("Private: their game details aren't public on Steam. “Friends only” "
                   "counts as private for apps.")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sig = None
        self._filling = False
        self.state = ''
        self.cards: list[ClickRow] = []
        self._subs: dict[int, QLabel] = {}
        self.footer: QLabel | None = None
        self.head: QLabel | None = None
        self._clock: float | None = None      # the gallery's sample time (preview_seed)

    @property
    def ladder(self) -> bool:
        return self.options.get('lead', 'auto') != 'now'

    def title_for(self, size):
        return "Friends playing now" if not self.ladder and size != 'small' else self.TITLE

    def preview_seed(self, hub):
        # The sample answer is stamped when the gallery opens: read it as of then.
        self._clock = float((hub.friends or {}).get('updated') or 0) + 5 or None

    def why(self, hub):
        if not hub.config.get('friends_enabled'):
            return ''
        n = sum(len(e['people']) for e in self._entries(hub, None))
        return f"{n} friend{'s' if n != 1 else ''} playing now" if n else ''

    @staticmethod
    def _entries(hub, limit: int | None = 0):
        """playing_now over the library; limit 0: MAX_CARDS, None: all."""
        return playing_now(getattr(hub, '_games', None) or (),
                           limit=MAX_CARDS if limit == 0 else limit)

    def has_content(self, hub):
        if self.ladder:
            return friends_ladder.has_content(self, hub)
        if not hub.config.get('friends_enabled') or not FRIENDS.steam_usable():
            return False
        if self.size_ == 'small':                # who's online: shown with any public friend
            return FRIENDS.error_kind() == 'bad-key' or bool(FRIENDS.steam_public())
        return FRIENDS.error_kind() == 'bad-key' or bool(self._entries(hub))

    def _state_of(self, hub) -> str:
        # Steam friends only (w-84): Game-db friends have no presence.
        if not hub.config.get('friends_enabled') or (
                FRIENDS.data is not None and not FRIENDS.steam_usable()):
            return 'setup'
        if FRIENDS.data is None:
            return 'loading'
        if FRIENDS.error_kind() == 'bad-key':
            return 'bad-key'
        if not FRIENDS.steam():
            return 'none'
        if not FRIENDS.steam_public():
            return 'private'
        return 'live'

    def fill(self, hub):
        if self._filling:
            return
        self._filling = True
        try:
            self._fill(hub)
        finally:
            self._filling = False

    def _fill(self, hub):
        if self.ladder:
            friends_ladder.fill(self, hub)
            return
        state = self._state_of(hub)
        if self.size_ == 'small':
            self._fill_small(hub, state)
            return
        entries = self._entries(hub, None if self.size_ == 'wide' else MAX_CARDS) \
            if state == 'live' else []
        sig = (self.size_, state, len(FRIENDS.private()),
               tuple((e['appid'], e['slug'], e['title'],
                      tuple((r['steamid'], r.get('name'), r.get('avatar'))
                            for r in e['people'])) for e in entries))
        if sig == self._sig:
            for e in entries:                       # only the minutes moved on
                sub = self._subs.get(e['appid'])
                if sub is not None:
                    sub.setText(who_text(e))
            self._set_footer()
            return
        self._sig = sig
        f = QApplication.focusWidget()
        focus_appid = next((c.appid for c in self.cards if c is f), None)
        self.clear()
        self.cards, self._subs, self.footer, self.state = [], {}, None, state
        if state == 'setup':
            self.body.addWidget(label("See what your Steam friends are playing and who owns "
                                      "your games. Nexus only reads what Steam shows; it "
                                      "shares nothing.", "Muted", wrap=True))
            b = QPushButton("Set up in Settings")
            b.clicked.connect(lambda: hub._open_settings('accounts', 'friends'))
            self.body.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
            return
        if state == 'loading':
            self.body.addWidget(label("Looking for your friends…", "Muted"))
            return
        if state == 'bad-key':
            self._error("Steam refused the key", hub._reload_friends)
            return
        if state == 'none':
            self.body.addWidget(label("No friends on this Steam account.", "Muted"))
            return
        if state == 'private':
            private = FRIENDS.private()
            self.body.addWidget(AvatarStack(private, 28, 5, ring='surface'))
            n = len(private)
            self.body.addWidget(label(
                f"None of your {n} friend{'s' if n != 1 else ''} show{'s' if n == 1 else ''} "
                "their game details on Steam, so Nexus can't see what they play.",
                "Muted", wrap=True))
            self.body.addWidget(label("A friend can change it under Steam › Edit Profile › "
                                      "Privacy Settings › Game details: Public.", "Muted",
                                      wrap=True))
            return
        if not entries:
            self.body.addWidget(label("No friends are in a game right now.", "Muted"))
            self._set_footer(new=True)
            return
        if self.size_ == 'full':
            host = QWidget()
            flow = FlowLayout(host, margin=0, spacing=SP['3'])
            for e in entries:
                card = PresenceCard(e)
                card.clicked.connect(lambda e=e: self._open(hub, e))
                flow.addWidget(card)
                self.cards.append(card)
                self._subs[e['appid']] = card.sub_label
            self.body.addWidget(host)
        else:
            def friend_row(e):
                right = [AvatarStack(e['people'], 22, 3, ring='surface')]
                if e['slug']:
                    right.append(_pill("In your library", 'accent'))
                row = game_row({'title': e['title'], 'headerImage': e['headerImage']}, right,
                               lambda e=e: self._open(hub, e), sub=who_text(e))
                row.appid = e['appid']
                row.setToolTip("Open its page" if e['slug'] else
                               "Not in your library · opens its Steam page ↗")
                self._subs[e['appid']] = row.sub_label       # its minutes move on, grown or not
                return row
            for e in entries[:self.WIDE_ROWS]:       # `cards`: the base rows (grown ones come and go)
                self.body.addWidget(friend_row(e))
                self.cards.append(self.body.itemAt(self.body.count() - 1).widget())
            self._tail_more(entries[self.WIDE_ROWS:], friend_row, "")
        self._set_footer(new=True)
        card = next((c for c in self.cards if c.appid == focus_appid), None) \
            if focus_appid is not None else None
        if card is not None:
            QTimer.singleShot(0, lambda c=card: alive(c) and focus_and_reveal(c))

    def _fill_small(self, hub, state: str):
        """Small (w-91): who's online as an avatar stack, "3 online · 2 in a
        game", and the first two in a game as rows (an owned game opens its
        page, another its Steam page). The other states as wide and full."""
        entries = self._entries(hub, None) if state == 'live' else []
        playing = [(r, e) for e in entries for r in e['people']]
        online = FRIENDS.online() if state == 'live' else []
        pub = FRIENDS.steam_public() if state == 'live' else []
        first = [r for r, _e in playing] + [r for r in online
                                           if all(r is not p for p, _e in playing)]
        stack = first + [r for r in pub if all(r is not o for o in first)]
        err = FRIENDS.error_kind()
        err = err if err in ('offline', 'rate-limited', 'private-list') else None
        fresh = FRIENDS.fresh()
        t = _ts((FRIENDS.data or {}).get('updated'))
        sig = ('small', state, err, fresh, len(FRIENDS.private()),
               tuple((r.get('steamid'), r.get('name'), r.get('avatar')) for r in stack),
               tuple(r.get('steamid') for r in online),
               tuple((r.get('steamid'), e['appid'], e['slug'], e['title']) for r, e in playing),
               datetime.fromtimestamp(t).strftime('%H:%M') if err and t else None)
        if sig == self._sig:
            return                              # nothing new: keep the rows (and pad focus)
        self._sig = sig
        f = QApplication.focusWidget()
        focus_sid = next((getattr(c, 'steamid', None) for c in self.cards if c is f), None)
        self.clear()
        self.cards, self._subs, self.footer, self.state = [], {}, None, state
        if state == 'setup':
            self.body.addWidget(label("See what your Steam friends play.", "Muted", wrap=True))
            b = QPushButton("Set up in Settings")
            b.clicked.connect(lambda: hub._open_settings('accounts', 'friends'))
            self.body.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
            return
        if state == 'loading':
            self.body.addWidget(label("Looking for your friends…", "Muted"))
            return
        if state == 'bad-key':
            self._error("Steam refused the key", hub._reload_friends)
            return
        if state == 'none':
            self.body.addWidget(label("No friends on this Steam account.", "Muted", wrap=True))
            return
        d = scaled(PERSON_H)
        if state == 'private':
            private = FRIENDS.private()
            n = len(private)
            self.body.addWidget(AvatarStack(private, d, 5, dim=True, ring='surface'))
            self.body.addWidget(label(
                f"All {n} friends keep their game details private." if n != 1 else
                "Your friend keeps their game details private.", "Muted", wrap=True))
            return
        live = bool(online) and fresh and not err
        stack_w = AvatarStack(stack, d, 5, dim=not live, ring='surface')
        if FRIENDS.private():
            stack_w.setToolTip(self.footer_text())
        self.body.addWidget(stack_w)
        if err:
            head = "Steam isn't answering"
        elif not fresh:
            head = "Checking who's online…"
        elif not online:
            head = "No friends online"
        else:
            n = len({r.get('steamid') for r, _e in playing})
            head = f"{len(online)} online" + (f" · {n} in a game" if n else '')
        self.body.addWidget(label(head, "StatValue"))
        if err:
            if t is not None:
                self.body.addWidget(label(
                    f"As of {datetime.fromtimestamp(t).strftime('%H:%M')}", "Muted"))
            return
        def person_row(re_):
            r, e = re_
            row = ClickRow()
            row.steamid, row.appid = r.get('steamid'), e['appid']
            row.clicked.connect(lambda e=e: self._open(hub, e))
            row.setToolTip("Open its page" if e['slug'] else
                           "Not in your library · opens its Steam page ↗")
            lay = QHBoxLayout(row)
            lay.setContentsMargins(SP['1'], SP['1'], SP['1'], SP['1'])
            lay.setSpacing(SP['2'])
            lay.addWidget(Avatar(r, scaled(GLYPH_H), live=True), 0,
                          Qt.AlignmentFlag.AlignVCenter)
            text = ElideLabel(f"{r.get('name') or ''} in {e['title']}", "DashSub")
            text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            lay.addWidget(text, 1)
            return row
        for re_ in playing[:self.SMALL_ROWS] if live else ():
            self.body.addWidget(person_row(re_))
            self.cards.append(self.body.itemAt(self.body.count() - 1).widget())
        if live:
            self._tail_more(playing[self.SMALL_ROWS:], person_row, " in a game")
        row = next((c for c in self.cards if c.steamid == focus_sid), None) \
            if focus_sid is not None else None
        if row is not None:
            QTimer.singleShot(0, lambda c=row: alive(c) and focus_and_reveal(c))

    def _tail_more(self, rest: list, build, suffix: str):
        """The rows past the base ones: shown as far as the card is tall
        enough (w-155), and a "+N more" line that says how many are left
        (the line stays, so the card's natural height never depends on it)."""
        if not rest:
            return
        line = label("", "Muted")

        def counted(k):
            line.setText(f"+{len(rest) - k} more{suffix}" if len(rest) - k
                         else "That's everyone")
        counted(0)
        self.add_more(rest, build, on_count=counted)
        self.body.addWidget(line)

    def footer_text(self) -> str:
        """'3 friends are private on Steam · updated 1 min ago'; 'as of
        14:02' when Steam isn't answering (the cards are its last word)."""
        n = len(FRIENDS.private())
        parts = [f"{n} friend{'s are' if n != 1 else ' is'} private on Steam"] if n else []
        t = _ts((FRIENDS.data or {}).get('updated'))
        if t is not None:
            if FRIENDS.error_kind() in ('offline', 'rate-limited', 'private-list'):
                parts.append(f"as of {datetime.fromtimestamp(t).strftime('%H:%M')}")
            else:
                parts.append(f"updated {fmt_ago(t)}")
        text = " · ".join(parts)
        return text[:1].upper() + text[1:]

    def _set_footer(self, new: bool = False):
        if new:
            self.footer = label("", "Muted")
            self.footer.setToolTip(self.PRIVATE_TIP)
            self.body.addWidget(self.footer)
        if self.footer is not None:
            self.footer.setText(self.footer_text())

    @staticmethod
    def _open(hub, entry: dict):
        """A game you own: its page. One you don't: its Steam store page."""
        if entry.get('slug'):
            hub._reveal_game(entry['slug'])
        else:
            open_url(storeinfo.STORE_PAGE.format(appid=entry['appid']))


def open_url(url: str):
    """The browser (a function of its own, so tests can stand in for it)."""
    QDesktopServices.openUrl(QUrl(url))


WEEK_SAME_MIN = 30      # a week within this many minutes of last week's is "about the same"


def _iso_day(text, fallback: date) -> date:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return fallback + timedelta(days=99)     # never inside a week back


def week_compare(this_min, last_to_date) -> str:
    """Your week against last week up to the same weekday (w-167): "" when
    neither has play; never a judgement, only the difference."""
    this_min, last_to_date = float(this_min or 0), float(last_to_date or 0)
    if this_min <= 0 and last_to_date <= 0:
        return ""
    diff = this_min - last_to_date
    if abs(diff) < WEEK_SAME_MIN:
        return "About the same as last week"
    return (f"+{fmt_hours(diff)} on last week" if diff > 0
            else f"{fmt_hours(-diff)} less than last week")


def _week_compare_scope(today_iso) -> str:
    try:
        wd = date.fromisoformat(today_iso).weekday()
    except (TypeError, ValueError):
        return "Compared with the same days last week"
    names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    return (f"Compared with {names[0]} last week, up to this time of day" if wd == 0
            else f"Compared with {names[0]} to {names[wd]} last week, up to this time of day")


class WeekWidget(DashWidget):
    """Your week (w-87): this week's play time, how many games and Monday to
    Sunday as bars, today bright; wide adds the week's three most played.
    Style 'number' leaves the chart out. The big charts are on Play history
    (Open). From sessions.jsonl via nexus-cli --history."""
    KIND, TITLE, GLYPH, GRAD, LINK = 'week', "Your week", G['clock'], 'dusk', 'history'

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hist: dict | None = None
        self._asked = None                   # monotonic; None: never
        self._filling = False
        self._sig = None

    def fill(self, hub):
        self._filling = True
        try:
            # Ask first: a cached answer comes back right here, inside fill.
            per_ = (self._hist or {}).get('periods') or {}
            moved = bool(per_.get('today')) and per_['today'] != today().isoformat()
            if self._hist is None or moved or time.monotonic() - self._asked > 300:
                if moved:                    # midnight passed: yesterday's memo is no answer
                    cli_cache_forget(['--history', '--json'])
                self._asked = time.monotonic()
                cli_json(['--history', '--json'],
                         lambda d, e, hub=hub: self._arrived(hub, d, e), 30000, ttl=60)
            per = (self._hist or {}).get('periods') or {}
            style = self.options.get('style', 'bars')
            sig = (self.size_, style, repr(per.get('week')), per.get('today'),
                   bool((self._hist or {}).get('since')), repr(per.get('last_week')))
            if sig == self._sig and self.body.count():
                return                  # nothing changed: keep the rows (and pad focus)
            self._sig = sig
            self.clear()
            self._build(style)
        finally:
            self._filling = False

    def preview_seed(self, hub):
        self._hist, self._asked = hub.sample['history'], time.monotonic()

    def why(self, hub):
        week = (((self._hist or {}).get('periods') or {}).get('week') or {})
        mins = week.get('minutes')
        if mins is None:                     # not asked yet: the insights payload's days
            data = getattr(getattr(getattr(hub, 'dashboard', None), 'feed', None), 'data', None)
            days = (data or {}).get('days') if isinstance(data, dict) else None
            t = today()
            mins = sum(float(v or 0) for d, v in (days or {}).items()
                       if 0 <= (t - _iso_day(d, t)).days < 7) if days else 0
        return f"You played {fmt_hours(mins)} this week" if mins and mins >= 30 else ''

    def _arrived(self, hub, data, err=''):
        if data is None:
            if self._hist is not None:
                return
            data = {}                        # no history: show the empty week
        self._hist = data
        if not self._filling:
            self.fill(hub)

    def _build(self, style: str):
        hist = self._hist or {}
        per = hist.get('periods') or {}
        week = per.get('week') or {}
        games = week.get('games') or []
        wide = self.size_ == 'wide'
        if wide:
            left, right = _columns(self.body, 2)
        else:
            left = right = self.body
        _add(left, label(fmt_hours(week.get('minutes') or 0) or '0 h', "DashBig"))
        if not hist.get('since'):
            _add(left, label("Your play time shows here as you play.", "Muted", wrap=True))
            return
        n = len(games)
        _add(left, label(f"{n} game{'s' if n != 1 else ''} this week" if n
                         else "Nothing played yet this week", "Muted", wrap=True))
        last = per.get('last_week')
        if isinstance(last, dict):
            cmp_ = week_compare(week.get('minutes'), last.get('to_date'))
            if cmp_:
                lab = label(cmp_, "Muted", wrap=True)
                lab.setAccessibleDescription(_week_compare_scope(per.get('today')))
                _add(left, lab)
        if style == 'bars':
            _add(left, _day_bars(week, per.get('today') or '', 84, month=False))
        if wide and games:
            _add(right, label("Most played", "StatLabel"))
            _bar_rows(right, play_rows([], hist)['week'][:3])


from .foryou import ForYouWidget  # noqa: E402  (it builds on DashWidget)
from .widget_gallery import DropSlot, MIME as GALLERY_MIME, parse_mime  # noqa: E402
from .insight_widgets import INSIGHT_WIDGETS  # noqa: E402  (so does this)
from .stats_widgets import STATS_WIDGETS  # noqa: E402

DASH_WIDGETS = {cls.KIND: cls for cls in (
    StageWidget, SaveWidget, ForYouWidget, FriendsWidget, PickWidget, SystemWidget, NewsWidget,
    InstalledWidget, SmartShelfWidget,
    DealsWidget,
    WeekWidget, *INSIGHT_WIDGETS, *STATS_WIDGETS, FavouritesWidget, CollectionsWidget,
    UpdatesWidget)}


class StatsAsk(QFrame):
    """The install count ask at the top of Home (w-102), for people who
    upgraded or skipped setup: two equal buttons, then never again. The
    window decides when it shows (MainWindow._sync_stats_ask) and saves the
    answer; this only says which button was pressed."""
    answered = pyqtSignal(bool)
    details = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        row = QHBoxLayout(self)
        row.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        row.setSpacing(SP['3'])
        row.addWidget(GlyphBadge(G['shield'], 'dusk', 32), 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(SP['0_5'])
        text.addWidget(label("Count this install anonymously?", "RowTitle", wrap=True))
        text.addWidget(label("At most once a day: the Nexus version, the system and the "
                             "language. No id, no games, no paths. Nothing is sent until you "
                             "say yes.", "Muted", wrap=True))
        row.addLayout(text, 1)
        self.btn_what = QPushButton("What is sent")
        self.btn_what.setObjectName("Ghost")
        self.btn_what.clicked.connect(self.details)
        self.btn_no = QPushButton("Don't")
        self.btn_no.clicked.connect(self._no)
        self.btn_yes = QPushButton("Count me")
        self.btn_yes.clicked.connect(self._yes)
        for b in (self.btn_what, self.btn_no, self.btn_yes):
            row.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        self.btn_no.setMinimumWidth(96)
        self.btn_yes.setMinimumWidth(96)

    def _yes(self):
        self.answered.emit(True)

    def _no(self):
        self.answered.emit(False)


class Dashboard(QWidget):
    """Home's widget grid, the Stage first. Columns follow the window width;
    "Customise" turns on per-widget edit bars (move ←↑↓→, size, hide) and an
    "Add widgets" row for hidden ones — all buttons, so a controller can do
    it too. The layout is saved in library.json."""

    def __init__(self, hub):
        super().__init__()
        self.hub = hub
        self.feed = HomeFeed(self)                # midnight, hour, insights (w-138)
        self.editing = False
        self.selected: str | None = None          # Customise: the card whose bar shows (w-159)
        self._more: Popover | None = None
        self.widgets: dict[str, DashWidget] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['3'])
        # The install count ask (w-102): above everything, hidden unless asked.
        self.ask = StatsAsk()
        lay.addWidget(self.ask)                   # in the layout first, then hidden
        self.ask.hide()
        # Customise sits in a slim row under the grid (w-85): the Stage is the
        # top of Home, with no title above it. While customising the button is
        # hidden: the panel docked at Home's right has Done (w-157).
        foot = QHBoxLayout()
        foot.addStretch()
        self.density = 'comfortable'
        self.btn_edit = QPushButton("Customise")
        self.btn_edit.setObjectName("Ghost")
        self.btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        set_glyph_icon(self.btn_edit, G['customise'], 16)
        self.btn_edit.clicked.connect(lambda: self.set_editing(True))
        foot.addWidget(self.btn_edit)
        self.history = HomeHistory()              # Undo (w-157)
        # Switch by time: checked when Home shows (go), at most once a minute.
        self.clock = datetime.now
        self.mono = time.monotonic
        self._time_at: float | None = None
        self._last_match: str | None = None
        self.grid = MasonryLayout()               # the Packed grid (w-154)
        self.grid.set_spacing(SP['4'], SP['5'])
        lay.addLayout(self.grid)
        lay.addLayout(foot)
        self._cols = 0
        self._laid_editing = False                # the editing flag the last relayout used
        self._laying = False                      # inside relayout (w-101)
        self._relayout_pending = False            # ... and asked for another
        self._cols_timer = QTimer(self)           # a column check for the next pass
        self._cols_timer.setSingleShot(True)
        self._cols_timer.setInterval(0)
        self._cols_timer.timeout.connect(self._check_cols)
        self._view = getattr(hub, 'homeview', None)
        self._skipped: set | None = None          # what the last relayout left out
        for kind, cls in DASH_WIDGETS.items():
            w = cls()
            w.setParent(self)                 # never a window of its own, even unplaced
            w.hide()
            w.go.connect(hub.go)
            w.edit.connect(self._edit)
            w.pressed.connect(self.select)
            self.widgets[kind] = w
        for w in self.widgets.values():
            w.bind(hub)
        self.foryou.attach(self, self.stage)
        self.stage.moved.connect(self._stage_moved)
        # Customise Home (w-92, w-157): the panel docked at Home's right, built
        # after the cards so Tab goes from them into it. It has a parent from
        # the start; the window moves it beside the scroll area (HomePage).
        self.panel = CustomisePanel(self)
        self.panel.hide()
        self.layouts_bar = self.panel.layouts_bar
        self.gallery = self.panel.gallery                 # the Widgets tab (w-158)
        # Dragging a gallery tile onto Home (w-158): one dashed slot card, built
        # once, laid out by the grid where the flow would put the widget.
        self.setAcceptDrops(True)
        self.drop_slot = DropSlot(self)
        self.drop_slot.hide()
        self._drop: tuple | None = None           # (kind, size, before id or None)
        self._drop_base = None                    # Home's placement without the slot
        self._scroll_dir = 0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(30)
        self._scroll_timer.timeout.connect(self._autoscroll)
        self.add_row_host = self.gallery                  # its old name (tests)
        self.gallery.add.connect(lambda k, z: self.add_widget(k, z))
        self.gallery.reveal.connect(self.reveal)
        self.gallery.settings.connect(lambda: self.hub._open_settings('accounts', 'deals'))
        # Previews render only while the Widgets tab shows (w-158).
        self.panel.tab.connect(lambda k: self.gallery.cache.pause(k != 'widgets'))
        bar = self.layouts_bar
        bar.preset.connect(self.pick_preset)
        bar.switch.connect(self.switch_layout)
        bar.save_as.connect(self._save_as)
        bar.adopt_old.connect(self.adopt_old_home)
        bar.rename.connect(self._rename)
        bar.delete.connect(self._delete)
        bar.edit_rule.connect(self._edit_rule)
        bar.density.connect(self._pick_density)
        QApplication.instance().focusChanged.connect(self._focus_changed)
        self.panel.undo.connect(self.undo)
        self.panel.reset.connect(self._reset)
        self.panel.done.connect(self._done)
        self.panel.folded.connect(self._panel_folded)
        self.set_density(getattr(hub.prefs, 'density', 'comfortable'))
        self.panel.hide()
        if self._view is not None:
            # Last, once everything it reads exists: the view's size, not
            # ours, sets the columns (w-101).
            self._view.installEventFilter(self)

    def insights_consumers(self) -> set:
        """The shown widgets that read the insights payload."""
        # Pick for me reads the payload only in style 'today' (w-164).
        shown = {i['id'] for i in self.items if not i['hidden']
                 and (i['id'] != 'pick' or i.get('style') == 'today')}
        return FED_BY.get('insights', set()) & shown

    @property
    def stage(self) -> StageWidget:
        return self.widgets['stage']

    @property
    def foryou(self) -> 'ForYouWidget':
        return self.widgets['foryou']

    def _stage_moved(self):
        """For you now never shows the Stage's game: ask again. Your save
        follows the Stage; Pick for me says what's on it (w-91)."""
        update = getattr(self.hub, 'dash_update', None)
        if update is not None:
            update('foryou')
        self.refresh(['save', 'pick'])

    @property
    def items(self):
        return self.hub.prefs.dashboard

    def _skip(self):
        """Shelf widgets with nothing in them stay out of the layout (except
        while editing, so they can still be arranged)."""
        if self.editing:
            return set()
        return {k for k, w in self.widgets.items() if not w.has_content(self.hub)}

    # ---- data ----
    def refresh(self, kinds=None):
        if kinds is not None:
            kinds = fed(kinds)                   # what the feeds say moved with them
        self.feed.ask(kinds)                     # ask first: the answer is there for the fill
        for k, w in self.widgets.items():
            if kinds is None or k in kinds:
                it = next((i for i in self.items if i['id'] == k), None)
                w.size_ = it['size'] if it else 'small'
                w.options = _dash_options(k, it or {})
                if it and not it['hidden']:
                    w.fill(self.hub)
        if kinds is not None and set(kinds) <= {'friends', 'stage', 'foryou', 'save', 'pick'}:
            # Presence ticks and every page or focus change ask for Friends
            # playing now, a game starting or quitting for the Stage: lay
            # out again only when one comes or goes.
            if self._skip() != self._skipped:
                self.relayout(force=True)
        elif kinds is None or {'stage', 'friends', 'installed', 'smart', 'favourites',
                               'collections', 'updates', 'deals'} & set(kinds):
            self.relayout(force=True)
        elif 'foryou' in kinds and self._skip() != self._skipped:
            self.relayout(force=True)            # the Strip came or went
        elif fed(['insights']) & set(kinds) and self._skip() != self._skipped:
            self.relayout(force=True)            # an insight widget came or went (w-139)
        self.foryou.sync_pill()
        if self.editing and (kinds is None or 'insights' in kinds):
            self._sync_gallery()                 # Recommended reads the payload (w-158)

    # ---- layout ----
    # Columns come from the width Home's scroll area offers, never from our
    # own width (w-101). The scroll area makes us as wide as its viewport or
    # our minimum, whichever is more, and the vertical bar comes and goes
    # with our height: both follow the column count. Near a breakpoint one
    # column came out wide enough for two and two narrow enough for one, and
    # resizeEvent -> relayout flipped them forever on the UI thread.
    def avail_width(self) -> int:
        """Home's width for the grid: the scroll area as if the vertical bar
        were always there, less the page margins. Nothing in the grid moves it."""
        view = self._view
        host = view.widget() if view is not None else None
        if host is None or not host.isAncestorOf(self):
            return self.width()
        w = view.maximumViewportSize().width()
        bar = view.verticalScrollBar()
        if view.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded and \
                not view.style().styleHint(QStyle.StyleHint.SH_ScrollBar_Transient, None, bar):
            w -= bar.sizeHint().width()
        m = host.layout().contentsMargins() if host.layout() is not None else None
        if m is not None:
            w -= m.left() + m.right()
        return w if w > 0 else self.width()

    def cols_width(self) -> int:
        """The width the column count comes from: Home's, plus the dock while
        customising, so opening the panel does not drop a column. The dock's
        width is a constant of its state, never a measured size (w-101)."""
        w = self.avail_width()
        docked = self.editing and self._view is not None \
            and self.panel.parentWidget() is not self
        # Three cards still fit (edit bars set their least width) down to
        # DOCKED_3 px beside the panel; narrower, Home counts what it has.
        return w + self.panel.dock_width() if docked and w >= scaled(DOCKED_3) else w

    def eventFilter(self, obj, ev):
        if obj is self._view:
            if ev.type() == QEvent.Type.Resize:
                self._check_cols()
            elif ev.type() == QEvent.Type.StyleChange:
                # Size (Appearance) moves the bar and margins without a
                # resize: look again once the restyle is done.
                self._cols_timer.start()
        return False

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._check_cols()

    def _check_cols(self):
        if not self._relayout_pending and dash_cols(max(self.cols_width(), 1)) == self._cols:
            return
        if self._laying:
            # A resize that relayout itself caused: look again on the next
            # pass rather than lay out inside a layout.
            self._cols_timer.start()
            return
        self.relayout()

    def relayout(self, force=False):
        if self._laying:
            self._relayout_pending = True
            self._cols_timer.start()
            return
        self._laying, self._relayout_pending = True, False
        try:
            self._relayout()
        finally:
            self._laying = False

    def _relayout(self):
        cols = dash_cols(max(self.cols_width(), 1))
        self._cols = cols
        self._laid_editing = self.editing
        self._skipped = self._skip()
        placed = dash_grid(self.items, cols, self._skipped)
        on = {k for k, *_ in placed}
        cards = []
        for k, r, c, span, rows in placed:
            it = next(i for i in self.items if i['id'] == k)
            self.widgets[k].options = _dash_options(k, it)
            self.widgets[k].selected = self.editing and k == self.selected
            self.widgets[k].set_editing(self.editing, it['size'], None if 'shelf' not in it
                                        else dash_shelf_label(self.hub, it['shelf']))
            cards.append((k, self.widgets[k], min(DASH_SPAN.get(it['size'], 1), cols),
                          self.widgets[k].grows, 0, it['size'] == 'tall', k == 'stage'))
        if self._drop is not None and self.editing:
            kind, size, before = self._drop
            cards = [c for c in cards if c[0] != kind]
            at = next((n for n, c in enumerate(cards) if c[0] == before), len(cards))
            cards.insert(at, ('_drop', self.drop_slot, min(DASH_SPAN.get(size, 1), cols), False, 0,
                              size == 'tall', False))
        # dash_grid walks the list, so cards are in reading order for grid_flow.
        self.grid.set_cards(cols, cards)
        dragged = self._drop[0] if self._drop is not None and self.editing else None
        for k, w in self.widgets.items():
            w.setVisible(k in on and k != dragged)
        self.drop_slot.setVisible(dragged is not None)
        self._sync_gallery()
        self.foryou.sync_pill(cols)

    @property
    def placed(self):
        """Where the Packed grid put each card (grid_flow.Placed, px from the
        grid's top)."""
        return self.grid.placed

    def _sync_gallery(self):
        """The gallery follows Home while customising: ticks, sizes, counts,
        the selected card and what to recommend (hidden widgets that have a
        reason in data already in hand)."""
        if not self.editing:
            return
        g = self.gallery
        g.sync(self.items)
        hidden = {i['id'] for i in self.items if i.get('hidden')} | \
            (set(self.widgets) - {i['id'] for i in self.items})
        reasons = {}
        for k in hidden:
            try:
                reasons[k] = self.widgets[k].why(self.hub)
            except Exception:                      # a reason is a nicety: never break Home
                reasons[k] = ''
        g.set_reasons(reasons)
        needs = {}
        for k, w in self.widgets.items():
            try:
                needs[k] = w.needs_note(self.hub)
            except Exception:                      # a note is a nicety: never break Home
                needs[k] = ''
        g.set_needs(needs)
        g.set_selected(self.selected)

    # ---- adding (w-158) ----
    def add_widget(self, kind: str, size: str, before=ADD_KEEP):
        """The gallery adds `kind` at `size` (its saved place, or `before` a
        card, or ADD_END): one Undo step. The new card is selected and Home
        scrolls to it; the focus stays where it was (the tile, now ticked)."""
        if kind not in self.widgets:
            return
        with self._undoable():
            self.hub.prefs.dashboard = dash_add(self.items, kind, size, before)
            self.refresh([kind])
            self.relayout(force=True)
            self.hub.prefs.save()
        self.select(kind)
        self._scroll_to(kind)

    # ---- dropping a gallery tile on Home (w-158) ----
    @staticmethod
    def _drag_widget(ev):
        md = ev.mimeData()
        if md is None or not md.hasFormat(GALLERY_MIME):
            return None
        return parse_mime(bytes(md.data(GALLERY_MIME)).decode('utf-8', 'replace'))

    def dragEnterEvent(self, ev):
        got = self._drag_widget(ev) if self.editing else None
        if got is None:
            ev.ignore()
            return
        ev.acceptProposedAction()
        self.drop_slot.set_widget(*got)

    def _drop_target(self, kind: str, pos) -> str | None:
        """The card a drop at `pos` (our coordinates) goes before, from the
        last placement; the slot and the dragged widget never count."""
        gr = self.grid.geometry()
        cols = max(1, self.grid._cols)                # the columns `placed` was laid out on
        gap = self.grid.horizontalSpacing()
        colw = (gr.width() - (cols - 1) * gap) / cols
        # From Home as it was before the slot came in: the slot's own room
        # would move the target under a resting pointer (and back, forever).
        if self._drop_base is None:
            self._drop_base = [p for p in self.placed if p.id != '_drop']
        return drop_before(self._drop_base, cols, colw, gap, pos.x() - gr.x(), pos.y() - gr.y(),
                           skip=(kind, '_drop'))

    def dragMoveEvent(self, ev):
        got = self._drag_widget(ev) if self.editing else None
        if got is None:
            ev.ignore()
            return
        ev.acceptProposedAction()
        kind, size = got
        pos = ev.position().toPoint()
        before = self._drop_target(kind, pos)
        if self._drop != (kind, size, before):            # lay out only when the target moves
            self._drop = (kind, size, before)
            self.drop_slot.set_widget(kind, size)
            self.relayout(force=True)
        self._edge_scroll(pos)

    def dragLeaveEvent(self, ev):
        self._clear_drop()
        ev.accept()

    def dropEvent(self, ev):
        got = self._drag_widget(ev) if self.editing else None
        if got is None:
            self._clear_drop()
            ev.ignore()
            return
        kind, size = got
        before = self._drop[2] if self._drop is not None and self._drop[:2] == got \
            else self._drop_target(kind, ev.position().toPoint())
        ev.acceptProposedAction()
        self._drop = self._drop_base = None
        self._scroll_timer.stop()
        self.drop_slot.hide()
        self.add_widget(kind, size, ADD_END if before is None else before)

    def _clear_drop(self):
        self._scroll_timer.stop()
        self._drop_base = None
        if self._drop is not None:
            self._drop = None
            self.relayout(force=True)
        self.drop_slot.hide()

    def _edge_scroll(self, pos):
        """Near the top or bottom of Home's view, scroll it while dragging."""
        view = self._view
        if view is None or not hasattr(view, 'viewport'):
            return
        y = self.mapTo(view.viewport(), pos).y() if view.viewport().isAncestorOf(self) else -1
        edge = scaled(48)
        h = view.viewport().height()
        self._scroll_dir = -1 if 0 <= y < edge else 1 if h - edge < y <= h else 0
        if self._scroll_dir and not self._scroll_timer.isActive():
            self._scroll_timer.start()
        elif not self._scroll_dir:
            self._scroll_timer.stop()

    def _autoscroll(self):
        view = self._view
        if not self._scroll_dir or view is None or self._drop is None:
            self._scroll_timer.stop()
            return
        bar = view.verticalScrollBar()
        bar.setValue(bar.value() + self._scroll_dir * scaled(18))

    def reveal(self, kind: str):
        """An on-Home tile: select its card and scroll Home to it."""
        self.select(kind)
        self._scroll_to(kind)

    def _scroll_to(self, kind: str):
        w = self.widgets.get(kind)
        view = self._view
        if w is None or view is None or not w.isVisibleTo(self):
            return
        if hasattr(view, 'ensureWidgetVisible') and view.widget() is not None \
                and view.widget().isAncestorOf(w):
            view.ensureWidgetVisible(w, 0, SP['6'])

    # ---- editing ----
    def set_editing(self, on: bool):
        self.foryou.close_list()
        f = QApplication.focusWidget()
        if not on:
            if f is not None and self.panel.isAncestorOf(f):
                self.btn_edit.show()
                self.btn_edit.setFocus(Qt.FocusReason.OtherFocusReason)   # before the panel hides
        elif f is None or f is self.btn_edit or self.isAncestorOf(f):
            f = self.btn_edit                  # it is about to hide: focus goes to the panel
        self.editing = on
        self.selected = None
        self.more_menu.close_popover(restore=False)
        if on:
            self.history.clear()
            self.panel.open()
            self.feed.ask_once()                  # Recommended reads the payload (w-158)
            self.panel.set_can_undo(False)
            self._sync_layouts()
            self.layouts_bar.set_density(self.density)
        self.panel.setVisible(on)
        if on and f is self.btn_edit:             # into the panel before the button hides (w-60)
            self.panel.default_focus().setFocus(Qt.FocusReason.OtherFocusReason)
        self.btn_edit.setVisible(not on)
        self._settle()
        if self._laid_editing != on:              # the view's resize may have laid out already
            self.relayout(force=True)
        if on:
            self._sync_gallery()
            self.gallery.cache.pause(False)       # the panel opens on Widgets
            self.gallery.open()                   # previews: missing or stale ones, lazily
        else:
            self.gallery.stop()
            self.hub.prefs.save()

    def _settle(self):
        """Give Home's page its new layout now, so the view has its new size
        and the column check runs once, not a frame later."""
        page = self.panel.parentWidget()
        if page is not None and page is not self and page.layout() is not None:
            page.layout().activate()

    def _panel_folded(self, _on: bool):
        self._settle()
        self._sync_sub()

    def _done(self):
        self.set_editing(False)

    # ---- undo (w-157) ----
    def _snap(self) -> HomeSnap:
        pr = self.hub.prefs
        return HomeSnap(tuple(dict(i) for i in self.items), self.density, pr.active, pr.base,
                        getattr(pr, 'preset', None))

    @contextmanager
    def _undoable(self):
        """A change to Home: the state before it is kept for Undo, unless
        nothing changed."""
        before = self._snap()
        try:
            yield
        finally:
            if self._snap() != before:
                self.history.push(before)
                self.panel.set_can_undo(True)

    def undo(self):
        """Take back the last change: items, density and the layout and preset
        Home was on. The "Before ..." layout a preset saved stays."""
        snap = self.history.pop()
        if snap is None:
            return
        pr = self.hub.prefs
        pr.active = snap.active if pr.layout(snap.active) is not None else None
        pr.base = snap.base if pr.layout(snap.base) is not None else None
        pr.preset = snap.preset
        self.apply_layout([dict(i) for i in snap.items], snap.density)
        self._sync_layouts()
        self.panel.set_can_undo(self.history.can_undo())

    def _sync_sub(self):
        pr = self.hub.prefs
        where = pr.active if pr.layout(pr.active) is not None \
            else PRESET_LABELS.get(getattr(pr, 'preset', None), "Your Home")
        self.panel.set_sub(f"{where} · {DENSITY_LABELS[self.density]}")

    # ---- the on-card bar (w-159) ----
    def select(self, kind: str | None):
        """Customise: this card's bar shows, no other. Not an edit (no Undo)."""
        if not self.editing or kind == self.selected or (kind is not None and kind not in self.widgets):
            return
        self.selected = kind
        if self.more_menu.isVisible():
            self.more_menu.close_popover(restore=False)
        self.relayout(force=True)

    def _focus_changed(self, _old, new):
        """Pad and keyboard: focus landing inside a card selects it (the menu's own widgets don't)."""
        if not self.editing or new is None or self._more is not None and self._more.isAncestorOf(new):
            return
        for k, w in self.widgets.items():
            if w.isVisible() and w.isAncestorOf(new):
                self.select(k)
                return

    @property
    def more_menu(self) -> Popover:
        if self._more is None:
            self._more = Popover(getattr(self.hub, 'desk', None) or self, 260)
        return self._more

    def _open_more(self, kind: str):
        """The More menu of a card: Style and Background radios, Size radios,
        its smart shelf, Hide. Every pick is one `_edit` (so one Undo step)."""
        w = self.widgets[kind]
        it = next(i for i in self.items if i['id'] == kind)
        pop = self.more_menu
        clear_layout(pop.body)
        card = pop.card
        for g in card.findChildren(QButtonGroup):
            g.deleteLater()

        def radios(head, action, pairs, cur):
            pop.body.addWidget(label(head, "SectionKicker"))
            group = QButtonGroup(card)                     # one choice each, not one for the card
            for value, text in pairs:
                r = QRadioButton(text, card)
                group.addButton(r)
                r.setChecked(value == cur)
                r.toggled.connect(lambda on, a=f"{action}={value}": on and self._edit(kind, a))
                pop.body.addWidget(r)
        for key, (prefix, _tip) in OPTION_BAR.items():
            pairs = SPEC[kind].options.get(key)
            if pairs is not None:
                radios({'style': "STYLE", 'bg': "BACKGROUND"}.get(key, prefix.upper()), key, pairs,
                       it.get(key) if it.get(key) in dict(pairs) else pairs[0][0])
        sizes = DASH_SIZES.get(kind, ('small',))
        if len(sizes) > 1:
            radios("SIZE", 'size', [(z, SIZE_LABELS[z]) for z in sizes], it['size'])
        if w.shelf_label is not None:
            b = QPushButton(f"Shelf: {w.shelf_label}", card)
            b.setObjectName("Ghost")
            b.setToolTip("Change which smart shelf this shows")
            b.clicked.connect(lambda _=False: (self._edit(kind, 'shelf'), self._open_more(kind)))
            pop.body.addWidget(b)
        hide = QPushButton("Hide this widget", card)
        hide.setObjectName("Ghost")
        set_glyph_icon(hide, G['eye_off'], 14)
        hide.clicked.connect(lambda _=False: (pop.close_popover(restore=False), self._edit(kind, 'hide')))
        pop.body.addWidget(hide)
        pop.open_at(w.btn_more)

    def _edit(self, kind, action):
        if action == 'more':
            self._open_more(kind)
            return
        with self._undoable():
            self._edit_now(kind, action)
        if action == 'hide' and self.selected == kind:
            self.selected = None
            self.relayout(force=True)

    def _edit_now(self, kind, action):
        items = self.items
        if action in ('left', 'right', 'up', 'down'):
            new = dash_move(items, kind, action, self._cols, self._skip(), self.placed)
        else:
            new = [dict(i) for i in items]
            it = next(i for i in new if i['id'] == kind)
            action, _, value = action.partition('=')          # 'style=number': that value, not the next
            if action == 'size':
                sizes = DASH_SIZES[kind]
                it['size'] = value if value in sizes else sizes[(sizes.index(it['size']) + 1) % len(sizes)]
            elif action == 'shelf':
                rules = [r for r in smart.WIDGET_RULES if r != 'linux' or has('proton')] + \
                    [smart.USER + s['id'] for s in self.hub.prefs.user_shelves()]   # w-51
                cur = _dash_shelf(it.get('shelf'))
                it['shelf'] = rules[(rules.index(cur) + 1) % len(rules)] if cur in rules \
                    else rules[0]
            elif action in OPTION_BAR:
                vals = DASH_OPTIONS[kind][action]
                cur = it.get(action) if it.get(action) in vals else vals[0]
                nxt = value if value in vals else vals[(vals.index(cur) + 1) % len(vals)]
                if action == 'bg':
                    slug = self.stage.art.game.get('slug')
                    if nxt == 'pinned' and not slug:      # nothing shown to pin: skip it
                        nxt = vals[(vals.index(nxt) + 1) % len(vals)]
                    if nxt == 'pinned':
                        it['pin'] = slug                  # Pinned art pins the game shown
                    else:
                        it.pop('pin', None)
                it[action] = nxt
            elif action == 'hide':
                it['hidden'] = True
            elif action == 'show':
                it['hidden'] = False
        self.hub.prefs.dashboard = new
        self.refresh([kind]) if action in ('size', 'show', 'shelf', *OPTION_BAR) else None
        self.relayout(force=True)
        self.hub.prefs.save()
        # Keep the controller's focus on the widget that moved.
        w = self.widgets.get(kind)
        btn = getattr(w, f'btn_{action}', None) if action in OPTION_BAR else None
        if btn is not None and btn.isVisibleTo(w):
            QTimer.singleShot(0, lambda: alive(btn) and focus_and_reveal(btn))
        if w is not None and w.isVisible() and action in ('left', 'right', 'up', 'down'):
            btn = next((b for b in w.editbar.findChildren(QToolButton)
                        if b.toolTip().lower().endswith(action)), None)
            QTimer.singleShot(0, lambda: btn and alive(btn) and focus_and_reveal(btn))

    def set_density(self, name: str):
        """Comfortable or Compact (DASH_DENSITY): Home's grid, cards, Stage
        and margins only; the Library's shelves and other pages keep theirs."""
        self.density = name if name in DASH_DENSITY else 'comfortable'
        d = DASH_DENSITY[self.density]
        self.grid.set_spacing(d['grid_h'], d['grid_v'])
        for w in self.widgets.values():
            w.set_density(d)
        view = getattr(self.hub, 'homeview', None)
        if view is not None and getattr(view, 'mode', None) == 'home':
            view.lay.setContentsMargins(d['home_side'], d['home_top'], d['home_side'], SP['6'])
        self.layouts_bar.set_density(self.density)
        self._sync_sub()

    def _pick_density(self, name: str):
        """The panel's Density switch."""
        with self._undoable():
            self.hub.prefs.density = name
            self.set_density(name)
            self.hub.prefs.save()

    def _reset(self):
        """The preset Home last started from, with its density (Cinematic when
        none was picked). Undo brings the old Home back."""
        pr = self.hub.prefs
        key = pr.preset if getattr(pr, 'preset', None) in PRESET_LABELS else 'cinematic'
        with self._undoable():
            self.apply_layout(dash_preset(key), PRESET_DENSITY.get(key))

    # ---- presets and saved layouts (w-92) ----
    def layouts_view(self, note: str = '') -> 'LayoutsView':
        pr = self.hub.prefs
        names = [lt['name'] for lt in pr.layouts]
        return LayoutsView([dict(lt) for lt in pr.layouts],
                           names.index(pr.active) if pr.active in names else None,
                           pr.old_home() is not None, note,
                           pr.preset if pr.preset in PRESET_LABELS else None)

    def _sync_layouts(self, note: str = ''):
        if self.editing:
            self.layouts_bar.show_view(self.layouts_view(note))
        self._sync_sub()

    def _focus_aside(self) -> QWidget:
        """Where focus goes when the widget that had it leaves Home: the
        panel's tab while customising, else the Customise button."""
        return self.panel.default_focus() if self.editing else self.btn_edit

    def apply_layout(self, layout, density: str | None = None):
        """Home shows `layout` (a saved layout's name, or a list of items)
        and its density when it has one. Every widget is already built: the
        ones whose size, hidden flag or options changed are filled again and
        the grid is laid out once more. Callers set prefs.active first (a
        save writes the items through to it)."""
        pr = self.hub.prefs
        if isinstance(layout, str):
            lt = pr.layout(layout)
            if lt is None:
                return
            layout, density = lt['items'], lt.get('density', density)
        new = dash_normalize([dict(i) for i in layout])
        old = {i['id']: i for i in self.items}
        changed = [i['id'] for i in new if old.get(i['id']) != i]
        f = QApplication.focusWidget()
        gone = [self.widgets[i['id']] for i in new if i['hidden'] and i['id'] in self.widgets]
        if f is not None and any(w.isAncestorOf(f) for w in gone):
            self._focus_aside().setFocus(Qt.FocusReason.OtherFocusReason)   # before it hides
        self.stage.clear_pick()
        pr.dashboard = new
        if density in DASH_DENSITY and density != self.density:
            pr.density = density
            self.set_density(density)
        if changed:
            self.refresh(changed)
        self.relayout(force=True)
        pr.save()

    def _note_full(self):
        self._sync_layouts(f"{MAX_LAYOUTS} layouts saved. Delete one first, so this Home"
                           " isn't lost.")

    def _refocus(self, w):
        QTimer.singleShot(0, lambda: alive(w) and w.isVisible() and focus_and_reveal(w))

    def _chip_of(self, name):
        names = [lt['name'] for lt in self.hub.prefs.layouts]
        btns = self.layouts_bar.layout_btns
        i = names.index(name) if name in names else None
        return btns[i] if i is not None and i < len(btns) else None

    def pick_preset(self, key: str):
        """Start from a preset; what Home showed is saved first."""
        pr = self.hub.prefs
        with self._undoable():
            if pr.keep_current(f"Before {PRESET_LABELS.get(key, key)}") == 'full':
                self._note_full()
                return
            pr.active = pr.base = None
            pr.preset = key
            self.apply_layout(dash_preset(key), PRESET_DENSITY.get(key))
        self._sync_layouts()
        self._refocus(self.layouts_bar.preset_btns.get(key))

    def switch_layout(self, index: int):
        """A layout chip: Home shows that layout; focus stays on the chip."""
        pr = self.hub.prefs
        if not 0 <= index < len(pr.layouts):
            return
        name = pr.layouts[index]['name']
        if name != pr.active:
            with self._undoable():
                pr.write_through()
                pr.active = pr.base = name
                self.apply_layout(name)
        self._sync_layouts()
        self._refocus(self._chip_of(name))

    def _save_as(self):
        def done(name):
            lt = self.hub.prefs.save_as(name)
            if lt is None:
                self._note_full()
                return
            self._sync_layouts()
            self._refocus(self._chip_of(lt['name']))
        self.hub.modal.prompt("Save as a layout", '', "Save", done, max_len=PROMPT_MAX,
                              text="Saves what Home shows now: widgets, sizes, styles and"
                                   " density.")

    def adopt_old_home(self):
        """My old Home: the layout an older Nexus saved, as a layout (once)."""
        pr = self.hub.prefs
        raw = pr.old_home()
        if raw is None:
            return
        with self._undoable():
            if pr.keep_current(f"Before {OLD_HOME}") == 'full':
                self._note_full()
                return
            lt = pr.add_layout(OLD_HOME, dash_normalize(raw, version=1), save=False)
            if lt is None:
                self._note_full()
                return
            pr.v1_taken = True
            pr.active = pr.base = lt['name']
            self.apply_layout(lt['name'])
        self._sync_layouts()
        self._refocus(self._chip_of(lt['name']))

    def _rename(self):
        pr = self.hub.prefs
        name = pr.active
        if pr.layout(name) is None:
            return

        def done(new):
            got = pr.rename_layout(name, new)
            if got:
                self.history.rename(name, got)           # Undo follows the new name
            self._sync_layouts()
            if got:
                self._refocus(self.layouts_bar.btn_rename)
        self.hub.modal.prompt("Rename layout", name, "Rename", done, max_len=PROMPT_MAX)

    def _delete(self):
        pr = self.hub.prefs
        lt = pr.layout(pr.active)
        if lt is None:
            return
        name = lt['name']

        def done():
            pr.delete_layout(name)
            self.history.clear()               # no step may point at the layout that is gone
            self.panel.set_can_undo(False)
            self._sync_layouts()
        self.hub.modal.ask(f"Delete “{name}”?",
                           "Your Home stays as it is now; only this saved layout goes"
                           + (", with its time rule." if lt.get('rule') else "."),
                           "Delete", done)

    def _edit_rule(self):
        pr = self.hub.prefs
        lt = pr.layout(pr.active)
        if lt is None:
            return
        name, modal = lt['name'], self.hub.modal
        others = [(o['name'], o['rule']) for o in pr.layouts
                  if o is not lt and o.get('rule')]
        body, ok = time_rule_body(lt.get('rule'), others)
        body.changed.connect(modal.refresh)
        body.changed.connect(modal.touched)

        def done(rule):
            pr.set_rule(name, rule)
            self._sync_layouts()
            self._refocus(self.layouts_bar.btn_time)
        modal.open("Switch by time", body, "Save", lambda: done(body.rule()),
                   glyph=G['clock'], sub=f"Home switches to “{name}”", can_submit=ok,
                   extra=("Remove rule", G['close'], lambda: done(None))
                   if lt.get('rule') else None, extra_danger=False, focus=body.first())

    def _busy(self) -> bool:
        """Something on top of Home: the clock waits."""
        hub = self.hub
        for name in ('_open_modal', '_open_sheet'):
            fn = getattr(hub, name, None)
            if callable(fn) and fn() is not None:
                return True
        return self.editing or self.foryou.list_open()

    def time_check(self, now: datetime | None = None):
        """Home is showing: switch layouts by time of day. At most once a
        minute, never while customising or with something open on top, and
        only when the matching rule changed since the last look (a layout
        picked by hand sticks until then). Leaving every window goes back
        to the layout last picked by hand (base)."""
        if self._busy():
            return
        mono = self.mono()
        if self._time_at is not None and mono - self._time_at < 60:
            return
        self._time_at = mono
        pr = self.hub.prefs
        match = layout_for(pr.layouts, now or self.clock())
        if match == self._last_match:
            return
        prev, self._last_match = self._last_match, match
        if match is not None:
            if match == pr.active:
                return
            if pr.layout(pr.active) is None:
                # An unnamed Home: keep it as a layout to come back to.
                kept = pr.add_layout(MY_HOME, self.items, self.density, auto=True, save=False)
                if kept is None:
                    return
                pr.base = kept['name']
            pr.write_through()
            pr.active = match
            self.apply_layout(match)
        elif prev is not None and pr.layout(pr.base) is not None and pr.base != pr.active:
            pr.write_through()
            pr.active = pr.base
            self.apply_layout(pr.base)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import ART  # noqa: E402
from .deals import _pill, DealArt, dim, FlexArt  # noqa: E402
from . import friends_ladder  # noqa: E402
from .friends import (_and, _ts, Avatar, AvatarStack, FRIENDS, MAX_CARDS, names_text,  # noqa: E402
    playing_now, who_text)
from .details import ElideLabel  # noqa: E402
from .gameinfo import is_installed, live_kicker, play_enabled  # noqa: E402
from .input import focus_and_reveal  # noqa: E402
from .customise_panel import CustomisePanel  # noqa: E402
from .popover import Popover  # noqa: E402
from .layouts_bar import LayoutsView, time_rule_body  # noqa: E402
from .jobs import cli_cache_forget, cli_json  # noqa: E402
from .library import featured_games, ShelfRow  # noqa: E402
from .newspage import news_style, NewsArt, your_games_first  # noqa: E402
from .system import _add, _bar_rows, _columns, _day_bars, play_rows  # noqa: E402
from .theatre import _search_norm  # noqa: E402
from .widgets import (clear_layout, FlowLayout, GrowHost, icon_button, icon_label, label, MasonryLayout,
    plain_label, set_glyph_icon)  # noqa: E402
