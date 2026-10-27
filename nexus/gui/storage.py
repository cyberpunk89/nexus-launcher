"""The Storage page: where the space goes and what can be reclaimed."""

import html
import re
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QPointF, QRectF, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QPainter, QPainterPath
from PyQt6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..features import has
from .fmt import fmt_bytes, fmt_hours, fmt_relative, fmt_size
from .theme import P, PAL, pill, R, SP


# Cleanup categories that need a Linux-only feature (features.has).
CATEGORY_NEEDS = {'orphan-prefix': 'compatdata', 'shadercache': 'compatdata',
                  'mesa-cache': 'proton',
                  'game-snapshot': 'btrfs'}


# Segment colours for the per-game storage bar (Rosé Pine accents, then tints).
_SEG_COLORS = [PAL['iris'], PAL['foam'], PAL['rose'], PAL['gold'], PAL['pine'],
               PAL['love'], '#a58fcf', '#7fb4bd', '#c99f9d', '#d1a566']


class StorageBar(QWidget):
    """One stacked bar: a segment per game, then other files, then free."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(22)
        self.segments: list[tuple[str, int, str]] = []   # (label, bytes, color)
        self.other = self.free = self.total = 0
        self.setMouseTracking(True)

    def set_data(self, segments, other, free, total):
        self.segments, self.other, self.free, self.total = segments, other, free, total
        self.update()

    def _rects(self):
        if not self.total:
            return []
        x, w = 0.0, float(self.width())
        out = []
        for name, size, color in [*self.segments,
                                  ("Other files", self.other, P['overlay']),
                                  ("Free", self.free, None)]:
            sw = w * size / self.total
            out.append((QRectF(x, 0, sw, self.height()), name, size, color))
            x += sw
        return out

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r, R['sm'], R['sm'])
        pt.setClipPath(clip)
        pt.fillRect(r, QColor(P['surface_2']))
        for rect, _name, _size, color in self._rects():
            if color:
                pt.fillRect(rect.adjusted(0, 0, -1, 0), QColor(color))
        pt.end()

    def mouseMoveEvent(self, ev):
        for rect, name, size, _c in self._rects():
            if rect.contains(QPointF(ev.position())):
                self.setToolTip(f"{name}: {fmt_size(size) or '0 GB'}")
                return
        self.setToolTip('')


class StoragePage(QWidget):
    """Where the space goes (one bar per drive, a sortable table) and what
    can be reclaimed: a cleanup checklist (to the Trash by default) and the
    quarantine of stray Steam prefixes (restorable)."""
    request = pyqtSignal(list, str, bool, str)
    select_game = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.games: list[dict] = []
        self.disks: list[dict] = []
        self.cands: list[dict] = []
        self.checks: dict[str, QCheckBox] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        host = QWidget()
        scroll.setWidget(host)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        head = QHBoxLayout()
        self.page_title = label("Storage", "PageTitle")   # hidden inside SystemPage
        self.content_lay = lay
        head.addWidget(self.page_title)
        head.addStretch()
        measure = QPushButton("Measure sizes")
        measure.setToolTip("Recompute every repack's size on disk (reads file sizes only)")
        measure.clicked.connect(lambda: self.request.emit(
            ['--list', '--sizes'], "Measuring game sizes", False, 'task'))
        head.addWidget(measure)
        lay.addLayout(head)

        self.disk_box = QVBoxLayout()
        self.disk_box.setSpacing(SP['2'])
        lay.addLayout(self.disk_box)

        # ---- cleanup ----
        cc = QFrame()
        cc.setObjectName("Card")
        cl = QVBoxLayout(cc)
        cl.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        cl.setSpacing(SP['2'])
        top = QHBoxLayout()
        t = label("Clean up", None)
        t.setStyleSheet(f"font-weight: 500; color: {P['text']};")
        top.addWidget(t)
        top.addStretch()
        self.btn_rescan = QPushButton("Re-scan")
        self.btn_rescan.clicked.connect(lambda: self.load_orphans(fresh=True))
        top.addWidget(self.btn_rescan)
        cl.addLayout(top)
        cl.addWidget(label("Nothing is removed until you choose. Selected items go to the Trash, "
                           "so you can still get them back; stray Steam prefixes go to "
                           "quarantine first. Anything that may hold saves is never "
                           "pre-selected.", "Muted", wrap=True))
        self.clean_rows = QVBoxLayout()
        self.clean_rows.setSpacing(SP['1'])
        cl.addLayout(self.clean_rows)
        foot = QHBoxLayout()
        self.sel_label = label("", None)
        self.sel_label.setStyleSheet(f"color: {P['text']};")
        foot.addWidget(self.sel_label, 1)
        self.btn_forever = QPushButton("Delete forever…")
        self.btn_forever.setObjectName("Danger")
        self.btn_forever.clicked.connect(self._ask_forever)
        self.btn_trash = QPushButton("Move to Trash")
        self.btn_trash.setObjectName("Primary")
        self.btn_trash.clicked.connect(lambda: self._clean(False))
        foot.addWidget(self.btn_forever)
        foot.addWidget(self.btn_trash)
        cl.addLayout(foot)
        self.forever_bar = QFrame()
        self.forever_bar.setObjectName("DangerBar")
        fb = QHBoxLayout(self.forever_bar)
        fb.setContentsMargins(SP['3'], SP['2'], SP['2'], SP['2'])
        self.forever_text = label("", None, wrap=True)
        fb.addWidget(self.forever_text, 1)
        no = QPushButton("Cancel")
        no.clicked.connect(lambda: self.forever_bar.setVisible(False))
        yes = QPushButton("Delete forever")
        self._forever = None
        yes.setObjectName("DangerSolid")
        yes.clicked.connect(lambda: self._clean(True))
        fb.addWidget(no)
        fb.addWidget(yes)
        self.forever_bar.setVisible(False)
        cl.addWidget(self.forever_bar)
        self.trash_note = label("", "Muted", wrap=True)
        trow = QHBoxLayout()
        trow.addWidget(self.trash_note, 1)
        open_trash = QPushButton("Open Trash")
        open_trash.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("trash:///")))
        trow.addWidget(open_trash)
        cl.addLayout(trow)
        lay.addWidget(cc)

        # ---- quarantine ----
        self.q_card = QFrame()
        self.q_card.setObjectName("Card")
        ql = QVBoxLayout(self.q_card)
        ql.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        ql.setSpacing(SP['2'])
        self.q_title = label("", None)
        self.q_title.setStyleSheet(f"font-weight: 500; color: {P['text']};")
        ql.addWidget(self.q_title)
        ql.addWidget(label("Stray prefixes you moved out of Steam's way. Steam no longer sees "
                           "them, but they still take space until you trash them. Restore "
                           "puts one back exactly where it was.", "Muted", wrap=True))
        self.q_rows = QVBoxLayout()
        ql.addLayout(self.q_rows)
        self.q_card.setVisible(False)
        lay.addWidget(self.q_card)

        # ---- per-game table ----
        lay.addWidget(label("Games by size", "ShelfTitle"))
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Game", "Source", "Size", "Played",
                                              "Last played", "Added"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(320)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, 6):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(
            lambda r, _c: self.select_game.emit(self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)))
        lay.addWidget(self.table)
        lay.addStretch()

    # ---- drives + table ----
    def set_library(self, games, disks):
        self.games = games
        self.disks = disks if isinstance(disks, list) else ([disks] if disks else [])
        clear_layout(self.disk_box)
        for disk in self.disks:
            self.disk_box.addWidget(self._disk_card(disk))
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(games))
        for r, g in enumerate(games):
            cells = [
                (g.get('display_name', g['slug']), (g.get('display_name') or '').lower()),
                (SOURCE_LABEL.get(g.get('source', 'nexus'), ''), g.get('source', '')),
                (fmt_size(g.get('disk_bytes')) or '—', g.get('disk_bytes') or 0),
                (fmt_hours(g.get('playtime_minutes')) or '—', g.get('playtime_minutes') or 0),
                (fmt_relative(g.get('last_played')) or 'never', g.get('last_played') or ''),
                (fmt_relative(g.get('added_at')) or '—', g.get('added_at') or ''),
            ]
            for c, (text, key) in enumerate(cells):
                it = SortItem(text, key)
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, g['slug'])
                self.table.setItem(r, c, it)
        self.table.setSortingEnabled(True)
        self.table.sortItems(2, Qt.SortOrder.DescendingOrder)

    def _disk_card(self, disk):
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(SP['4'], SP['3'], SP['4'], SP['3'])
        cl.setSpacing(SP['2'])
        top = QHBoxLayout()
        t = label(disk['path'], None)
        t.setStyleSheet(f"font-weight: 500; color: {P['text']};")
        top.addWidget(t)
        top.addStretch()
        top.addWidget(label(f"{fmt_size(disk['free'])} free of {fmt_size(disk['total'])}",
                            "Muted"))
        cl.addLayout(top)
        on = sorted((g for g in self.games if g.get('disk_bytes')
                     and (g.get('dev') == disk.get('dev') or 'dev' not in disk)),
                    key=lambda g: g['disk_bytes'], reverse=True)
        games_total = sum(g['disk_bytes'] for g in on)
        segs = [(g.get('display_name', g['slug']), g['disk_bytes'],
                 _SEG_COLORS[i % len(_SEG_COLORS)]) for i, g in enumerate(on)]
        bar = StorageBar()
        bar.set_data(segs, max(0, disk['used'] - games_total), disk['free'], disk['total'])
        cl.addWidget(bar)
        host = QWidget()
        legend = FlowLayout(host, margin=0, spacing=SP['3'])
        for i, g in enumerate(on[:12]):
            legend.addWidget(self._legend(_SEG_COLORS[i % len(_SEG_COLORS)],
                                          f"{g.get('display_name', g['slug'])} · "
                                          f"{fmt_size(g['disk_bytes'])}"))
        if len(on) > 12:
            legend.addWidget(label(f"+{len(on) - 12} more", "Muted"))
        legend.addWidget(self._legend(P['overlay'], "Other files · "
                                      f"{fmt_size(max(0, disk['used'] - games_total))}"))
        cl.addWidget(host)
        return card

    def _legend(self, color, text):
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(SP['1_5'])
        dot = QLabel()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(f"background: {color}; border-radius: {pill(10)}px;")
        l.addWidget(dot)
        l.addWidget(label(text, "Muted"))
        return w

    # ---- cleanup ----
    def load_orphans(self, fresh: bool = False):
        """(Re)scan cleanup candidates and the quarantine in the background.
        A scan from the last two minutes is reused unless `fresh` (Rescan);
        any job clears it."""
        self.btn_rescan.setEnabled(False)
        clear_layout(self.clean_rows)
        self.clean_rows.addWidget(label("Scanning…", "Muted"))

        def done(data, err):
            try:
                self.btn_rescan.setEnabled(True)
                self._fill_cleanup(data or {}, err)
            except RuntimeError:
                pass
        if fresh:
            _CLI_MEMO.pop(('--cleanup-scan', '--json'), None)
        cli_json(['--cleanup-scan', '--json'], done, 180000, ttl=120)

    def _fill_cleanup(self, data, err):
        clear_layout(self.clean_rows)
        self.checks = {}
        self.forever_bar.setVisible(False)
        self.cands = [c for c in data.get('candidates', [])
                      if c['category'] not in CATEGORY_NEEDS
                      or has(CATEGORY_NEEDS[c['category']])]
        cats = data.get('categories', {})
        if not data:
            self.clean_rows.addWidget(label(f"Couldn't scan: {err}", "Muted", wrap=True))
        elif not self.cands:
            self.clean_rows.addWidget(label("Nothing to clean up.", "Muted"))
        order = list(cats) or sorted({c['category'] for c in self.cands})
        for cat in order:
            items = [c for c in self.cands if c['category'] == cat]
            if not items:
                continue
            meta = cats.get(cat, {'title': cat, 'about': ''})
            head = QHBoxLayout()
            box = QCheckBox(f"{meta['title']}  ·  {fmt_bytes(sum(c['size'] for c in items))}")
            box.setStyleSheet(f"color: {P['text']}; font-weight: 500;")
            head.addWidget(box, 1)
            body = QWidget()
            body_lay = QVBoxLayout(body)
            body_lay.setContentsMargins(0, 0, 0, 0)
            body_lay.setSpacing(SP['1'])
            if len(items) > 3:
                more = QPushButton(f"Show all {len(items)} ▾")
                more.setObjectName("Ghost")
                more.clicked.connect(lambda _=False, b=body, m=more, n=len(items): (
                    b.setVisible(not b.isVisible()),
                    m.setText(f"Hide ▴" if b.isVisible() else f"Show all {n} ▾")))
                head.addWidget(more)
                body.setVisible(False)
            self.clean_rows.addSpacing(SP['2'])
            self.clean_rows.addLayout(head)
            if meta.get('about'):
                self.clean_rows.addWidget(label(meta['about'], "Muted", wrap=True))
            self.clean_rows.addWidget(body)
            children = []
            for c in items:
                row = QHBoxLayout()
                row.setContentsMargins(SP['5'], 0, 0, 0)
                cb = QCheckBox(c['title'])
                cb.setChecked(c['preselect'])
                cb.setToolTip(c['path'])
                cb.toggled.connect(self._update_selected)
                self.checks[c['id']] = cb
                children.append(cb)
                row.addWidget(cb, 1)
                why = label(c['reason'], "Muted")
                row.addWidget(why)
                if c.get('saves'):
                    w = label("may hold saves", None)
                    w.setStyleSheet(f"color: {P['warning']};")
                    row.addWidget(w)
                size = label(fmt_bytes(c['size']), "Muted")
                size.setMinimumWidth(70)
                size.setAlignment(Qt.AlignmentFlag.AlignRight)
                row.addWidget(size)
                body_lay.addLayout(row)
            box.setChecked(all(ch.isChecked() for ch in children))
            box.clicked.connect(lambda on, ch=children: [c.setChecked(on) for c in ch])
        tb = data.get('trash_bytes') or 0
        self.trash_note.setText(
            f"The Trash holds {fmt_size(tb) or 'under 0.1 GB'} — that space is only freed "
            f"once you empty it." if tb else "The Trash is empty.")
        self._fill_quarantine(data.get('quarantine', []))
        self._update_selected()

    def _selected(self):
        return [c for c in self.cands if self.checks.get(c['id']) and
                self.checks[c['id']].isChecked()]

    def _update_selected(self):
        sel = self._selected()
        total = sum(c['size'] for c in sel)
        self.sel_label.setText(f"{len(sel)} selected · {fmt_bytes(total)}"
                               if sel else "Nothing selected")
        self.btn_trash.setEnabled(bool(sel))
        self.btn_forever.setEnabled(bool(sel))
        # The question named a selection; a changed one needs asking again.
        self.forever_bar.setVisible(False)

    def _ask_forever(self):
        sel = self._forever = self._selected()
        saves = [c for c in sel if c.get('saves')]
        self.forever_text.setText(
            f"Permanently delete {len(sel)} item(s), {fmt_size(sum(c['size'] for c in sel))}? "
            f"This can't be undone."
            + (f" {len(saves)} of them may hold saves." if saves else ""))
        self.forever_bar.setVisible(True)

    def _clean(self, forever):
        # Delete forever deletes exactly what the confirmation listed.
        sel = getattr(self, '_forever', None) if forever else self._selected()
        self._forever = None
        if not sel:
            return
        self.forever_bar.setVisible(False)
        args = ['--cleanup', *[c['id'] for c in sel], '--yes']
        if forever:
            args.append('--forever')
        total = fmt_bytes(sum(c['size'] for c in sel))
        self.request.emit(args, f"{'Deleting' if forever else 'Trashing'} {len(sel)} "
                          f"item(s) · {total}", False, 'task')

    def _fill_quarantine(self, items):
        clear_layout(self.q_rows)
        self.q_card.setVisible(bool(items))
        self.q_title.setText(f"Quarantine · {len(items)} item(s) · "
                             f"{fmt_size(sum(q['size'] for q in items)) or '< 0.1 GB'}")
        for q in items:
            row = QHBoxLayout()
            texts = QVBoxLayout()
            texts.setSpacing(0)
            m = re.match(r'(\d+)', q['name'])
            appid = int(m.group(1)) if m else None
            size = fmt_size(q['size']) or '< 0.1 GB'
            if appid is None:
                title, sub = q['name'], ''
            elif appid >= 0x80000000:
                title, sub = "Non-Steam shortcut prefix", f"id {q['name']}"
            else:
                title = f"Steam app {appid}"
                sub = (f"<a href='https://store.steampowered.com/app/{appid}' "
                       f"style='color:{P['accent']}'>store page</a>")
            texts.addWidget(label(f"{title} · {size}"))
            hint = q.get('save_hint') or []
            if hint:
                sub = (sub + " · " if sub else "") + "saves: " + html.escape(", ".join(hint))
            if sub:
                sub_l = label(sub, "Muted")
                sub_l.setOpenExternalLinks(True)
                texts.addWidget(sub_l)
            row.addLayout(texts, 1)
            if q.get('has_savedata'):
                w = _pill("may hold saves", 'warning')
                row.addWidget(w)
            restore = QPushButton("Restore")
            restore.setToolTip(f"Back to {q['origin']}")
            restore.clicked.connect(lambda _=False, n=q['name']: self.request.emit(
                ['--quarantine-restore', n], f"Restoring {n}", True, 'task'))
            trash = QPushButton("Move to Trash")
            trash.clicked.connect(lambda _=False, p=q['path']: self.request.emit(
                ['--cleanup', f"quarantine:{p}", '--yes'], f"Trashing {Path(p).name}",
                False, 'task'))
            row.addWidget(restore)
            row.addWidget(trash)
            self.q_rows.addLayout(row)


class SortItem(QTableWidgetItem):
    """Table cell that shows `text` but sorts by `key`."""

    def __init__(self, text, key):
        super().__init__(text)
        self.key = key

    def __lt__(self, other):
        try:
            return self.key < other.key
        except TypeError:
            return str(self.key) < str(other.key)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .deals import _pill  # noqa: E402
from .gameinfo import SOURCE_LABEL  # noqa: E402
from .jobs import _CLI_MEMO, cli_json  # noqa: E402
from .widgets import clear_layout, FlowLayout, label  # noqa: E402
