"""The Storage page: where the space goes and what can be reclaimed."""

import html
import re
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QItemSelectionModel, QPointF, QRectF, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QPainter, QPainterPath
from PyQt6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QMenu,
    QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..features import has
from .common import alive
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
    uninstall = pyqtSignal(list, bool, bool)    # repack slugs, save backup, remove snapshots
    game_action = pyqtSignal(str, str)          # a game-menu action for one game (Steam's)

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
        thead = QHBoxLayout()
        thead.addWidget(label("Games by size", "ShelfTitle"))
        thead.addStretch()
        self.btn_uninstall = QPushButton("Uninstall…")
        self.btn_uninstall.setObjectName("Danger")
        self.btn_uninstall.clicked.connect(self.ask_uninstall)
        thead.addWidget(self.btn_uninstall)
        lay.addLayout(thead)
        lay.addWidget(self._build_uninstall_bar())
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Game", "Source", "Size", "Played",
                                              "Last played", "Added"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context)
        self.table.itemSelectionChanged.connect(self._selection_changed)
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
        self._selection_changed()

    # ---- drives + table ----
    def set_library(self, games, disks):
        self.games = games
        self.disks = disks if isinstance(disks, list) else ([disks] if disks else [])
        clear_layout(self.disk_box)
        for disk in self.disks:
            self.disk_box.addWidget(self._disk_card(disk))
        # The library reloads after every job: keep the rows that were
        # selected, and an open question as long as all its games are here.
        keep = set(self.selected_slugs())
        here = {g['slug'] for g in games}
        if self._un_slugs and not set(self._un_slugs) <= here:
            self.un_bar.setVisible(False)
            self._un_slugs = []
        self.table.blockSignals(True)
        self.table.clearSelection()
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
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) in keep:
                self.table.selectionModel().select(
                    self.table.model().index(r, 0),
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows)
        self.table.blockSignals(False)
        self._selection_changed(keep_question=True)

    # ---- uninstall (w-48) ----
    def _build_uninstall_bar(self) -> QFrame:
        """The inline question, the game page's (DetailsPanel.ask_uninstall)
        for one or several rows: saves backed up first, snapshots kept
        unless ticked (shown only when there are some)."""
        self._un_slugs: list[str] = []          # the rows the question is about
        self._un_games: list[str] = []          # the repacks among them
        self._un_token = 0
        self.un_bar = QFrame()
        self.un_bar.setObjectName("DangerBar")
        cl = QVBoxLayout(self.un_bar)
        cl.setContentsMargins(SP['3'], SP['2_5'], SP['3'], SP['2_5'])
        self.un_text = label("", None, wrap=True)
        cl.addWidget(self.un_text)
        self.un_skip = label("", "Muted", wrap=True)
        self.un_skip.hide()
        cl.addWidget(self.un_skip)
        self.un_backup = QCheckBox("Back up save-game files first, if any are found")
        self.un_backup.setChecked(True)
        if not has('save_backup'):
            self.un_backup.hide()
        cl.addWidget(self.un_backup)
        self.un_snaps = QCheckBox("")
        self.un_snaps.hide()
        cl.addWidget(self.un_snaps)
        row = QHBoxLayout()
        row.addStretch()
        keep = QPushButton("Keep it")
        keep.clicked.connect(self._close_uninstall)
        self.un_go = QPushButton("Uninstall")
        self.un_go.setObjectName("DangerSolid")
        self.un_go.clicked.connect(self._do_uninstall)
        row.addWidget(keep)
        row.addWidget(self.un_go)
        cl.addLayout(row)
        self.un_bar.setVisible(False)
        return self.un_bar

    def selected_slugs(self) -> list[str]:
        """The selected rows' games, top to bottom."""
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [s for r in rows if (it := self.table.item(r, 0))
                and (s := it.data(Qt.ItemDataRole.UserRole))]

    def _games_of(self, slugs) -> list[dict]:
        by = {g['slug']: g for g in self.games}
        return [by[s] for s in slugs if s in by]

    @staticmethod
    def uninstall_kind(g: dict) -> tuple[str, str]:
        """How a row's game is uninstalled: ('repack', ''), ('steam', '') or
        ('', why not here)."""
        src = g.get('source', 'nexus')
        name = g.get('display_name', g['slug'])
        if g.get('playing') or g.get('launching'):
            return '', f"{name} is running — close it first"
        if g.get('busy'):
            return '', f"{name} is busy — wait for its current job"
        if src == 'nexus':
            if g.get('incomplete'):
                return '', f"{name} is an unfinished install — discard it from its page"
            return 'repack', ''
        if src == 'steam' and g.get('appid') and _supported('steam-uninstall'):
            return 'steam', ''
        return '', f"{name}: uninstall it in {SOURCE_LABEL.get(src) or 'its launcher'}"

    def _plan(self, slugs):
        """(repacks, steam games, reasons for the rest) for a selection."""
        repacks, steam, why = [], [], []
        for g in self._games_of(slugs):
            kind, reason = self.uninstall_kind(g)
            (repacks if kind == 'repack' else steam if kind == 'steam' else why).append(
                g if kind else reason)
        return repacks, steam, why

    def _selection_changed(self, keep_question=False):
        slugs = self.selected_slugs()
        repacks, steam, _why = self._plan(slugs)
        steam_only = bool(steam) and not repacks and len(slugs) == 1
        self.btn_uninstall.setText("Uninstall in Steam…" if steam_only else
                                   f"Uninstall {len(repacks)} games…" if len(repacks) > 1
                                   else "Uninstall…")
        self.btn_uninstall.setEnabled(bool(repacks) or steam_only)
        if not slugs:
            self.btn_uninstall.setToolTip("Select one or more games in the table")
        elif not repacks and not steam_only:
            self.btn_uninstall.setToolTip("; ".join(_why) or "Nothing here can be uninstalled")
        else:
            self.btn_uninstall.setToolTip("")
        # The question named a selection; a changed one needs asking again.
        if not keep_question and self.un_bar.isVisible() and set(slugs) != set(self._un_slugs):
            self._close_uninstall()

    def _menu_entries(self):
        """The right-click menu for the selection: (text, callback, enabled)."""
        slugs = self.selected_slugs()
        out = []
        if len(slugs) == 1:
            out.append(("Open game page", lambda s=slugs[0]: self.select_game.emit(s), True))
        out.append((self.btn_uninstall.text(), self.ask_uninstall,
                    self.btn_uninstall.isEnabled()))
        return out

    def _context(self, pos):
        r = self.table.rowAt(pos.y())
        if r < 0:
            return
        if not self.table.item(r, 0).isSelected():
            self.table.selectRow(r)
        m = QMenu(self)
        for text, fn, on in self._menu_entries():
            a = m.addAction(text, fn)
            a.setEnabled(on)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def ask_uninstall(self):
        """Uninstall… for the selected rows: one Steam game goes to Steam's own
        question; repacks get the inline question (several at once allowed)."""
        slugs = self.selected_slugs()
        repacks, steam, why = self._plan(slugs)
        if not repacks:
            if len(slugs) == 1 and steam:
                self.game_action.emit('steam-uninstall', steam[0]['slug'])
            return
        why += [f"{g.get('display_name', g['slug'])}: one at a time, Steam asks for each"
                for g in steam]
        self._un_slugs = slugs
        self._un_games = [g['slug'] for g in repacks]
        size = sum(g.get('disk_bytes') or 0 for g in repacks)
        gb = f" ({fmt_size(size)})" if fmt_size(size) else ''
        if len(repacks) == 1:
            name = repacks[0].get('display_name', repacks[0]['slug'])
            self.un_text.setText(
                f"Uninstall {name}? This deletes its install folder{gb} and removes it from "
                f"Steam, Heroic and the app menu. Logs are kept.")
            self.un_go.setText("Uninstall")
        else:
            names = ", ".join(g.get('display_name', g['slug']) for g in repacks[:4]) \
                + ("…" if len(repacks) > 4 else "")
            self.un_text.setText(
                f"Uninstall {len(repacks)} games{gb}? {names}. Their install folders are "
                f"deleted and they are removed from Steam, Heroic and the app menu. "
                f"Logs are kept.")
            self.un_go.setText(f"Uninstall {len(repacks)} games")
        self.un_skip.setText("Left out: " + "; ".join(why) + "." if why else "")
        self.un_skip.setVisible(bool(why))
        self.un_snaps.setChecked(False)
        self.un_snaps.hide()
        self._un_token += 1
        if has('btrfs'):
            self._count_snapshots([g['slug'] for g in repacks if g.get('owned_dir')])
        self.un_bar.setVisible(True)

    def _count_snapshots(self, slugs):
        """Ask the CLI how many snapshots the games have; the box appears only
        when there are some (they hold copies of the saves)."""
        token, counts = self._un_token, {}

        def done(data, _err, slug):
            if token != self._un_token or not alive(self.un_snaps):
                return
            counts[slug] = len((data or {}).get('snapshots') or [])
            n = sum(counts.values())
            if n:
                one = len(self._un_games) == 1
                self.un_snaps.setText(
                    f"Also delete {'its' if one else 'their'} {n} snapshot{'s' if n > 1 else ''}"
                    f" (they hold copies of {'its' if one else 'their'} saves)")
                self.un_snaps.show()
        for s in slugs:
            cli_json(['--snapshots', s, '--json'], lambda d, e, s=s: done(d, e, s), 30000)

    def _close_uninstall(self):
        self.un_bar.setVisible(False)
        self._un_slugs = []
        self._un_token += 1

    def _do_uninstall(self):
        slugs = list(self._un_games)
        backup = has('save_backup') and self.un_backup.isChecked()
        snaps = not self.un_snaps.isHidden() and self.un_snaps.isChecked()
        self._close_uninstall()
        if slugs:
            self.uninstall.emit(slugs, backup, snaps)

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
from .actions import _supported  # noqa: E402
from .deals import _pill  # noqa: E402
from .gameinfo import SOURCE_LABEL  # noqa: E402
from .jobs import _CLI_MEMO, cli_json  # noqa: E402
from .widgets import clear_layout, FlowLayout, label  # noqa: E402
