"""The game tile: art, hover title, progress ring."""

import html
import os

from PyQt6.QtCore import (pyqtProperty, pyqtSignal, QEasingCurve, QPoint, QPointF,
    QPropertyAnimation, QRectF, Qt, QTimer)
from PyQt6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen,
    QPixmap)
from PyQt6.QtWidgets import QFrame, QToolButton

from .fmt import fmt_hours, fmt_relative, fmt_size
from .theme import FONTS, FS, G, GRAD, P, pill, R, SP, TILE_LIFT, TILE_W


class GameTile(QFrame):
    """A game's art, nothing else at rest — no badges or tags (all of that
    lives on the game page). On hover/focus the title fades in with one big
    Play button and a ⋮ menu button. A running job replaces Play with a
    progress ring; a playing game gets a small green dot.

    kind='cover' is the portrait 2/3 tile; kind='wide' is a 16:9 card using
    the game's wide/hero art, which keeps its title visible (hero art rarely
    carries the game's name).

    The widget reserves TILE_LIFT px at the top and paints its body offset into
    it, so the hover lift never re-runs the layout."""
    selected = pyqtSignal(dict, object)     # (game, keyboard modifiers)
    play = pyqtSignal(dict)
    menu = pyqtSignal(dict, QPoint)
    activated = pyqtSignal(dict)            # Enter / controller A (Settings → Controls)
    double = pyqtSignal(dict)               # a double click (Settings → Controls)
    _loading: dict[tuple, QPixmap] = {}     # shared "art on its way" fills, per size

    def __init__(self, game: dict, pending: bool = False, width: int = TILE_W,
                 kind: str = 'cover', theatre: bool = False):
        super().__init__()
        self.game, self.pending, self.kind = game, pending, kind
        self.theatre = theatre                  # bigger focus glow, read from the couch
        self.tw = width
        self.th = int(width * 9 / 16) if kind == 'wide' else int(width * 3 / 2)
        self.setObjectName("Tile")
        self.setFixedSize(self.tw, self.th + TILE_LIFT)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = 0.0
        self._selected = False
        self.job: Job | None = None
        self.mark_new = False                   # "New" pill: installed, never played
        self._spin = 0

        self._art = None
        art_path, url, self._art_kind = art_lookup(game, WIDE_KINDS if kind == 'wide'
                                                   else COVER_KINDS)
        self._art_sig = self._sig_of(game, art_path, url)
        if not art_path and url:
            ART.fetch(url, self._art_arrived)
        self._set_art(art_path)

        self._anim = QPropertyAnimation(self, b'hoverT', self)
        self._anim.setDuration(150)              # motion.fast
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._spinner = QTimer(self)
        self._spinner.setInterval(40)
        self._spinner.timeout.connect(self._advance_spin)

        self._pb = 56 if min(self.tw, self.th) >= 170 else 44
        self.btn_play = QToolButton(self)
        self.btn_play.setObjectName("PlayRound")
        self.btn_play.setText(G['play'])
        self.btn_play.setToolTip("Play")
        self.btn_play.setFixedSize(self._pb, self._pb)
        self.btn_play.setStyleSheet(f"border-radius: {pill(self._pb)}px;")
        self.btn_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_play.clicked.connect(lambda: self.play.emit(self.game))
        self.btn_more = QToolButton(self)
        self.btn_more.setObjectName("TileMore")
        self.btn_more.setText(G['more'])
        self.btn_more.setToolTip("More actions")
        self.btn_more.setFixedSize(28, 28)
        self.btn_more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_more.clicked.connect(lambda: self.menu.emit(
            self.game, self.btn_more.mapToGlobal(QPoint(0, self.btn_more.height()))))
        # Mouse affordances only: keyboard/controller use Enter (details) and
        # the Menu key / X (menu), so these never steal focus from the tile.
        self.btn_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_play.hide()
        self.btn_more.hide()
        self._place_buttons()
        self.setToolTip(self._tooltip())

    def _set_art(self, art_path):
        # Not installed: the art in grey, so the library reads at a glance.
        grey = not is_installed(self.game)
        pm = ART.peek(art_path, self.tw, self.th, grey) if art_path else None
        if pm is None and art_path:
            # Decoded in the background: a plain card until it's there.
            self._art_for = art_path
            key = (self.tw, self.th, P['surface_2'])
            if key not in GameTile._loading:
                fill = GameTile._loading[key] = QPixmap(self.tw, self.th)
                fill.fill(QColor(P['surface_2']))
            self._art = GameTile._loading[key]
            ART.request(art_path, self.tw, self.th,
                        lambda pm, p=art_path: self._art_decoded(p, pm), grey)
            return
        self._art_for = None
        self._apply_art(pm)

    def _art_decoded(self, path, pm):
        if getattr(self, '_art_for', None) == path:
            self._apply_art(pm)
            self.update()

    def _apply_art(self, pm):
        if pm is None and self.pending:
            pm = QPixmap(self.tw, self.th)       # no initials under the progress ring
            pm.fill(QColor(P['surface_2']))
        self._art = pm if pm is not None else placeholder_pixmap(
            self.game.get('display_name', '?'), self.tw, self.th)

    ART_KEYS = ('cover', 'cover_url', 'wide', 'wide_url', 'hero', 'hero_url', 'artwork_dir',
                'installed', 'display_name')

    def set_game(self, game: dict):
        """Re-point a kept tile at a fresh copy of its game (a library
        reload); the art is only reloaded when it changed."""
        self.game = game
        art_path, url, kind = art_lookup(game, WIDE_KINDS if self.kind == 'wide'
                                         else COVER_KINDS)
        # A new pick is written over the old file (same path), and an install
        # turns grey art to colour: the file's mtime and the install state
        # count as much as the path.
        sig = self._sig_of(game, art_path, url)
        if sig != self._art_sig:
            self._art_sig, self._art_kind = sig, kind
            if not art_path and url:
                ART.fetch(url, self._art_arrived)
            self._set_art(art_path)
        self.setToolTip(self._tooltip())
        self._sync_buttons()
        self.update()

    @staticmethod
    def _sig_of(game, art_path, url):
        try:
            mtime = os.stat(art_path).st_mtime_ns if art_path else 0
        except OSError:
            mtime = 0
        return art_path, mtime, url, is_installed(game)

    def _art_arrived(self, path):
        try:
            self._art_kind = art_lookup(self.game, WIDE_KINDS if self.kind == 'wide'
                                        else COVER_KINDS)[2]
            self._set_art(path)
            self.update()
        except RuntimeError:                     # tile already rebuilt
            pass

    # ---- state ----
    def set_selected(self, on: bool):
        self._selected = on
        self.update()

    @staticmethod
    def _job_sig(job) -> tuple | None:
        """What the tile shows of a job (its ring): unchanged -> no repaint.
        Output lines change a job ~8 times a second without changing this."""
        if job is None:
            return None
        return (id(job), job.state, job.active, job.step,     # step: a pending tile's caption
                job.progress if job.state == 'running' else None)

    def _spins(self) -> bool:
        """The ring is the indeterminate spinner (no percentage to show)."""
        j = self.job
        if not (j and j.active):
            return False
        prog = j.progress if j.state == 'running' else None
        return not (prog and prog[1])

    def set_job(self, job: 'Job | None'):
        sig = self._job_sig(job)
        if job is self.job and sig == getattr(self, '_shown_job', None):
            return
        self.job, self._shown_job = job, sig
        self._sync_spinner()
        self._sync_buttons()
        self.update()

    def _sync_spinner(self):
        if self._spins() and self.isVisible():
            if not self._spinner.isActive():
                self._spinner.start()
        else:
            self._spinner.stop()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._sync_spinner()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._spinner.stop()

    def _advance_spin(self):
        self._spin = (self._spin + 12) % 360
        if not self._spins():
            self._spinner.stop()
            self._sync_buttons()
        self.update()

    # ---- hover animation driver ----
    def _get_hover(self) -> float:
        return self._hover

    def _set_hover(self, v: float):
        self._hover = v
        self._place_buttons()
        self.update()

    hoverT = pyqtProperty(float, _get_hover, _set_hover)

    def _place_buttons(self):
        top = TILE_LIFT - TILE_LIFT * self._hover
        cy = self.th * (0.42 if self.kind == 'cover' else 0.40)
        self.btn_play.move((self.tw - self._pb) // 2, int(top + cy - self._pb / 2))
        self.btn_more.move(self.tw - 28 - SP['2'], int(top + SP['2']))

    def _sync_buttons(self):
        # Mouse only: with a controller or keys, A/Enter opens the game page
        # (Play is right there) and X/Menu opens the menu, so the overlay
        # buttons would just cover the art.
        # A click (or A) opens the game's page, where Play is; the card itself
        # only offers ⋮ on mouse hover.
        self.btn_play.setVisible(False)
        self.btn_more.setVisible(self.underMouse() and not self.pending and not self.theatre
                                 and input_mode() == 'mouse')

    def _animate(self, to: float):
        self._anim.stop()
        if not self.isVisible():
            # Hidden (its window closing, a page switched away): no glide —
            # an animation started now would step a tile that may be gone.
            self._hover = to
            return
        self._anim.setStartValue(self._hover)
        self._anim.setEndValue(to)
        self._anim.start()

    def _hovered(self, on: bool):
        self._animate(1.0 if on else 0.0)
        self._sync_buttons()

    def enterEvent(self, ev):
        self._hovered(True)
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        if self.hasFocus():
            self._sync_buttons()           # keep the lift, drop the buttons
        else:
            self._hovered(False)
        super().leaveEvent(ev)

    def focusInEvent(self, ev):
        self._hovered(True)
        super().focusInEvent(ev)

    def focusOutEvent(self, ev):
        if not self.underMouse():
            self._hovered(False)
        super().focusOutEvent(ev)

    # ---- input ----
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.game, ev.modifiers())
        elif ev.button() == Qt.MouseButton.MiddleButton and not self.pending:
            self.menu.emit(self.game, ev.globalPosition().toPoint())   # the ⋮ menu
            return
        super().mousePressEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        if not self.pending and ev.button() == Qt.MouseButton.LeftButton:
            self.double.emit(self.game)          # play, or open (Settings → Controls)
        super().mouseDoubleClickEvent(ev)

    def contextMenuEvent(self, ev):
        if not self.pending:
            if not self.theatre:          # in Theatre, selecting opens the game page
                self.selected.emit(self.game, Qt.KeyboardModifier.NoModifier)
            self.menu.emit(self.game, ev.globalPos())

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self.pending:
                self.selected.emit(self.game, Qt.KeyboardModifier.NoModifier)
            else:
                self.activated.emit(self.game)
        elif ev.key() == Qt.Key.Key_Menu:
            self.menu.emit(self.game, self.mapToGlobal(QPoint(self.tw // 2, self.th // 2)))
        else:
            super().keyPressEvent(ev)

    def _tooltip(self) -> str:
        g = self.game
        lines = [f"<b>{html.escape(g.get('display_name', ''))}</b>"]
        stats = [s for s in (fmt_relative(g.get('last_played')),
                             fmt_hours(g.get('playtime_minutes')),
                             fmt_size(g.get('disk_bytes'))) if s]
        if stats:
            lines.append(" · ".join(stats))
        if g.get('proton'):
            lines.append(f"Proton: {html.escape(g['proton'])}")
        lines.append("<i>Click to open · right-click for actions</i>")
        return "<br>".join(lines)

    # ---- painting ----
    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        top = TILE_LIFT - TILE_LIFT * self._hover
        body = QRectF(0, top, self.tw, self.th)
        rad = R['tile']
        clip = QPainterPath()
        clip.addRoundedRect(body, rad, rad)
        pt.save()
        pt.setClipPath(clip)

        scale = 1.0 + 0.04 * self._hover
        aw, ah = self.tw * scale, self.th * scale
        pt.drawPixmap(QRectF(body.center().x() - aw / 2, body.center().y() - ah / 2, aw, ah),
                      self._art, QRectF(self._art.rect()))
        owned = not is_installed(self.game)
        if self.pending or self._hover > 0.01 or (self.job and self.job.active) or owned:
            # A light scrim so the Play button / progress ring reads on any art.
            dim = QColor(0, 0, 0)
            dim.setAlphaF(0.55 if (self.pending or self.job) else
                          max(0.35 - 0.2 * self._hover, 0.28 * self._hover) if owned else
                          0.28 * self._hover)
            pt.fillRect(body, dim)

        # The caption (and its gradient) fades in with hover; pending tiles and
        # wide cards always show it.
        cap = 1.0 if (self.pending or self.kind == 'wide') else self._hover
        if cap > 0.01:
            cap_h = self.th * (0.42 if self.kind == 'cover' else 0.55)
            g = QLinearGradient(0, body.bottom(), 0, body.bottom() - cap_h)
            c0 = QColor(0, 0, 0); c0.setAlphaF(0.88 * cap)
            c1 = QColor(0, 0, 0); c1.setAlphaF(0.45 * cap)
            c2 = QColor(0, 0, 0); c2.setAlphaF(0.0)
            g.setColorAt(0.0, c0); g.setColorAt(0.45, c1); g.setColorAt(1.0, c2)
            pt.fillRect(QRectF(0, body.bottom() - cap_h, self.tw, cap_h), QBrush(g))
            pt.setOpacity(cap)
            self._paint_caption(pt, body)
            pt.setOpacity(1.0)
        pt.restore()

        if self.game.get('playing') or self.game.get('launching'):
            self._paint_playing(pt, body)
        if self.job and self.job.active:
            self._paint_ring(pt, body)
        elif owned and self.game.get('installing'):
            f = install_fraction(self.game)
            self._paint_progress(pt, body, f, f"{int(f * 100)}%" if f is not None else "")
        elif owned:
            self._paint_download_badge(pt, body)
        elif self.mark_new and not self.game.get('last_played'):
            self._paint_new_badge(pt, body)

        pt.setBrush(Qt.BrushStyle.NoBrush)
        if self._selected:
            g = QLinearGradient(body.topLeft(), body.bottomRight())
            a, b = (QColor(c) for c in GRAD['dusk'])
            g.setColorAt(0.0, a); g.setColorAt(1.0, b)
            pt.setPen(QPen(QBrush(g), 2.5))
            pt.drawRoundedRect(body.adjusted(1.25, 1.25, -1.25, -1.25), rad - 1, rad - 1)
        elif self._hover > 0.01 and not self.hasFocus():   # focus: the FocusRing
            ring = QColor(P['accent']); ring.setAlphaF(min(0.6, self._hover))
            pt.setPen(QPen(ring, 1.5))
            pt.drawRoundedRect(body.adjusted(1, 1, -1, -1), rad - 1, rad - 1)
        else:
            hair = QColor(P['on_art']); hair.setAlphaF(0.06)
            pt.setPen(QPen(hair, 1))
            pt.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
        pt.end()

    def _paint_playing(self, pt: QPainter, body: QRectF):
        """A small green dot with a soft halo, top-right — the only status a
        tile shows at rest."""
        c = QPointF(body.right() - SP['3'], body.top() + SP['3'])
        role = 'success' if self.game.get('playing') else 'accent'   # starting: accent
        halo = QColor(P[role]); halo.setAlphaF(0.30)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(halo)
        pt.drawEllipse(c, 7.0, 7.0)
        pt.setBrush(QColor(P[role]))
        pt.drawEllipse(c, 4.0, 4.0)

    def _paint_ring(self, pt: QPainter, body: QRectF):
        prog = self.job.progress if self.job.state == 'running' else None
        if prog and prog[1]:
            frac = min(prog[0], prog[1]) / prog[1]
            self._paint_progress(pt, body, frac, f"{int(frac * 100)}%")
        else:
            self._paint_progress(pt, body, None, "…" if self.job.state == 'queued' else "")

    def _paint_download_badge(self, pt: QPainter, body: QRectF):
        """Owned, not installed: a small download mark, top-left."""
        d = 24
        r = QRectF(body.left() + SP['2'], body.top() + SP['2'], d, d)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QColor(P['scrim_badge']))
        pt.drawEllipse(r)
        f = QFont(FONTS.icon)
        f.setPixelSize(15)
        pt.setFont(f)
        pt.setPen(QColor(P['on_art']))
        pt.drawText(r, Qt.AlignmentFlag.AlignCenter, G['download'])

    def _paint_new_badge(self, pt: QPainter, body: QRectF):
        """Installed but never played: a small "New" pill, top-left."""
        f = QFont(FONTS.ui, -1, QFont.Weight.DemiBold)
        f.setPixelSize(FS['xs'])
        pt.setFont(f)
        w = pt.fontMetrics().horizontalAdvance("New") + 2 * SP['2']
        r = QRectF(body.left() + SP['2'], body.top() + SP['2'], w, 20)
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QColor(P['accent']))
        pt.drawRoundedRect(r, 10, 10)
        pt.setPen(QColor(P['on_accent']))
        pt.drawText(r, Qt.AlignmentFlag.AlignCenter, "New")

    def _paint_progress(self, pt: QPainter, body: QRectF, frac: float | None, text: str):
        d = 60
        cy = self.th * (0.42 if self.kind == 'cover' else 0.40)
        rect = QRectF(body.center().x() - d / 2, body.top() + cy - d / 2, d, d)
        track = QColor(P['on_art']); track.setAlphaF(0.18)
        pt.setPen(QPen(track, 5))
        pt.drawEllipse(rect)
        pen = QPen(QColor(P['accent']), 5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pt.setPen(pen)
        if frac is not None:
            pt.drawArc(rect, 90 * 16, -int(frac * 360 * 16))
        else:
            pt.drawArc(rect, -self._spin * 16, 100 * 16)
        if text:
            f = QFont(FONTS.ui, -1, QFont.Weight.Medium)
            f.setPixelSize(FS['xs'])
            pt.setFont(f)
            pt.setPen(QColor(P['on_art']))
            pt.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_caption(self, pt: QPainter, body: QRectF):
        """Title (2-line clamp) plus a muted stats line, over the caption gradient."""
        pad = SP['2_5']
        avail = self.tw - pad * 2
        sub_f = QFont(FONTS.ui, -1)
        sub_f.setPixelSize(FS['2xs'] + 1)
        if self.pending:
            sub = self.job.step if self.job else "Queued"
        elif not is_installed(self.game):
            sub = play_label(self.game) if self.game.get('installing') else " · ".join(
                s for s in ("Not installed", fmt_hours(self.game.get('playtime_minutes'))) if s)
        else:
            sub = " · ".join(s for s in (fmt_relative(self.game.get('last_played')),
                                         fmt_hours(self.game.get('playtime_minutes'))) if s) \
                or "Not played yet"
        pt.setFont(sub_f)
        sfm = pt.fontMetrics()
        sub = sfm.elidedText(sub, Qt.TextElideMode.ElideRight, avail)
        sub_y = body.bottom() - SP['2'] - sfm.descent()
        muted = QColor(P['on_art']); muted.setAlphaF(0.72)
        pt.setPen(muted)
        pt.drawText(QPoint(int(pad), int(sub_y)), sub)

        f = QFont(FONTS.ui, -1, QFont.Weight.Medium)
        f.setPixelSize(FS['xs'])
        pt.setFont(f)
        fm = pt.fontMetrics()
        words = self.game.get('display_name', '').split()
        lines, cur = [], ''
        for w in words:
            trial = f"{cur} {w}".strip()
            if fm.horizontalAdvance(trial) <= avail or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
                if len(lines) == 2:
                    break
        if cur and len(lines) < 2:
            lines.append(cur)
        consumed = len(' '.join(lines).split())
        if lines and consumed < len(words):
            lines[-1] = fm.elidedText(' '.join(words[len(' '.join(lines[:-1]).split()):]),
                                      Qt.TextElideMode.ElideRight, avail)
        if self.kind == 'wide' and getattr(self, '_art_kind', None) == 'wide' \
                and not self.pending:
            return                       # the banner art already names the game
        lh = int(fm.height() * 1.25)
        y = sub_y - sfm.height() - SP['0_5'] - lh * (len(lines) - 1)
        pt.setPen(QColor(P['on_art']))
        for i, line in enumerate(lines):
            pt.drawText(QPoint(int(pad), int(y + lh * i)), line)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .art import ART, art_lookup, COVER_KINDS, placeholder_pixmap, WIDE_KINDS  # noqa: E402
from .gameinfo import install_fraction, is_installed, play_label  # noqa: E402
from .input import input_mode  # noqa: E402
from .jobs import Job  # noqa: E402
