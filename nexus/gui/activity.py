"""The Activity page and strip: one card per job, plus the debug log."""

from pathlib import Path

from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QPlainTextEdit, QProgressBar,
    QPushButton, QRadioButton, QScrollArea, QSplitter, QVBoxLayout, QWidget)

from .fmt import fmt_bytes, fmt_elapsed
from .theme import G, P, SP


class ActivityCard(QFrame):
    """Title, step, progress, elapsed time, Cancel, and (full size) a
    per-job log you can expand. `compact` is the one-line library strip.
    A finished install whose exe was a guess among several lists them, so a
    wrong pick is one click to fix."""
    open_activity = pyqtSignal(object)
    exe_chosen = pyqtSignal(str, str)          # (slug, exe path)

    def __init__(self, job: Job, compact=False):
        super().__init__()
        self.job, self.compact = job, compact
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP['3'], SP['2_5'], SP['3'], SP['2_5'])
        outer.setSpacing(SP['1_5'])
        row = QHBoxLayout()
        row.setSpacing(SP['2_5'])
        self.icon = icon_label(G['download'] if job.kind in ('install', 'install-proton')
                               else G['wrench'], 16)
        row.addWidget(self.icon)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.title = plain_label(job.title)
        self.title.setStyleSheet(f"font-weight: 500; color: {P['text']};")
        self.step = label("", "Muted")
        texts.addWidget(self.title)
        texts.addWidget(self.step)
        row.addLayout(texts, 1)
        self.elapsed = label("", "Muted")
        row.addWidget(self.elapsed)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self._cancel)
        row.addWidget(self.btn_cancel)
        self.btn_log = icon_button(G['terminal'], "Show this job's log", 30)
        self.btn_log.clicked.connect(self._toggle_log)
        row.addWidget(self.btn_log)
        outer.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setObjectName("CardProgress")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        outer.addWidget(self.bar)
        self.detail = label("", "Muted")
        outer.addWidget(self.detail)
        self.detail.setVisible(not compact)
        self.exe_box = None
        self.logview = None
        if compact:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        job.changed.connect(self.refresh)
        job.output.connect(self._append)
        self.refresh()

    def mousePressEvent(self, ev):
        if self.compact:
            self.open_activity.emit(self.job)
        super().mousePressEvent(ev)

    def _toggle_log(self):
        if self.compact:
            self.open_activity.emit(self.job)
            return
        if self.logview is None:
            self.logview = QPlainTextEdit()
            self.logview.setObjectName("Console")
            self.logview.setReadOnly(True)
            self.logview.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            self.logview.setMinimumHeight(220)
            self.logview.setMaximumBlockCount(8000)
            self.logview.setPlainText(''.join(self.job.log))
            self.layout().addWidget(self.logview)
            self.logview.moveCursor(self.logview.textCursor().MoveOperation.End)
        else:
            self.logview.setVisible(not self.logview.isVisible())

    def show_log(self):
        if not self.compact and (self.logview is None or not self.logview.isVisible()):
            self._toggle_log()

    def _cancel(self):
        j = self.job
        if j.kind == 'install' and j.state == 'running':
            # Minutes of copying in: one stray click shouldn't end it.
            ask(self, f"Stop installing {j.name}?",
                "What's been copied so far stays; Resume in the library carries on from it "
                "(or Discard throws it away).", "Stop install", j.cancel)
        else:
            j.cancel()

    def _append(self, text):
        if self.logview is not None:
            self.logview.moveCursor(self.logview.textCursor().MoveOperation.End)
            self.logview.insertPlainText(text)

    def refresh(self):
        j = self.job
        self.title.setText(j.title)
        self.step.setText(j.step)
        color = {'failed': P['danger'], 'done': P['success'], 'cancelled': P['muted']}.get(
            j.state, P['accent'])
        if color != getattr(self, '_color', None):     # a restyle is a repolish: only on change
            self._color = color
            self.icon.setStyleSheet(f"color: {color};")
            self.step.setStyleSheet(
                f"color: {color if j.state in ('failed', 'done') else P['muted']};")
        self.detail.setText(j.detail if j.active else '')
        self.detail.setVisible(not self.compact and bool(j.detail) and j.active)
        self.btn_cancel.setVisible(j.active and j.state != 'cancelling')
        self.bar.setVisible(j.active)
        if j.state == 'queued':
            self.bar.setRange(0, 1)
            self.bar.setValue(0)
        elif j.progress:
            self.bar.setRange(0, j.progress[1])
            self.bar.setValue(min(*j.progress))
        else:
            self.bar.setRange(0, 0)            # busy
        if j.state == 'done' and self.exe_box is None and not self.compact:
            self._exe_choices()
        self.tick()

    def _exe_choices(self):
        ev = self.job.events.get('exe-candidates') or {}
        cands, slug = ev.get('candidates') or [], ev.get('slug')
        if len(cands) < 2 or not slug:
            return
        self.exe_box = QFrame()
        lay = QVBoxLayout(self.exe_box)
        lay.setContentsMargins(0, SP['1'], 0, 0)
        lay.setSpacing(SP['1'])
        lay.addWidget(label("Which file starts the game?", "StatLabel"))
        group = QButtonGroup(self.exe_box)
        for c in cands:
            path = Path(c['path'])
            b = QRadioButton(f"{path.name}  ·  {fmt_bytes(c.get('size'))}  ·  {path.parent.name}")
            b.setToolTip(c['path'])
            b.setChecked(c['path'] == ev.get('chosen'))
            group.addButton(b)
            b.toggled.connect(lambda on, p=c['path']: on and self.exe_chosen.emit(slug, p))
            lay.addWidget(b)
        self.layout().insertWidget(self.layout().indexOf(self.detail) + 1, self.exe_box)

    def tick(self):
        j = self.job
        self.elapsed.setText(fmt_elapsed(j.elapsed()) if j.started else '')


class ActivityPage(QWidget):
    """Every job this session, newest first, plus the raw debug log."""

    def __init__(self, jobs: JobManager):
        super().__init__()
        self.jobs = jobs
        self.cards: dict[int, ActivityCard] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        head = QHBoxLayout()
        self.page_title = label("Activity", "PageTitle")   # hidden inside SystemPage
        self.content_lay = lay
        head.addWidget(self.page_title)
        head.addStretch()
        clear = QPushButton("Clear finished")
        clear.clicked.connect(self._clear)
        head.addWidget(clear)
        self.btn_debug = QPushButton("Debug log ▾")
        self.btn_debug.setToolTip("Raw output of every job (Ctrl+`)")
        self.btn_debug.clicked.connect(lambda: self.show_debug(not self.debug.isVisible()))
        head.addWidget(self.btn_debug)
        lay.addLayout(head)

        self.split = QSplitter(Qt.Orientation.Vertical)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        self.list = QVBoxLayout(host)
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setSpacing(SP['2'])
        self.empty = EmptyState(G['activity'], "Nothing running",
                                "Installs, Steam and Heroic changes, cleanups and checks "
                                "show up here while they run.")
        self.list.addWidget(self.empty)
        self.list.addStretch()
        scroll.setWidget(host)
        self.split.addWidget(scroll)
        self.debug = QPlainTextEdit()
        self.debug.setObjectName("Console")
        self.debug.setReadOnly(True)
        self.debug.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.debug.setMaximumBlockCount(8000)
        self.debug.setVisible(False)
        # Installer output can arrive hundreds of times a second: appended in
        # batches, and only kept (bounded) while the console is hidden.
        self._debug_buf: list[str] = []
        self._debug_len = 0
        self._debug_timer = QTimer(self)
        self._debug_timer.setSingleShot(True)
        self._debug_timer.setInterval(100)
        self._debug_timer.timeout.connect(self._flush_debug)
        self.split.addWidget(self.debug)
        self.split.setChildrenCollapsible(False)
        lay.addWidget(self.split, 1)

        jobs.added.connect(self._add)
        jobs.pruned.connect(self._pruned)
        jobs.output.connect(self._debug_append)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)                # elapsed time, only while jobs run
        self.timer.timeout.connect(self._tick)
        jobs.changed.connect(lambda: (self.timer.isActive() or self.timer.start())
                             if jobs.active() else self.timer.stop())

    exe_chosen = pyqtSignal(str, str)

    def _pruned(self, jobs):
        for job in jobs:
            card = self.cards.pop(id(job), None)
            if card is not None:
                card.hide()
                card.deleteLater()
            # The Job itself goes when nothing (a toast's "Show log") holds it.

    def _add(self, job):
        card = ActivityCard(job)
        card.exe_chosen.connect(self.exe_chosen)
        self.cards[id(job)] = card
        self.list.insertWidget(0, card)
        self.empty.setVisible(False)

    def _tick(self):
        for c in self.cards.values():
            if c.job.state in ('running', 'cancelling'):
                c.tick()

    def _clear(self):
        self.jobs.clear_finished()
        for key, card in list(self.cards.items()):
            if not card.job.active:
                card.hide()
                card.deleteLater()
                del self.cards[key]
        self.empty.setVisible(not self.cards)

    DEBUG_KEEP = 2 << 20                      # chars held back while the console is hidden

    def _debug_append(self, job, text):
        self._debug_buf.append(text)
        self._debug_len += len(text)
        while self._debug_len > self.DEBUG_KEEP and len(self._debug_buf) > 1:
            self._debug_len -= len(self._debug_buf.pop(0))
        if self.debug.isVisible() and not self._debug_timer.isActive():
            self._debug_timer.start()

    def _flush_debug(self):
        if not self._debug_buf:
            return
        text, self._debug_buf, self._debug_len = ''.join(self._debug_buf), [], 0
        self.debug.moveCursor(self.debug.textCursor().MoveOperation.End)
        self.debug.insertPlainText(text)

    def show_debug(self, on=True):
        self.debug.setVisible(on)
        if on:
            self._flush_debug()
        self.btn_debug.setText("Debug log ▴" if on else "Debug log ▾")
        if on:
            h = max(self.split.height(), 400)
            self.split.setSizes([int(h * 0.55), int(h * 0.45)])

    def focus_job(self, job):
        card = self.cards.get(id(job))
        if card:
            card.show_log()


class ActivityStrip(QWidget):
    """Compact card for the running job at the bottom of the library."""
    open_activity = pyqtSignal(object)

    def __init__(self, jobs: JobManager):
        super().__init__()
        self.jobs = jobs
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(SP['5'], 0, SP['5'], SP['3'])
        self.lay.setSpacing(SP['2'])
        self.card = None
        self.more = label("", "Muted")
        self.setVisible(False)
        jobs.changed.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(lambda: self.card and self.card.tick())

    def refresh(self):
        active = self.jobs.active()
        running = next((j for j in active if j.state in ('running', 'cancelling')),
                       active[0] if active else None)
        if running is None:
            self.setVisible(False)
            self.timer.stop()
            return
        if not self.timer.isActive():           # restarting it on every change starved it
            self.timer.start()
        if self.card is None or self.card.job is not running:
            if self.card is not None:
                self.card.hide()
                self.card.deleteLater()
            self.card = ActivityCard(running, compact=True)
            self.card.open_activity.connect(self.open_activity)
            self.lay.insertWidget(0, self.card, 1)
            self.lay.addWidget(self.more)
        queued = len(active) - 1
        self.more.setText(f"+{queued} queued" if queued > 0 else "")
        self.setVisible(True)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .jobs import Job, JobManager  # noqa: E402
from .widgets import ask, EmptyState, icon_button, icon_label, label, plain_label  # noqa: E402
