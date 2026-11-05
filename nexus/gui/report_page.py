"""System > Report: turn a crash (or a problem without one) into a GitHub
issue the user has read line by line.

The CLI does the work, and the window never blocks on it:
--report-pending lists the crash files, --report-collect gathers a scrubbed
draft (slow: Doctor runs), --report-preview renders it with the user's
choices and --report-url turns it into the prefilled issue link. This page
never renders a report itself, so what it shows is what the CLI would send.
Nothing leaves the computer without a button press: Open on GitHub only
opens the browser on a prefilled form, which the user still submits, and
Send anonymously (--report-send) posts the same text to the reports relay,
which holds it in a private inbox until it is checked. --report-status says
on load whether the relay takes reports; the button greys out with the
reason when it doesn't, and is hidden when [report] relay = false.
Your reports (--report-sent) lists what was sent from here with the state
the daily update check last heard ("#123 fixed in 0.5.4"), and a draft whose
crash signature matches a public issue says so above the choices
(--report-similar)."""

from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout,
    QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .. import platform
from .theme import FS, G, P, SP

MANUAL = ''                          # the picker's "no crash file" entry
KIND_TEXT = {'gui-exception': "Window error", 'cli-internal': "Command-line error",
             'native-crash': "Nexus crashed", 'game-failure': "Game didn't run",
             'user-report': "Problem report"}
PROTON_HINT = "Adding the Proton log helps most with game crashes."
GAME_HINT = "The game's title and appid. Off unless you tick it, for this report only."
DEBOUNCE_MS = 300
GITHUB = 'https://github.com/'
GH_USER = r'[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}'


def when_text(stamp: str | None) -> str:
    """'2026-11-05T12:00:00Z' -> '5 Nov, 13:00' in local time ('' if unknown)."""
    if not stamp:
        return ''
    try:
        t = datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return ''
    t = t.astimezone()
    return f"{t.day} {t.strftime('%b, %H:%M')}"        # no %-d: Windows has none


def crash_title(r: dict) -> str:
    kind = KIND_TEXT.get(r.get('kind'), "Problem report")
    typ = str(r.get('type') or '').strip()
    return f"{kind}: {typ}" if typ and typ != 'unknown' else kind


def crash_detail(r: dict) -> str:
    n = int(r.get('count') or 1)
    parts = [f"{n} times" if n > 1 else "once",
             f"last {when_text(r.get('last'))}" if when_text(r.get('last')) else '',
             f"Nexus {r['version']}" if r.get('version') else '',
             f"sig {r['signature']}" if r.get('signature') else '']
    return " · ".join(p for p in parts if p)


class PreviewLine(QWidget):
    """One numbered line of the report, with Remove / Undo."""

    def __init__(self, line: dict, toggled, parent=None):
        super().__init__(parent)
        self.n = line['n']
        self.original = line['text']
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['2'])
        # Styled by object name in the app sheet: a report can have a few
        # hundred lines, and per-widget sheets are slow to apply.
        num = label(str(self.n), "ReportLineNo")
        num.setFixedWidth(28)
        num.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        lay.addWidget(num, 0, Qt.AlignmentFlag.AlignTop)
        self.text = plain_label('', None, wrap=True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.text, 1)
        self.button = QPushButton("Remove")
        self.button.setObjectName("LineAction")
        self.button.setToolTip("Leave this line out of the report")
        self.button.clicked.connect(lambda: toggled(self.n))
        lay.addWidget(self.button, 0, Qt.AlignmentFlag.AlignTop)
        self.removed = None
        self.set_removed(bool(line.get('removed')))

    def set_removed(self, removed: bool):
        if removed == self.removed:
            return
        self.removed = removed
        self.text.setObjectName("ReportLineOff" if removed else "ReportLine")
        self.text.style().unpolish(self.text)
        self.text.style().polish(self.text)
        self.text.setText("[line removed by you]" if removed else soft_wrap(self.original))
        self.text.setToolTip(self.original if removed else '')
        self.button.setText("Undo" if removed else "Remove")
        self.button.setToolTip("Put this line back" if removed
                               else "Leave this line out of the report")


class ReportPage(QWidget):
    """Pick a crash, tick what to share, read the exact lines, then open
    the prefilled GitHub form, copy the text or save it."""
    toast = pyqtSignal(str, str)
    dismissed = pyqtSignal(str)            # a crash file the user forgot

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending: list[dict] = []
        self._want = None                  # a crash id to select on the next load
        self._loaded = False
        self._gen = 0                      # drops answers for an older draft
        self._draft = None                 # {draft, title, kind, groups, default_exclude}
        self._preview = None               # the last --report-preview answer
        self._removed: set[int] = set()
        self._relay = None                 # the last --report-status answer
        self._sending = False
        self._checks: dict[str, QCheckBox] = {}
        self._rows: dict[int, PreviewLine] = {}
        self._heads: dict[str, QWidget] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._refresh_preview)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        self.content_lay = lay
        head = QHBoxLayout()
        head.setSpacing(SP['2'])
        self.page_title = label("Report a problem", "PageTitle")   # hidden inside SystemPage
        head.addWidget(self.page_title)
        self.pick = NoWheelCombo()
        self.pick.setMinimumWidth(260)
        self.pick.setMaximumWidth(560)
        self.pick.setToolTip("Which problem to report")
        self.pick.activated.connect(self._picked)
        head.addWidget(self.pick, 1)
        self.btn_forget = QPushButton("Forget this crash")
        self.btn_forget.setToolTip("Delete this crash file without reporting it")
        self.btn_forget.clicked.connect(self._forget)
        head.addWidget(self.btn_forget)
        head.addStretch()
        lay.addLayout(head)

        self.summary = QFrame()
        self.summary.setObjectName("Card")
        srow = QHBoxLayout(self.summary)
        srow.setContentsMargins(SP['3'], SP['2_5'], SP['3'], SP['2_5'])
        srow.setSpacing(SP['2_5'])
        self.summary_icon = icon_label(G['shield_alert'], 18, P['accent'])
        srow.addWidget(self.summary_icon, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(SP['0_5'])
        self.summary_title = plain_label("Report a problem", None, wrap=True)
        self.summary_title.setStyleSheet(f"font-size: {FS['sm']}px; font-weight: 600;")
        texts.addWidget(self.summary_title)
        self.summary_detail = label("", "Muted", wrap=True)
        texts.addWidget(self.summary_detail)
        srow.addLayout(texts, 1)
        lay.addWidget(self.summary)

        # "This looks like #N": a public issue with the draft's signature.
        self.similar = QFrame()
        self.similar.setObjectName("Card")
        mrow = QHBoxLayout(self.similar)
        mrow.setContentsMargins(SP['3'], SP['2'], SP['3'], SP['2'])
        mrow.setSpacing(SP['2_5'])
        mrow.addWidget(icon_label(G['info'], 16, P['info']), 0, Qt.AlignmentFlag.AlignTop)
        self.similar_text = label("", "Muted", wrap=True)
        mrow.addWidget(self.similar_text, 1)
        self.similar_open = QPushButton("Open issue")
        self.similar_open.setToolTip("Opens the public issue in your browser")
        self.similar_open.clicked.connect(lambda: self._open(self._similar_url))
        mrow.addWidget(self.similar_open, 0, Qt.AlignmentFlag.AlignTop)
        self._similar_url = None
        self.similar.setVisible(False)
        lay.addWidget(self.similar)

        body = QHBoxLayout()
        body.setSpacing(SP['4'])
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, SP['1'], 0)
        ll.setSpacing(SP['2'])
        ll.addWidget(label("What to include", "StatLabel"))
        self.groups_card = QFrame()
        self.groups_card.setObjectName("Card")
        self.groups_lay = QVBoxLayout(self.groups_card)
        self.groups_lay.setContentsMargins(SP['3'], SP['2'], SP['3'], SP['2'])
        self.groups_lay.setSpacing(SP['1_5'])
        self.groups_wait = label("Gathering…", "Muted")
        self.groups_lay.addWidget(self.groups_wait)
        ll.addWidget(self.groups_card)
        ll.addSpacing(SP['1'])
        ll.addWidget(label("What happened", "StatLabel"))
        self.describe = QPlainTextEdit()
        self.describe.setObjectName("Describe")
        self.describe.setPlaceholderText("What were you doing, and what did you expect? "
                                         "(optional)")
        self.describe.setTabChangesFocus(True)
        self.describe.setFixedHeight(96)
        self.describe.textChanged.connect(self._changed)
        ll.addWidget(self.describe)
        ll.addSpacing(SP['1'])
        ll.addWidget(label("Contact (only for Send anonymously)", "StatLabel"))
        self.contact = QLineEdit()
        self.contact.setObjectName("ReportContact")
        self.contact.setPlaceholderText("Your GitHub username (optional)")
        self.contact.setMaxLength(39)
        from PyQt6.QtCore import QRegularExpression
        from PyQt6.QtGui import QRegularExpressionValidator
        self.contact.setValidator(QRegularExpressionValidator(
            QRegularExpression('@?' + GH_USER), self.contact))
        self.contact.setToolTip("Kept in the private inbox so we can ask you about it. It "
                                "never goes on the public issue unless you tick Mention me.")
        self.contact.textChanged.connect(self._contact_changed)
        ll.addWidget(self.contact)
        self.mention = QCheckBox("Mention me on the public issue")
        self.mention.setToolTip("Puts @username on the issue, so GitHub tells you when it "
                                "changes")
        self.mention.setEnabled(False)
        ll.addWidget(self.mention)
        privacy = label("Your home folder, user name, game names and paths are replaced "
                        "before you see the preview. Nothing leaves this computer until you "
                        "press a button below. Send anonymously needs no GitHub account: the "
                        "report waits in a private inbox until it is checked.", "Muted",
                        wrap=True)
        ll.addWidget(privacy)
        ll.addSpacing(SP['1'])
        sent_body = QWidget()
        self.sent_lay = QVBoxLayout(sent_body)
        self.sent_lay.setContentsMargins(SP['1'], 0, 0, 0)
        self.sent_lay.setSpacing(SP['1_5'])
        self.sent = Disclosure('report-sent', "Your reports", G['history'], sent_body)
        self.sent.setToolTip("Reports sent anonymously from this computer, checked once a "
                             "day with the update check. Kept 90 days.")
        self.sent.setVisible(False)
        self._sent: list[dict] = []
        ll.addWidget(self.sent)
        ll.addStretch()
        side = QScrollArea()                  # a short window scrolls the choices
        side.setWidgetResizable(True)
        side.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side.setFixedWidth(300)
        side.setWidget(left)
        body.addWidget(side)

        right = QVBoxLayout()
        right.setSpacing(SP['2'])
        phead = QHBoxLayout()
        phead.addWidget(label("Preview", "StatLabel"))
        phead.addStretch()
        self.length = label("", "Muted")
        phead.addWidget(self.length)
        right.addLayout(phead)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("ReportPreview")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QFrame()
        host.setObjectName("Card")
        self.lines_lay = QVBoxLayout(host)
        self.lines_lay.setContentsMargins(SP['3'], SP['2_5'], SP['3'], SP['2_5'])
        self.lines_lay.setSpacing(SP['0_5'])
        self.preview_title = plain_label("", None, wrap=True)
        self.preview_title.setStyleSheet(f"color: {P['text']}; font-weight: 600;")
        self.lines_lay.addWidget(self.preview_title)
        self.lines_lay.addStretch()
        self.scroll.setWidget(host)
        right.addWidget(self.scroll, 1)
        body.addLayout(right, 1)
        lay.addLayout(body, 1)

        foot = QHBoxLayout()
        foot.setSpacing(SP['2'])
        self.status = label("", "Muted", wrap=True)
        foot.addWidget(self.status, 1)
        self.btn_save = QPushButton("Save to file…")
        self.btn_save.clicked.connect(self._save)
        foot.addWidget(self.btn_save)
        self.btn_copy = QPushButton("Copy report")
        self.btn_copy.clicked.connect(self._copy)
        foot.addWidget(self.btn_copy)
        self.btn_github = QPushButton("Open on GitHub")
        self.btn_github.setObjectName("Primary")
        self.btn_github.setToolTip("Opens a prefilled issue form in your browser; you "
                                   "submit it there")
        self.btn_github.clicked.connect(self._github)
        foot.addWidget(self.btn_github)
        self.btn_send = QPushButton("Send anonymously")
        self.btn_send.setToolTip("Sends exactly the preview through the Nexus reports relay. "
                                 "No GitHub account needed")
        self.btn_send.clicked.connect(self._send)
        foot.addWidget(self.btn_send)
        lay.addLayout(foot)
        self._set_ready(False)
        self.btn_forget.setVisible(False)

    # ---- loading ----
    def want(self, crash_id: str | None = None):
        """Select that crash the next time the page shows (None: the newest
        one, or a report without a crash file when there is none)."""
        self._want = crash_id
        self._loaded = False

    def shown(self):
        """The section came on screen: load once, then keep what is there."""
        if not self._loaded:
            self.reload()

    def reload(self):
        self._loaded = True
        self._gen += 1
        self._set_ready(False)
        cli_json(['--report-status', '--json'], self._on_status, 30000)
        self.status.setText("Looking for crash reports…")
        cli_json(['--report-pending', '--json'], self._on_pending, 30000)
        self.load_sent()

    # ---- Your reports ----
    def load_sent(self):
        """The reports sent from here (--report-sent reads sent.jsonl; the
        relay is only asked by the daily check)."""
        if self._loaded:
            cli_json(['--report-sent', '--json'], self._on_sent, 30000)

    def _on_sent(self, data, _err):
        self._sent = [r for r in (data or {}).get('reports') or [] if r.get('id')]
        clear_layout(self.sent_lay)
        for r in self._sent:
            self.sent_lay.addWidget(self._sent_row(r))
        n = len(self._sent)
        self.sent.setVisible(bool(n))
        fixed = sum(1 for r in self._sent if r.get('state') == 'fixed')
        self.sent.set_summary(f"{n}" + (f" · {fixed} fixed" if fixed else ''))

    def _sent_row(self, r: dict) -> QWidget:
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(SP['1_5'])
        texts = QVBoxLayout()
        texts.setSpacing(0)
        state = plain_label(r.get('text') or '', None, wrap=True)
        color = {'fixed': P['success'], 'open': P['info'], 'closed': P['muted']}.get(
            r.get('state'), P['text'])
        state.setStyleSheet(f"color: {color}; font-size: {FS['sm']}px; font-weight: 600;")
        texts.addWidget(state)
        when = when_text(r.get('at'))
        what = r.get('title') or KIND_TEXT.get(r.get('kind'), "Problem report")
        texts.addWidget(label(f"{what}" + (f" · {when}" if when else ''), "Muted", wrap=True))
        hl.addLayout(texts, 1)
        if (r.get('url') or '').startswith(GITHUB):
            b = QPushButton(G['external'])
            b.setObjectName("LineAction")
            b.setToolTip("Open the public issue")
            b.clicked.connect(lambda _=False, u=r['url']: self._open(u))
            hl.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        rm = QPushButton("Remove")
        rm.setObjectName("LineAction")
        rm.setToolTip("Drop it from this list (the report itself stays where it is)")
        rm.clicked.connect(lambda _=False, sid=r['id']: self._remove_sent(sid))
        hl.addWidget(rm, 0, Qt.AlignmentFlag.AlignTop)
        return row

    def _remove_sent(self, sid: str):
        def done(data, err):
            if not (data or {}).get('removed'):
                self.toast.emit(f"Couldn't remove it: {err or 'it was already gone'}", 'error')
            self.load_sent()
        cli_json(['--report-sent-remove', sid, '--json'], done, 30000)

    def _open(self, url):
        if url and url.startswith(GITHUB) and not platform.open_url(url):
            self.toast.emit("Couldn't open the browser.", 'error')

    # ---- a public issue like this one ----
    def _check_similar(self, gen):
        if not self._draft or (self._relay or {}).get('off'):
            return
        cli_json(['--report-similar', self._draft['draft'], '--json'],
                 lambda d, e: self._on_similar(gen, d, e), 30000)

    def _on_similar(self, gen, data, _err):
        if gen != self._gen:
            return
        m = (data or {}).get('match')
        url = (m or {}).get('url') or ''
        if not m or not url.startswith(GITHUB):
            self.similar.setVisible(False)
            return
        n = m.get('issue')
        if m.get('state') == 'fixed':
            text = (f"This looks like #{n}, fixed in {m['fixed_in']}. Updating Nexus should "
                    "fix it; send this report if it still happens there.")
        elif m.get('state') == 'closed':
            text = f"This looks like #{n}, which is closed. Send it if it still matters."
        else:
            text = (f"This looks like #{n}, already open. Sending counts it once more; you "
                    "can also follow the issue.")
        self._similar_url = url
        self.similar_text.setText(text)
        self.similar.setVisible(True)

    def _on_pending(self, data, err):
        self._pending = [r for r in (data or {}).get('reports') or [] if r.get('id')]
        want, self._want = self._want, None
        self.pick.blockSignals(True)
        self.pick.clear()
        for r in self._pending:
            when = when_text(r.get('last'))
            self.pick.addItem(crash_title(r) + (f"  ·  {when}" if when else ''), r['id'])
        self.pick.addItem("A problem without a crash report", MANUAL)
        ids = [r['id'] for r in self._pending]
        self.pick.setCurrentIndex(ids.index(want) if want in ids else
                                  len(ids) if want == MANUAL else 0)
        self.pick.blockSignals(False)
        self._collect()

    def _picked(self, _i=None):
        self._collect()

    def current_crash(self) -> dict | None:
        cid = self.pick.currentData()
        return next((r for r in self._pending if r['id'] == cid), None) if cid else None

    def _collect(self):
        self._gen += 1
        gen = self._gen
        self._draft = self._preview = None
        self._removed.clear()
        self._timer.stop()
        self._clear_draft_widgets()
        self.similar.setVisible(False)
        r = self.current_crash()
        self.btn_forget.setVisible(r is not None)
        if r is None:
            self.summary_icon.setText(G['shield_alert'])
            self.summary_title.setText("Report a problem")
            self.summary_detail.setText("Something misbehaved without a crash? Describe it, "
                                        "and Nexus adds what helps to track it down.")
        else:
            self.summary_icon.setText(G['fail'] if r.get('kind') == 'native-crash'
                                      else G['warn'])
            self.summary_title.setText(crash_title(r))
            self.summary_detail.setText(crash_detail(r))
        self._set_ready(False)
        self.status.setText("Gathering your system details (a few seconds)…")
        args = ['--report-collect', '--json'] + (['--crash', r['id']] if r else [])
        cli_json(args, lambda d, e: self._on_collect(gen, d, e), 120000)

    def _on_collect(self, gen, data, err):
        if gen != self._gen:
            return
        if not data or not data.get('draft'):
            self.status.setText(f"Couldn't gather the report: {err or 'no answer'}")
            self.groups_wait.setText("Nothing gathered.")
            return
        self._draft = data
        self.status.setText("")
        self._refresh_preview()
        self._check_similar(gen)

    # ---- choices ----
    def selection_args(self) -> list[str]:
        """The CLI options for what the user ticked, removed and wrote."""
        args = []
        if self._checks:
            out = [k for k, cb in self._checks.items() if k != 'game' and not cb.isChecked()]
            args.append(f"--exclude={','.join(out)}")      # empty: every group
            if 'game' in self._checks and self._checks['game'].isChecked():
                args.append('--with-game')
        for n in sorted(self._removed):
            args.append(f"--remove-line={n}")
        text = self.describe.toPlainText().strip()
        if text:
            args.append(f"--describe={text}")
        return args

    def _changed(self, *_):
        if self._draft:
            self._timer.start()             # 300 ms after the last change

    def _toggle_line(self, n: int):
        if n in self._removed:
            self._removed.discard(n)
        else:
            self._removed.add(n)
        row = self._rows.get(n)
        if row is not None:
            row.set_removed(n in self._removed)
        self._changed()

    # ---- the preview ----
    def _refresh_preview(self):
        if not self._draft:
            return
        gen = self._gen
        cli_json(['--report-preview', self._draft['draft'], *self.selection_args(), '--json'],
                 lambda d, e: self._on_preview(gen, d, e), 30000)

    def _on_preview(self, gen, data, err):
        if gen != self._gen:
            return
        if not data or 'lines' not in data:
            self.status.setText(f"Couldn't build the preview: {err or 'no answer'}")
            return
        self._preview = data
        if not self._checks:
            self._build_checks(data.get('groups') or [])
        if not self._rows:
            self._build_rows(data.get('lines') or [], data.get('groups') or [])
        self.fill_preview(data)
        self._set_ready(True)

    def _build_checks(self, groups: list[dict]):
        clear_layout(self.groups_lay)
        self._checks = {}
        kind = (self._draft or {}).get('kind')
        for g in groups:
            n = g.get('lines') or 0
            cb = QCheckBox(f"{g['label']}  ·  {n} line{'s' if n != 1 else ''}",
                           self.groups_card)
            cb.setChecked(bool(g.get('included')))
            if g['key'] == 'app':
                cb.setEnabled(False)
                cb.setToolTip("Always included: without the version a report can't be used")
            elif g['key'] == 'game':
                cb.setToolTip(GAME_HINT)
            self.groups_lay.addWidget(cb)
            cb.toggled.connect(self._changed)
            self._checks[g['key']] = cb
            if g['key'] == 'proton_log' and kind == 'game-failure':
                hint = label(PROTON_HINT, "Muted", wrap=True)
                hint.setObjectName("ReportHint")
                hint.setStyleSheet(f"color: {P['info']}; padding-left: {SP['6']}px;")
                self.groups_lay.addWidget(hint)
        if not groups:
            self.groups_lay.addWidget(label("Nothing to include.", "Muted"))

    def _build_rows(self, lines: list[dict], groups: list[dict]):
        names = {g['key']: g['label'] for g in groups}
        host = self.lines_lay.parentWidget()
        self.lines_lay.takeAt(self.lines_lay.count() - 1)        # the stretch
        group = None
        for ln in lines:
            if ln['group'] != group:
                group = ln['group']
                h = label(names.get(group, group), "StatLabel")
                self.lines_lay.addSpacing(SP['1'])
                self.lines_lay.addWidget(h)
                self._heads[group] = h
            row = PreviewLine(ln, self._toggle_line, host)
            self.lines_lay.addWidget(row)
            self._rows[ln['n']] = row
        self.lines_lay.addStretch()

    def fill_preview(self, data: dict):
        self.preview_title.setText(data.get('title') or '')
        shown = {k: False for k in self._heads}
        for ln in data.get('lines') or []:
            row = self._rows.get(ln['n'])
            if row is None:
                continue
            row.setVisible(bool(ln['included']))
            row.set_removed(bool(ln['removed']))
            if ln['included']:
                shown[ln['group']] = True
        for k, h in self._heads.items():
            h.setVisible(shown.get(k, False))
        n = int(data.get('length') or 0)
        self.length.setText(f"{n:,} characters")

    def _clear_draft_widgets(self):
        clear_layout(self.groups_lay)
        self._checks = {}
        self.groups_wait = label("Gathering…", "Muted")
        self.groups_lay.addWidget(self.groups_wait)
        while self.lines_lay.count() > 1:          # keep the title
            item = self.lines_lay.takeAt(1)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self.lines_lay.addStretch()
        self._rows, self._heads = {}, {}
        self.preview_title.setText('')
        self.length.setText('')

    def _set_ready(self, on: bool):
        for b in (self.btn_github, self.btn_copy, self.btn_save):
            b.setEnabled(on)
        self._ready = on
        self._update_send()

    # ---- the relay ----
    def _on_status(self, data, err):
        self._relay = data or {'enabled': False,
                               'reason': f"Couldn't reach the relay: {err or 'no answer'}"}
        self._update_send()

    def relay_reason(self) -> str | None:
        """Why Send anonymously is off, or None when it can be pressed."""
        r = self._relay
        if r is None:
            return "Checking whether the relay takes reports…"
        if self._sending:
            return "Sending…"
        return None if r.get('enabled') else (r.get('reason') or "The relay is off.")

    def _update_send(self):
        r = self._relay or {}
        hidden = bool(r.get('off'))          # [report] relay = false
        self.btn_send.setVisible(not hidden)
        self.contact.setEnabled(not hidden)
        reason = self.relay_reason()
        self.btn_send.setEnabled(bool(getattr(self, '_ready', False)) and reason is None)
        self.btn_send.setToolTip(reason or "Sends exactly the preview through the Nexus "
                                           "reports relay. No GitHub account needed")

    def _contact_changed(self, text):
        self.mention.setEnabled(bool(text.strip().lstrip('@')))
        if not self.mention.isEnabled():
            self.mention.setChecked(False)

    def contact_args(self) -> list[str]:
        name = self.contact.text().strip().lstrip('@')
        out = ['--contact', name] if name and self.contact.hasAcceptableInput() else []
        return out + (['--mention-me'] if out and self.mention.isChecked() else [])

    def _send(self):
        if not self._draft or self.relay_reason():
            return
        self._timer.stop()
        gen = self._gen
        self._sending = True
        self._update_send()
        self.status.setText("Sending the report…")

        def done(data, err):
            self._sending = False
            self._update_send()
            if gen != self._gen:
                return
            data = data or {}
            if 'error' in data or not data.get('status'):
                e = data.get('error') or {}
                msg = e.get('message') if isinstance(e, dict) else None
                self.status.setText(f"Not sent: {msg or err or 'no answer'}")
                self.toast.emit(f"The report wasn't sent: {msg or err or 'no answer'}",
                                'error')
                return
            if data['status'] == 'counted':
                msg = "Sent. This problem was already reported, so it now counts one more."
            else:
                msg = ("Sent. It waits in a private inbox until it is checked, then goes "
                       "public without your contact.")
            if data.get('issue'):
                msg += f" Issue: {data['issue']}"
            self.status.setText(msg + (" You can forget this crash now."
                                       if self.current_crash() else ''))
            self.toast.emit("Report sent. Thank you!", 'success')
            self.load_sent()
        cli_json(['--report-send', self._draft['draft'], *self.selection_args(),
                  *self.contact_args(), '--json'], done, 60000)

    # ---- the buttons ----
    def _render(self, then):
        """The current report from the CLI, then `then(title, body)`."""
        if not self._draft:
            return
        self._timer.stop()
        gen = self._gen

        def done(data, err):
            if gen != self._gen:
                return
            if not data or 'body' not in data:
                self.toast.emit(f"Couldn't build the report: {err or 'no answer'}", 'error')
                return
            self._on_preview(gen, data, err)
            then(data.get('title') or '', data['body'])
        cli_json(['--report-preview', self._draft['draft'], *self.selection_args(), '--json'],
                 done, 30000)

    def _copy(self):
        def copy(title, body):
            QApplication.clipboard().setText(f"{title}\n\n{body}")
            self.toast.emit("Report copied to the clipboard.", 'success')
        self._render(copy)

    def _save(self):
        if not self._draft:
            return
        default = str(Path.home() / f"nexus-report-{self._draft['draft']}.md")
        path, _ = QFileDialog.getSaveFileName(self, "Save the report", default,
                                              "Markdown (*.md);;Text (*.txt)")
        if not path:
            return

        def save(title, body):
            try:
                Path(path).write_text(f"# {title}\n\n{body}\n", encoding='utf-8')
            except OSError as e:
                self.toast.emit(f"Couldn't save the report: {e.strerror or e}", 'error')
                return
            self.status.setText(f"Saved to {short_path(path)}.")
            self.toast.emit("Report saved.", 'success')
        self._render(save)

    def _github(self):
        if not self._draft:
            return
        self._timer.stop()
        gen = self._gen

        def done(data, err):
            if gen != self._gen:
                return
            url = (data or {}).get('url') or ''
            if not url.startswith(GITHUB):
                self.toast.emit(f"Couldn't make the link: {err or 'no answer'}", 'error')
                return
            clip = data.get('clipboard')
            if clip:
                QApplication.clipboard().setText(clip)
                msg = ("The report is too long for a link, so it is on your clipboard: "
                       "paste it into the report field on GitHub.")
                self.status.setText(msg)
                self.toast.emit(msg, 'info')
            if not platform.open_url(url):
                self.toast.emit("Couldn't open the browser.", 'error')
                return
            if not clip:
                self.status.setText("GitHub is open in your browser. Once you've submitted "
                                    "the issue you can forget this crash here.")
        cli_json(['--report-url', self._draft['draft'], *self.selection_args(), '--json'],
                 done, 30000)

    def _forget(self):
        r = self.current_crash()
        if r is None:
            return
        cid = r['id']

        def done(data, err):
            if not (data or {}).get('dismissed'):
                self.toast.emit(f"Couldn't forget it: {err or 'it was already gone'}", 'error')
            self.dismissed.emit(cid)
            self.want(None)
            self.reload()
        cli_json(['--report-dismiss', cid, '--json'], done, 30000)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .details import Disclosure  # noqa: E402
from .jobs import cli_json  # noqa: E402
from .widgets import (clear_layout, icon_label, label, NoWheelCombo, plain_label,  # noqa: E402
    short_path, soft_wrap)
