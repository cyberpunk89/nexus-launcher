"""The System page: health checks, play history."""

import time
from datetime import datetime

from PyQt6.QtCore import pyqtSignal, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFrame, QGridLayout, QHBoxLayout,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from .. import achievements as A, platform
from ..features import has
from .fmt import fmt_hours, fmt_relative
from .theme import FONTS, FS, G, P, SP


# Health checks that need a Linux-only feature (features.has), by (group,
# title); a title of None covers the whole group. --doctor sends the Windows
# set already; this keeps a stray one (or NEXUS_FEATURES) from showing.
CHECK_NEEDS = {('Performance', None): 'tips', ('Integrations', 'Heroic'): 'heroic',
               ('Integrations', 'Play Proton'): 'proton',
               ('Integrations', 'protongen'): 'protongen'}


def check_supported(c: dict) -> bool:
    need = CHECK_NEEDS.get((c['group'], c['title'])) or CHECK_NEEDS.get((c['group'], None))
    return not need or has(need)


class HealthPage(QWidget):
    request = pyqtSignal(list, str, bool, str)
    counts = pyqtSignal(int, int)          # (problems, failures) for the sidebar badge
    toast = pyqtSignal(str, str)
    update_wanted = pyqtSignal()           # the Nexus update row's button (the Updater)

    STATUS = {'ok': (G['check'], 'success'), 'warn': (G['warn'], 'warning'),
              'fail': (G['fail'], 'danger'), 'info': (G['info'], 'muted')}

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        head = QHBoxLayout()
        self.page_title = label("Health", "PageTitle")   # hidden inside SystemPage
        self.content_lay = lay
        head.addWidget(self.page_title)
        head.addStretch()
        self.btn_ok_toggle = QCheckBox("Show passing checks")
        self.btn_ok_toggle.toggled.connect(lambda _: self._render())
        head.addWidget(self.btn_ok_toggle)
        copy = QPushButton("Copy report")
        copy.clicked.connect(self._copy)
        head.addWidget(copy)
        self.btn_recheck = QPushButton("Re-check")
        self.btn_recheck.clicked.connect(self.refresh)
        head.addWidget(self.btn_recheck)
        lay.addLayout(head)
        self.summary = label("Checking…", "Muted")
        lay.addWidget(self.summary)
        self.update_card = self._build_update_card()
        lay.addWidget(self.update_card)
        self.update_card.hide()
        self._update = None                # the Updater's view while a newer Nexus is out
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.body = QVBoxLayout(host)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(SP['2'])
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)
        self.checks: list[dict] = []
        self._checked_at = 0.0

    def _build_update_card(self) -> QFrame:
        """A newer Nexus: one row above the checks, kept across re-checks
        (it isn't a --doctor row) until the update is installed."""
        card = QFrame()
        card.setObjectName("Card")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(SP['3'], SP['2'], SP['3'], SP['2'])
        outer.setSpacing(SP['2'])
        row = QHBoxLayout()
        row.setSpacing(SP['2_5'])
        ic = icon_label(G['update'], 14, P['accent'])
        row.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(SP['0_5'])
        self.update_title = plain_label("", wrap=True)
        texts.addWidget(self.update_title)
        self.update_detail = label("", "Muted", wrap=True)
        texts.addWidget(self.update_detail)
        row.addLayout(texts, 1)
        self.update_btn = QPushButton()
        self.update_btn.setObjectName("Primary")
        self.update_btn.clicked.connect(self.update_wanted)
        row.addWidget(self.update_btn, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(row)
        self.update_bar = QProgressBar()
        self.update_bar.setObjectName("CardProgress")
        self.update_bar.setTextVisible(False)
        self.update_bar.setFixedHeight(6)
        outer.addWidget(self.update_bar)
        self.update_bar.hide()
        return card

    def show_update(self, view: dict | None):
        """The Updater's state (Updater.view()); None hides the row."""
        was = self._update is not None
        self._update = view
        self.update_card.setVisible(view is not None)
        if view is not None:
            self.update_title.setText(view['title'])
            self.update_detail.setText(view['detail'])
            self.update_btn.setText(view['button'])
            self.update_btn.setEnabled(view['enabled'])
            n_total = view.get('progress')
            self.update_bar.setVisible(bool(n_total))
            if n_total:
                self.update_bar.setRange(0, n_total[1])
                self.update_bar.setValue(n_total[0])
        if was != (view is not None):
            self._emit_counts()

    def _emit_counts(self):
        """Problems for the badge: warn and fail checks, and a Nexus update."""
        problems = [c for c in self.checks if c['status'] in ('warn', 'fail')]
        fails = [c for c in problems if c['status'] == 'fail']
        self.counts.emit(len(problems) + (self._update is not None), len(fails))

    def refresh(self):
        self.btn_recheck.setEnabled(False)
        self.summary.setText("Checking…")

        def done(data, err):
            try:
                self.btn_recheck.setEnabled(True)
            except RuntimeError:
                return
            if data is None:
                self.summary.setText(f"Couldn't run the checks: {err}")
                return
            self.checks = [c for c in data.get('checks', []) if check_supported(c)]
            self._checked_at = time.time()
            self._render()
        cli_json(['--doctor', '--json'], done, 120000)

    def _render(self):
        clear_layout(self.body)
        problems = [c for c in self.checks if c['status'] in ('warn', 'fail')]
        fails = [c for c in problems if c['status'] == 'fail']
        self._emit_counts()
        self.summary.setText("" if not problems else
                             f"{len(problems)} thing(s) need attention"
                             + (f", {len(fails)} serious." if fails else "."))
        self.summary.setVisible(bool(problems))
        show_ok = self.btn_ok_toggle.isChecked()
        if not problems and not show_ok:
            groups = list(dict.fromkeys(c['group'] for c in self.checks))
            when = (datetime.fromtimestamp(self._checked_at).strftime('%H:%M')
                    if self._checked_at else '')
            ok = EmptyState(G['check'], f"All {len(self.checks)} checks passed",
                            ", ".join(groups) + (f" · checked at {when}" if when else ""),
                            "Show passing checks", 'success')
            ok.action.connect(lambda: self.btn_ok_toggle.setChecked(True))
            self.body.addWidget(ok)
        group = None
        card = None
        for c in self.checks:
            if c['status'] in ('ok', 'info') and not show_ok and not c['actions']:
                continue
            if c['group'] != group:
                group = c['group']
                self.body.addWidget(label(group, "StatLabel"))
                card = QFrame()
                card.setObjectName("Card")
                QVBoxLayout(card).setContentsMargins(SP['3'], SP['1'], SP['3'], SP['1'])
                self.body.addWidget(card)
            card.layout().addWidget(self._row(c))
        self.body.addStretch()

    def _row(self, c):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, SP['2'], 0, SP['2'])
        lay.setSpacing(SP['2_5'])
        glyph, role = self.STATUS[c['status']]
        ic = icon_label(glyph, 14, P[role])
        ic.setAlignment(Qt.AlignmentFlag.AlignTop)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(SP['0_5'])
        texts.addWidget(plain_label(c['title'], wrap=True))
        if c['detail']:
            # Launch strings are long comma runs with no spaces; give the
            # word-wrapper somewhere to break them.
            detail = soft_wrap(c['detail'])
            d = label(detail, "Muted", wrap=True)
            d.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            texts.addWidget(d)
        if c['fix']:
            texts.addWidget(label(c['fix'], "Muted", wrap=True))
        lay.addLayout(texts, 1)
        for a in c['actions']:
            if a['args'][:1] == ['--open-url']:
                # Windows' fixes only open a page (Settings, Windows Security,
                # a help link): straight from here, no job, nothing else.
                url = a['args'][1] if len(a['args']) == 2 else ''
                if not platform.fix_page(url):
                    continue
                b = QPushButton(amp(a['label']))
                b.setToolTip(url)
                b.setProperty('url', url)
                b.clicked.connect(self._open_page)
                lay.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
                continue
            b = QPushButton(amp(a['label']))
            b.clicked.connect(lambda _=False, a=a, b=b: (
                b.setEnabled(False),
                self.request.emit(a['args'], f"{a['label']} — {c['title'].split(':')[0]}",
                                  bool(a.get('steam')), 'task')))
            lay.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        return w

    def _open_page(self):
        url = self.sender().property('url') or ''
        if not platform.fix_page(url) or not platform.open_url(url):
            self.toast.emit(f"Couldn't open {url}", 'error')

    def _copy(self):
        def done(out, err):
            if out is None:
                self.toast.emit(f"Couldn't build the report: {err}", 'error')
                return
            QApplication.clipboard().setText(out)
            self.toast.emit("Health report copied to the clipboard.", 'success')
        CliQuery(['--doctor'], done, 120000)


# --------------------------------------------------------------------------
# Settings page
# --------------------------------------------------------------------------

class MinuteBars(QWidget):
    """Minutes played per slot (a day or a week), oldest left: plain painted
    bars with the time above each (when there's room) and a label below
    every `labels_every`-th one, counted back from the last. Bars in
    `highlight` are full strength, the rest dimmed. `scale` sizes the text
    and its room (Theatre's Your week passes THEATRE_SCALE)."""

    def __init__(self, height: int = 170, labels_every: int = 2, scale: float = 1.0):
        super().__init__()
        self.bars: list[dict] = []          # {minutes, label}
        self.highlight: set[int] = set()
        self.labels_every = labels_every
        self.scale = scale
        self.setMinimumHeight(height)

    def set_bars(self, bars: list[dict], highlight=None):
        self.bars = bars
        self.highlight = set(highlight if highlight is not None else [len(bars) - 1])
        self.update()

    def paintEvent(self, ev):
        if not self.bars:
            return
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(FONTS.ui)
        sc = self.scale
        f.setPixelSize(int(FS['2xs'] * sc))
        pt.setFont(f)
        n = len(self.bars)
        top, bottom = 18.0 * sc, 20.0 * sc
        h = self.height() - top - bottom
        slot = self.width() / n
        most = max((b['minutes'] for b in self.bars), default=0) or 1
        for i, b in enumerate(self.bars):
            x = i * slot + slot * 0.18
            bw = slot * 0.64
            bh = max(2.0, h * b['minutes'] / most) if b['minutes'] else 2.0
            r = QRectF(x, top + h - bh, bw, bh)
            pt.setPen(Qt.PenStyle.NoPen)
            col = QColor(P['accent'] if b['minutes'] else P['overlay'])
            if b['minutes'] and i not in self.highlight:
                col.setAlphaF(0.55)
            pt.setBrush(col)
            pt.drawRoundedRect(r, 3.0 * sc, 3.0 * sc)
            pt.setPen(QColor(P['subtext']))
            if b['minutes'] and slot >= 22:
                pt.drawText(QRectF(i * slot - 4, r.top() - 16 * sc, slot + 8, 14 * sc),
                            Qt.AlignmentFlag.AlignCenter, f"{b['minutes'] / 60:.0f} h"
                            if b['minutes'] >= 60 else f"{b['minutes']:.0f} m")
            if b.get('label') and (n - 1 - i) % self.labels_every == 0:
                pt.setPen(QColor(P['muted']))
                pt.drawText(QRectF(i * slot - slot, self.height() - bottom + 4 * sc, slot * 3,
                                   14 * sc), Qt.AlignmentFlag.AlignCenter, b['label'])
        pt.end()


class WeekBars(MinuteBars):
    """Hours played per week (the Play history page): this week stands out,
    every other week dated."""

    def set_weeks(self, weeks: list[dict]):
        bars = []
        for w in weeks:
            try:
                lab = datetime.fromisoformat(w['start']).strftime('%d %b')
            except (KeyError, ValueError):
                lab = ''
            bars.append({'minutes': w['minutes'], 'label': lab})
        self.set_bars(bars)


def play_rows(games: list[dict], hist: dict | None) -> dict:
    """Play history's rows by game, each (name, text, minutes, the most in
    that list): 'week' and 'month' from `--history` periods, 'all' from the
    library's play time, most played first."""
    per = (hist or {}).get('periods') or {}

    def period(key):
        gs = (per.get(key) or {}).get('games') or []
        return [(m['name'], fmt_hours(m['minutes']) or '', m['minutes'], gs[0]['minutes'])
                for m in gs]
    top = sorted((g for g in games if g.get('playtime_minutes')),
                 key=lambda g: g['playtime_minutes'], reverse=True)
    return {'week': period('week'), 'month': period('month'),
            'all': [(g.get('display_name', ''), fmt_hours(g['playtime_minutes']) or '',
                     g['playtime_minutes'], top[0]['playtime_minutes']) for g in top]}


def achievement_view(games: list[dict], recent: list | None, rarest: list | None,
                     n: int) -> dict:
    """Steam achievements for Play history: unlocked and complete counts over
    the Steam games with stats, and up to `n` rows each of the newest unlocks
    (title, "game · when"), the games closest to 100% (name, "9/10", 9, 10)
    and the rarest unlocks (title, "0.7% · game")."""
    steam = [g for g in games if g.get('source') == 'steam' and g.get('achievements')]
    names = {g['appid']: g.get('display_name', '') for g in games if g.get('appid')}
    rec = []
    for a in (recent or [])[:n]:
        when = fmt_relative(datetime.fromtimestamp(a['time']).isoformat()) or ''
        rec.append((a['title'], f"{names.get(a['appid'], '')} · {when}".strip(' ·')))
    close = [steam[i] for i in A.closest({i: g['achievements'] for i, g in enumerate(steam)}, n)]
    return {'games': len(steam), 'unlocked': sum(g['achievements'][0] for g in steam),
            'complete': sum(1 for g in steam if g['achievements'][0] == g['achievements'][1]),
            'recent': rec,
            'closest': [(g.get('display_name', ''),
                         f"{g['achievements'][0]}/{g['achievements'][1]}",
                         g['achievements'][0], g['achievements'][1]) for g in close],
            'rarest': [(a['title'],
                        f"{a['percent']:.1f}% · {names.get(a['appid'], '')}".strip(' ·'))
                       for a in (rarest or [])[:n]]}


def _columns(body, n: int) -> list[QVBoxLayout]:
    """`n` side-by-side columns in a full-width card."""
    row = QHBoxLayout()
    row.setSpacing(SP['6'])
    cols = []
    for _ in range(n):
        c = QVBoxLayout()
        c.setSpacing(SP['1_5'])
        row.addLayout(c, 1)
        cols.append(c)
    body.addLayout(row)
    for c in cols:
        c.addStretch()
    return cols


def _add(lay, w):
    """Add above a column's closing stretch (or at the end of a plain body)."""
    i = lay.count() - 1
    if not (i >= 0 and lay.itemAt(i).spacerItem()):
        i = lay.count()
    if isinstance(w, QWidget):
        lay.insertWidget(i, w)
    else:
        lay.insertLayout(i, w)


def _bar_rows(lay, rows):
    """Name, value on the right, and a 4 px bar under it: rows of
    (name, text, value, most)."""
    for name, text, value, most in rows:
        row = QHBoxLayout()
        n = plain_label(name, None)
        n.setStyleSheet(f"color: {P['subtext']}; font-size: {FS['xs']}px;")
        n.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(n, 1)
        row.addWidget(label(text, "Muted"))
        _add(lay, row)
        bar = QProgressBar()
        bar.setObjectName("CardProgress")
        bar.setTextVisible(False)
        bar.setFixedHeight(4)
        bar.setRange(0, 1000)
        bar.setValue(int(1000 * value / most) if most else 0)
        _add(lay, bar)


def _note_row(lay, title: str, right: str):
    """A title with a muted note on the right (an achievement and its game)."""
    row = QHBoxLayout()
    t = plain_label(title, None)
    t.setStyleSheet(f"color: {P['text']}; font-size: {FS['xs']}px;")
    t.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    row.addWidget(t, 1)
    r = plain_label(right, "Muted")
    r.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    row.addWidget(r)
    _add(lay, row)


def _day_bars(period: dict, today: str, height: int, month: bool) -> MinuteBars:
    """A week (weekday under each bar, today highlighted) or a month (every
    seventh day numbered, this week highlighted) from history.periods()."""
    days = period.get('days') or []
    bars, hi = [], set()
    try:
        now = datetime.fromisoformat(today).date()
    except (TypeError, ValueError):
        now = None
    for i, d in enumerate(days):
        try:
            day = datetime.fromisoformat(d['date']).date()
        except (KeyError, ValueError):
            day = None
        lab = ''
        if day:
            lab = (str(day.day) if day.day % 7 == 1 else '') if month else day.strftime('%a')
            if now and (day == now if not month
                        else now.isocalendar()[:2] == day.isocalendar()[:2]):
                hi.add(i)
        bars.append({'minutes': d.get('minutes') or 0, 'label': lab})
    chart = MinuteBars(height, labels_every=1)
    chart.set_bars(bars, hi)
    return chart


class PlayHistoryPage(QWidget):
    """Your play sessions and Steam achievements: this year in review and
    the last twelve weeks, this month by day and by game, all time by game,
    then achievements (the big charts that were on Home until w-87). Filled
    from `nexus-cli --history`, `--achievements recent|rarest` and the
    library's games, which the window hands over (refresh(games=...)) as it
    does Storage's."""
    reveal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        self.content_lay = lay
        self.page_title = label("Play history", "PageTitle")
        lay.addWidget(self.page_title)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        inner = QVBoxLayout(host)
        inner.setContentsMargins(0, 0, SP['2'], 0)
        inner.setSpacing(SP['6'])
        self.empty = EmptyState(G['history'], "No sessions yet",
                                "Every time you play — a repack, or a Steam or Heroic game "
                                "while Nexus is open — the session is noted here.")
        inner.addWidget(self.empty)
        self.body = QWidget()                       # this year
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(SP['3'])
        self.year_title = self._title("")
        bl.addWidget(self.year_title)
        self.tiles = QGridLayout()
        self.tiles.setHorizontalSpacing(SP['2'])
        self.tiles.setVerticalSpacing(SP['2'])
        self.t_hours = StatTile(G['timer'], "Played")
        self.t_sessions = StatTile(G['play'], "Sessions")
        self.t_games = StatTile(G['gamepad'], "Games")
        self.t_streak = StatTile(G['calendar'], "Best streak", 'info')
        self.t_long = StatTile(G['trophy'], "Longest session", 'success')
        self.t_day = StatTile(G['history'], "Busiest day", 'info')
        for i, t in enumerate((self.t_hours, self.t_sessions, self.t_games, self.t_streak,
                               self.t_long, self.t_day)):
            self.tiles.addWidget(t, i // 3, i % 3)
        bl.addLayout(self.tiles)
        bl.addWidget(label("Last 12 weeks", "StatLabel"))
        self.bars = WeekBars()
        bl.addWidget(self.bars)
        bl.addWidget(label("Most played this year", "StatLabel"))
        self.top = QVBoxLayout()
        self.top.setSpacing(SP['1'])
        bl.addLayout(self.top)
        inner.addWidget(self.body)
        self.play_sec, self.play_lay = self._section()     # this month, all time
        inner.addWidget(self.play_sec)
        self.ach_sec, self.ach_lay = self._section()       # achievements
        inner.addWidget(self.ach_sec)
        inner.addStretch()
        self.scroll.setWidget(host)
        lay.addWidget(self.scroll, 1)
        for w in (self.body, self.play_sec, self.ach_sec):
            w.setVisible(False)
        self._games: list[dict] = []
        self._hist: dict | None = None
        self._recent: list | None = None
        self._rarest: list | None = None
        self._err = ''
        self._asked = self._asked_recent = self._asked_rare = None   # monotonic; None: never
        self._filling = False
        self._sig: dict[str, str] = {}

    @staticmethod
    def _title(text: str):
        t = label(text, None)
        t.setStyleSheet(f"font-size: {FS['lg']}px; font-weight: 600;")
        return t

    @staticmethod
    def _section():
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        return w, lay

    # ---- data ----
    def refresh(self, force: bool = False, games: list[dict] | None = None):
        """Bring the page up to date. `games` is the library (all-time play
        and the Steam stats); --history is asked at most every 30 s unless
        forced, recent unlocks every 5 min, the rarest every 30 min, and
        neither achievements query without a Steam game that has stats."""
        if games is not None:
            self._games = games
        now = time.monotonic()
        self._filling = True
        try:
            # Ask first: a cached answer comes back right here, inside the
            # fill, and must not draw a second time (the _filling flag).
            if force or self._asked is None or now - self._asked >= 30:
                self._asked = now
                cli_json(['--history', '--json'], self._got_history, 30000)
            if any(g.get('source') == 'steam' and g.get('achievements') for g in self._games):
                if force or self._asked_recent is None or now - self._asked_recent > 300:
                    self._asked_recent = now
                    cli_json(['--achievements', 'recent', '--json'], self._got_recent, 30000,
                             ttl=300)
                if self._asked_rare is None or now - self._asked_rare > 1800:
                    self._asked_rare = now
                    cli_json(['--achievements', 'rarest', '--json'], self._got_rarest, 60000,
                             ttl=1800)
            self._draw()
        finally:
            self._filling = False

    def fill(self, data: dict):
        """Draw from a --history answer."""
        self._hist = data or {}
        self._draw()

    def _got_history(self, data, _err=''):
        if data is None and self._hist is not None:
            return                                  # failed: keep what is drawn
        self._hist = data or {}
        self._redraw()

    def _got_recent(self, data, err=''):
        if data is None:
            if self._err:
                return
            self._err = err or 'error'
            self._recent = self._recent or []
        else:
            new = data.get('recent') or []
            if new == self._recent and not self._err:
                return
            self._err, self._recent = '', new
        self._redraw()

    def _got_rarest(self, data, _err=''):
        new = (self._rarest or []) if data is None else (data.get('rarest') or [])
        if new == self._rarest:                     # offline or failed: keep what we had
            return
        self._rarest = new
        self._redraw()

    def _redraw(self):
        if self._filling:
            return                                  # the running fill draws it
        try:
            self._draw()
        except RuntimeError:                        # the page is gone
            pass

    # ---- drawing ----
    def _draw(self):
        hist = self._hist or {}
        sessions = bool(hist.get('since') or (hist.get('year') or {}).get('sessions'))
        self._show(self.empty, not sessions)
        self._draw_year(hist)
        self._draw_play(hist, play_rows(self._games, hist), sessions)
        self._draw_ach(achievement_view(self._games, self._recent, self._rarest, 6))

    def _show(self, w, on: bool):
        if not on and w.isVisible() and w.isAncestorOf(QApplication.focusWidget()):
            self.scroll.setFocus()                  # hiding it would throw focus onwards
        w.setVisible(on)

    def _clear(self, sec, lay):
        if sec.isAncestorOf(QApplication.focusWidget()):
            self.scroll.setFocus()
        clear_layout(lay)

    def _changed(self, key: str, sig) -> bool:
        """False when the section already shows this: its widgets (and the
        pad's focus) stay."""
        sig = repr(sig)
        if self._sig.get(key) == sig:
            return False
        self._sig[key] = sig
        return True

    def _draw_year(self, data: dict):
        y = data.get('year') or {}
        has = bool(y.get('sessions'))
        self._show(self.body, has)
        if not has or not self._changed('year', (y, data.get('weeks'), data.get('since'))):
            return
        self.year_title.setText(f"{y['year']} so far"
                                + (f" · since {data['since']}" if data.get('since') else ''))
        self.t_hours.set(fmt_hours(int(y['minutes'])) or '0 h')
        self.t_sessions.set(str(y['sessions']))
        self.t_games.set(str(y['games']), f"Games · {y['days']} days")
        self.t_streak.set(f"{y['best_streak']} day{'s' if y['best_streak'] != 1 else ''}")
        lg = y.get('longest') or {}
        self.t_long.set(fmt_hours(int(lg.get('minutes') or 0)) or '—',
                        f"Longest · {lg.get('name', '')}" if lg else "Longest session")
        self.t_day.set(y.get('busiest_day') or '—')
        self.bars.set_weeks(data.get('weeks') or [])
        self._clear(self.body, self.top)
        most = max((t['minutes'] for t in y.get('top') or []), default=1) or 1
        for t in y.get('top') or []:
            row = QHBoxLayout()
            b = QPushButton(amp(t['name']))
            b.setObjectName("Ghost")
            b.setStyleSheet("text-align: left;")
            b.clicked.connect(lambda _=False, sl=t['slug']: self.reveal.emit(sl))
            row.addWidget(b, 1)
            row.addWidget(label(fmt_hours(int(t['minutes'])) or '', "Muted"))
            self.top.addLayout(row)
            bar = QProgressBar()
            bar.setObjectName("CardProgress")
            bar.setTextVisible(False)
            bar.setFixedHeight(4)
            bar.setRange(0, 1000)
            bar.setValue(int(1000 * t['minutes'] / most))
            self.top.addWidget(bar)

    def _draw_play(self, data: dict, rows: dict, sessions: bool):
        """This month by day (this week bright), then this month and all
        time by game; without sessions, all time alone."""
        per = data.get('periods') or {}
        month = per.get('month') or {}
        self._show(self.play_sec, sessions or bool(rows['all']))
        if not self._changed('play', (sessions, month, per.get('today'), rows['month'],
                                      rows['all'])):
            return
        self._clear(self.play_sec, self.play_lay)
        lay = self.play_lay
        lay.addWidget(self._title("Play time"))
        if sessions:
            head = QHBoxLayout()
            head.addWidget(label("This month by day", "StatLabel"))
            head.addStretch()
            if month.get('minutes'):
                head.addWidget(label(f"{fmt_hours(month['minutes'])} this month", "Muted"))
            lay.addLayout(head)
            lay.addWidget(_day_bars(month, per.get('today') or '', 150, month=True))
            lay.addSpacing(SP['2'])
        by_month, by_all = _columns(lay, 2)
        if sessions:
            _add(by_month, label("This month", "StatLabel"))
            if rows['month']:
                _bar_rows(by_month, rows['month'][:8])
            else:
                _add(by_month, label("Nothing played this month yet.", "Muted", wrap=True))
        else:
            by_all = by_month
        _add(by_all, label("All time", "StatLabel"))
        _bar_rows(by_all, rows['all'][:8])

    def _draw_ach(self, view: dict):
        """The unlocked count, then Recent, Closest to 100% and Rarest side
        by side; offline (or nothing cached) Rarest is left out."""
        self._show(self.ach_sec, bool(view['games']))
        if not view['games'] or not self._changed('ach', (view, self._err, self._recent)):
            return
        self._clear(self.ach_sec, self.ach_lay)
        lay = self.ach_lay
        lay.addWidget(self._title("Achievements"))
        lay.addWidget(label(f"{view['unlocked']:,}", "DashBig"))
        n = view['games']
        lay.addWidget(label(f"unlocked across {n} game{'s' if n != 1 else ''}"
                            + (f" · {view['complete']} complete" if view['complete'] else ""),
                            "Muted"))
        lay.addSpacing(SP['2'])
        recent_col, close_col, rare_col = _columns(lay, 3)
        _add(recent_col, label("Recent", "StatLabel"))
        if self._err:
            _add(recent_col, label(f"Recent unlocks unavailable: {self._err}", "Muted",
                                   wrap=True))
        elif self._recent is not None and not view['recent']:
            _add(recent_col, label("Nothing unlocked lately.", "Muted", wrap=True))
        for title, right in view['recent']:
            _note_row(recent_col, title, right)
        if view['closest']:
            _add(close_col, label("Closest to 100%", "StatLabel"))
            _bar_rows(close_col, view['closest'])
        if view['rarest']:
            _add(rare_col, label("Rarest", "StatLabel"))
            for title, right in view['rarest']:
                _note_row(rare_col, title, right)


class SystemPage(QWidget):
    """Activity, Play history, Storage, Health and Report under one top-bar
    icon: a switcher over those pages (their own titles hidden; their
    toolbars stay)."""
    section_changed = pyqtSignal(str)
    SECTIONS = (('activity', "Activity", 'activity'), ('play', "Play history", 'history'),
                ('storage', "Storage", 'storage'), ('health', "Health", 'health'),
                ('report', "Report", 'shield_alert'))

    def __init__(self, pages: dict):
        super().__init__()
        self.pages = pages
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(SP['5'], SP['4'], SP['5'], 0)
        head.setSpacing(SP['3'])
        head.addWidget(label("System", "PageTitle"))
        self.seg = SegmentedControl('pill', 34)
        for key, text, glyph in self.SECTIONS:
            self.seg.add(key, text, G[glyph])
        self.seg.changed.connect(self.show_section)
        head.addWidget(self.seg)
        head.addStretch()
        lay.addLayout(head)
        self.stack = QStackedWidget()
        for key, _t, _g in self.SECTIONS:
            p = pages[key]
            p.page_title.hide()
            m = p.content_lay.contentsMargins()
            p.content_lay.setContentsMargins(m.left(), SP['2'], m.right(), m.bottom())
            self.stack.addWidget(p)
        lay.addWidget(self.stack, 1)
        self.seg.set_current('activity', animate=False)

    def section(self) -> str:
        return self.seg.current()

    def show_section(self, key: str):
        if key not in self.pages:
            return
        if self.seg.current() != key:
            self.seg.set_current(key, animate=self.isVisible())
        self.stack.setCurrentWidget(self.pages[key])
        self.section_changed.emit(key)

    def set_badge(self, key: str, text: str, role: str = 'accent'):
        self.seg.set_badge(key, text, role)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .details import StatTile  # noqa: E402
from .jobs import cli_json, CliQuery  # noqa: E402
from .widgets import (amp, clear_layout, EmptyState, icon_label, label, plain_label,
    SegmentedControl, soft_wrap)  # noqa: E402
