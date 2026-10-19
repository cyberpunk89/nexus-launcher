"""The System page: health checks, play history."""

import time
from datetime import datetime

from PyQt6.QtCore import pyqtSignal, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFrame, QGridLayout, QHBoxLayout,
    QProgressBar, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from .fmt import fmt_hours
from .theme import FONTS, FS, G, P, SP


class HealthPage(QWidget):
    request = pyqtSignal(list, str, bool, str)
    counts = pyqtSignal(int, int)          # (problems, failures) for the sidebar badge
    toast = pyqtSignal(str, str)

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
            self.checks = data.get('checks', [])
            self._checked_at = time.time()
            self._render()
        cli_json(['--doctor', '--json'], done, 120000)

    def _render(self):
        clear_layout(self.body)
        problems = [c for c in self.checks if c['status'] in ('warn', 'fail')]
        fails = [c for c in problems if c['status'] == 'fail']
        self.counts.emit(len(problems), len(fails))
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
            b = QPushButton(a['label'])
            b.clicked.connect(lambda _=False, a=a, b=b: (
                b.setEnabled(False),
                self.request.emit(a['args'], f"{a['label']} — {c['title'].split(':')[0]}",
                                  bool(a.get('steam')), 'task')))
            lay.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        return w

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

class WeekBars(QWidget):
    """Hours played per week, oldest left: plain painted bars with the
    week's hours above each and its date below."""

    def __init__(self):
        super().__init__()
        self.weeks: list[dict] = []
        self.setMinimumHeight(170)

    def set_weeks(self, weeks: list[dict]):
        self.weeks = weeks
        self.update()

    def paintEvent(self, ev):
        if not self.weeks:
            return
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(FONTS.ui)
        f.setPixelSize(FS['2xs'])
        pt.setFont(f)
        n = len(self.weeks)
        top, bottom = 18.0, 20.0
        h = self.height() - top - bottom
        slot = self.width() / n
        most = max((w['minutes'] for w in self.weeks), default=0) or 1
        for i, w in enumerate(self.weeks):
            x = i * slot + slot * 0.18
            bw = slot * 0.64
            bh = max(2.0, h * w['minutes'] / most) if w['minutes'] else 2.0
            r = QRectF(x, top + h - bh, bw, bh)
            pt.setPen(Qt.PenStyle.NoPen)
            col = QColor(P['accent'] if w['minutes'] else P['overlay'])
            if w['minutes'] and i != n - 1:
                col.setAlphaF(0.55)                     # this week stands out
            pt.setBrush(col)
            pt.drawRoundedRect(r, 3.0, 3.0)
            pt.setPen(QColor(P['subtext']))
            if w['minutes']:
                pt.drawText(QRectF(i * slot, r.top() - 16, slot, 14),
                            Qt.AlignmentFlag.AlignCenter, f"{w['minutes'] / 60:.0f} h"
                            if w['minutes'] >= 60 else f"{w['minutes']:.0f} m")
            if i % 2 == (n - 1) % 2:
                try:
                    d = datetime.fromisoformat(w['start'])
                    pt.setPen(QColor(P['muted']))
                    pt.drawText(QRectF(i * slot - slot / 2, self.height() - bottom + 4, slot * 2, 14),
                                Qt.AlignmentFlag.AlignCenter, d.strftime('%d %b'))
                except ValueError:
                    pass
        pt.end()


class PlayHistoryPage(QWidget):
    """Your play sessions: the last twelve weeks and this year in review.
    Filled from `nexus-cli --history --json`."""
    reveal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        self.content_lay = lay
        self.page_title = label("Play history", "PageTitle")
        lay.addWidget(self.page_title)
        self.empty = EmptyState(G['history'], "No sessions yet",
                                "Every time you play — a repack, or a Steam or Heroic game "
                                "while Nexus is open — the session is noted here.")
        lay.addWidget(self.empty)
        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(SP['3'])
        self.year_title = label("", None)
        self.year_title.setStyleSheet(f"font-size: {FS['lg']}px; font-weight: 600;")
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
        bl.addStretch()
        lay.addWidget(self.body, 1)
        self.body.setVisible(False)
        self._asked = 0.0

    def refresh(self, force: bool = False):
        if not force and time.monotonic() - self._asked < 30:
            return
        self._asked = time.monotonic()

        def done(data, _err):
            try:
                self.fill(data or {})
            except RuntimeError:
                pass
        cli_json(['--history', '--json'], done, 30000)

    def fill(self, data: dict):
        y = data.get('year') or {}
        has = bool(y.get('sessions'))
        self.empty.setVisible(not has)
        self.body.setVisible(has)
        if not has:
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
        clear_layout(self.top)
        most = max((t['minutes'] for t in y.get('top') or []), default=1) or 1
        for t in y.get('top') or []:
            row = QHBoxLayout()
            b = QPushButton(t['name'])
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


class SystemPage(QWidget):
    """Activity, Storage and Health under one top-bar icon: a switcher over
    the three existing pages (their own titles hidden; their toolbars stay)."""
    section_changed = pyqtSignal(str)
    SECTIONS = (('activity', "Activity", 'activity'), ('play', "Play history", 'history'),
                ('storage', "Storage", 'storage'), ('health', "Health", 'health'))

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
from .widgets import (clear_layout, EmptyState, icon_label, label, plain_label,
    SegmentedControl, soft_wrap)  # noqa: E402
