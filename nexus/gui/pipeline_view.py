"""The Downloads page's pipeline (w-94): a disk forecast on top, then one
card per game Nexus downloads, from the torrent to Play (Download, Verify,
Install, Finish), and a short row per game in Done recently.

The widgets only draw what nexus/pipeline.py computed (`--torrents --all
--json` → `pipeline` and `drives`). They are kept by key and refilled with
`set()`, a no-op when nothing changed, so focus survives the 2 s poll.
Colours come from the app QSS (object names and properties) or from P at
paint time, so a theme change needs no rebuild; `restyle()` picks up a
new Size."""

import time

from PyQt6.QtCore import pyqtSignal, QLineF, QPoint, QPointF, QRectF, QSize, Qt, QVariantAnimation
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QBoxLayout, QFrame, QGraphicsOpacityEffect, QGridLayout,
    QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from nexus import pipeline

from . import art
from .fmt import fmt_bytes, fmt_eta
from .theme import _mix, FONTS, FS, G, GRAD, P, PILL_H, R, scaled, SP

LABELS = {'download': "Download", 'verify': "Verify", 'install': "Install", 'finish': "Finish"}
SKIPPED_VERIFY = "Checked by the torrent client"
ART_PX = 88                 # the card's cover (scaled)
SIDE_W = 210                # the side column's width (wider only for a long ETA)
NARROW = 760                # below this list width the side column goes under the card (mockup)
MOVING = ('downloading', 'checking', 'verifying', 'installing', 'updating', 'finishing')


def repolish(w: QWidget):
    """Re-apply the app QSS after an object name or property changed."""
    w.style().unpolish(w)
    w.style().polish(w)


def title_of(r: dict) -> str:
    return r.get('title') or r.get('name') or str(r.get('hash') or '')[:12]


def span(seconds) -> str:
    """"under a minute", "25 min", "1 h 05 min" (fmt_eta without its
    "left", and never in seconds)."""
    s = int(seconds or 0)
    return "under a minute" if s < 60 else fmt_eta(max(s, 90)).removesuffix(" left")


def state_line(c: dict) -> tuple[str, str, str]:
    """The card's state line: (what, figure, tag). `tag` is 'estimate' for
    an install measured against the page's size, 'exact' for a check."""
    st, step = c['state'], c.get('step') or ''
    dl, inst = c.get('download') or {}, c.get('install') or {}
    frac = {t.get('stage'): t.get('frac') for t in c.get('track') or ()}
    if st == 'downloading':
        fig = f"{fmt_bytes(dl.get('done'))} of {fmt_bytes(dl.get('total'))}" \
            if dl.get('total') else ''
        if dl.get('rate'):
            fig += f"{' · ' if fig else ''}↓ {fmt_bytes(dl['rate'])}/s"
        return "Downloading", fig, ''
    if st == 'checking':                    # the client re-checks a download not finished
        check = c.get('check')
        return "Checking files", f"{check * 100:.0f}%" if check is not None else '', ''
    if st == 'paused':
        return step, f"at {(frac.get('download') or 0) * 100:.0f}%", ''
    if st == 'verifying':
        if frac.get('verify') is None:      # the client gave no figure: indeterminate
            return step or "Checking files", '', ''
        return step or "Checking files", f"{frac['verify'] * 100:.0f}%", 'exact'
    if st == 'installing':
        fig = ''
        if inst.get('written') is not None and inst.get('expected'):
            fig = f"{fmt_bytes(inst['written'])} of about {fmt_bytes(inst['expected'])}"
        if inst.get('rate'):
            fig += f"{' · ' if fig else ''}{fmt_bytes(inst['rate'])}/s"
        return step or "Installing", fig, 'estimate' if inst.get('expected') else ''
    if st == 'updating':
        fig = f"{fmt_bytes(inst['written'])} written" if inst.get('written') is not None else ''
        return "Updating", fig, ''
    if st == 'downloaded':
        return "Downloaded", fmt_bytes(dl['total']) if dl.get('total') else '', ''
    if st == 'ready':
        size = c.get('original_bytes')
        return "Ready to play", f"Installed {fmt_bytes(size)}" if size else '', ''
    return step or st.capitalize(), '', ''


def _about(seconds) -> str:
    text = span(seconds)
    return text if text.startswith("under") else f"about {text}"


def eta_line(c: dict) -> str:
    """The pipeline's ETA: one figure across the stages left once an install
    was recorded (`eta_all`: "Done in about 25 min, est."); before that the
    client's for a download and the install's estimate ("est.") while
    installing, with no guess across stages."""
    if not c.get('eta') or (c['state'] not in MOVING and not c.get('eta_all')):
        return ''
    if c['stage'] == 'download' and not c.get('eta_all'):
        return f"Download done in {_about(c['eta'])}"
    return f"Done in {_about(c['eta'])}" + (", est." if c.get('eta_est') else '')


def buttons(c: dict, stalled: bool = False, has_log: bool = False,
            has_game: bool = True) -> list[tuple[str, str, bool]]:
    """The card's buttons, (action, text, primary), for its state. A
    failure's actions take the place of Pause."""
    st, why = c['state'], c.get('reason')
    if st in ('downloading', 'queued', 'checking'):
        return [('pause', "Pause", False)]
    if st == 'paused':
        return [('resume', "Resume", False)]
    if st == 'error':
        return [('resume', "Try again", True)]
    if st == 'failed':
        if why == 'uac':
            return [('retry', "Try again", True)]
        if why == 'cancelled':
            return [('retry', "Install again", True)]
        out = [('retry', "Retry", True), ('verify', "Verify & retry", False)]
        return out + ([('log', "Show log", False)] if has_log else [])
    if st == 'downloaded':
        return [('install', "Install", True)]
    if st == 'ready':
        return ([('play', "Play", True)] if has_game else []) + [('show', "Show", False)]
    return []


def kicker(text: str) -> QLabel:
    """A small all-caps section heading ("IN PROGRESS · 3")."""
    lab = QLabel(text.upper())
    lab.setObjectName("SectionKicker")
    f = lab.font()
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
    lab.setFont(f)
    return lab


def _alpha(c: str, a: float) -> QColor:
    q = QColor(c)
    q.setAlphaF(a)
    return q


class StageTrack(QWidget):
    """The four stages as one bar, each as wide as its usual share of the
    time (pipeline.STAGES), with its name under it. Done segments are the
    accent mixed into the base, the active one the dusk gradient (with a
    shimmer while it moves), the rest the track colour; a failed one is
    danger. A Verify that didn't run keeps its share, labelled "Verify –"
    and dimmed. When a name doesn't fit its segment (or the card is
    narrow) only the current stage is named, as in the mockup."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.track: list[dict] = []
        self.shimmer = False            # the page's timer repaints it while True
        self.compact = False            # narrow card: only the current stage's name
        self._phase = 0.0
        self._shown: dict[str, float] = {}      # the fill drawn now (animated)
        self._from: dict[str, float] = {}
        self._to: dict[str, float] = {}
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._step)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def _font(self) -> QFont:
        f = QFont(FONTS.mono)
        f.setPixelSize(scaled(FS['2xs']))
        f.setWeight(QFont.Weight.Medium)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
        return f

    def _bar_h(self) -> float:
        return float(scaled(SP['2_5']))

    def _gap(self) -> float:
        return float(scaled(SP['1']))

    def sizeHint(self) -> QSize:
        h = int(self._bar_h()) + scaled(SP['1_5']) + QFontMetrics(self._font()).height()
        return QSize(scaled(320), h)

    def minimumSizeHint(self) -> QSize:
        return QSize(scaled(160), self.sizeHint().height())

    def restyle(self):
        self.updateGeometry()
        self.update()

    def set_track(self, track: list[dict]):
        new = {t.get('stage'): float(t.get('frac') or 0) for t in track}
        old = dict(self._shown)
        self.track = track
        moving = art.MOTION and art.anim_ms(450) > 0 and old and any(
            new.get(k, 0) > old.get(k, 0) for k in new)
        if moving:
            self._from, self._to = old, new
            self._anim.stop()
            self._anim.setDuration(art.anim_ms(450))
            self._anim.start()
        else:
            self._anim.stop()
            self._shown = new
        self.setToolTip(f"Verify: {SKIPPED_VERIFY}" if self.skipped() else '')
        self.update()

    def _step(self, t):
        t = float(t)
        self._shown = {k: self._from.get(k, 0) + (v - self._from.get(k, 0)) * t
                       for k, v in self._to.items()}
        self.update()

    def tick(self):
        """One shimmer frame (the page's timer)."""
        self._phase = (time.monotonic() % 1.6) / 1.6
        self.update()

    def skipped(self) -> bool:
        return any(t.get('stage') == 'verify' and t.get('status') == 'skipped'
                   for t in self.track)

    def segments(self) -> list[dict]:
        """Where each stage is drawn: stage, x, w, status ('skipped' for a
        Verify that didn't run), frac and its name."""
        by_stage = {t.get('stage'): t for t in self.track}
        gap = self._gap()
        room = max(0.0, self.width() - gap * (len(pipeline.STAGES) - 1))
        x, out = 0.0, []
        for name, weight in pipeline.STAGES:
            t = by_stage.get(name) or {}
            status = t.get('status') or 'todo'
            text = LABELS[name] + (" –" if status == 'skipped' else '')
            if status == 'done':
                text = "✓ " + text
            w = room * weight
            out.append({'stage': name, 'x': x, 'w': w, 'status': status,
                        'frac': self._shown.get(name, 0.0), 'label': text.upper(),
                        'indeterminate': status == 'active' and t.get('frac') is None})
            x += w + gap
        return out

    def labels(self) -> list[dict]:
        """The names drawn under the bar, never cut: text, x, w, status.
        The current stage's (active or failed) always, moved left as far
        as it needs; every other one only when it fits its segment, isn't
        in the current one's way and the card isn't narrow (the mockup
        hides the others there)."""
        fm = QFontMetrics(self._font())
        out, cur = [], None
        for s in self.segments():
            tw = float(fm.horizontalAdvance(s['label']))
            lab = {'text': s['label'], 'x': s['x'], 'w': tw, 'status': s['status']}
            if s['status'] in ('active', 'failed'):
                lab['x'] = max(0.0, min(s['x'], self.width() - tw))
                cur = lab
                out.append(lab)
            elif not self.compact and tw <= s['w']:
                out.append(lab)
        if cur is not None:
            gap = self._gap()
            out = [lab for lab in out if lab is cur or lab['x'] + lab['w'] + gap <= cur['x']
                   or lab['x'] >= cur['x'] + cur['w'] + gap]
        return out

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self._bar_h()
        rad = h / 2
        done = QColor(_mix(P['bg'], P['accent'], 0.7))
        for s in self.segments():
            r = QRectF(s['x'], 0.0, s['w'], h)
            if r.width() < 1:
                continue
            clip = QPainterPath()
            clip.addRoundedRect(r, rad, rad)
            pt.save()
            pt.setClipPath(clip)
            pt.fillRect(r, QColor(P['surface_2']))
            st = s['status']
            if st == 'done':
                pt.fillRect(r, done)
            elif st == 'skipped':
                pt.fillRect(r, _alpha(P['muted'], 0.18))
            elif st in ('active', 'failed'):
                fw = r.width() * max(0.0, min(1.0, s['frac']))
                indeterminate = st == 'active' and fw <= 0 and (
                    s['stage'] == 'install' or s['indeterminate'])
                if indeterminate:
                    pt.fillRect(r, _alpha(P['accent'], 0.22))
                if st == 'failed':
                    pt.fillRect(r, _alpha(P['danger'], 0.28))
                    pt.fillRect(QRectF(r.x(), 0.0, fw, h), QColor(P['danger']))
                elif fw > 0:
                    g = QLinearGradient(QPointF(r.x(), 0.0), QPointF(r.x() + max(fw, 1.0), 0.0))
                    a, b = GRAD['dusk']
                    g.setColorAt(0.0, QColor(a))
                    g.setColorAt(1.0, QColor(b))
                    pt.fillRect(QRectF(r.x(), 0.0, fw, h), g)
                if st == 'active' and self.shimmer and (fw > 0 or indeterminate):
                    width = fw if fw > 0 else r.width()
                    cx = r.x() - width * 0.5 + width * 2.0 * self._phase
                    g = QLinearGradient(QPointF(cx - width * 0.25, 0.0),
                                        QPointF(cx + width * 0.25, 0.0))
                    g.setColorAt(0.0, _alpha(P['on_art'], 0.0))
                    g.setColorAt(0.5, _alpha(P['on_art'], 0.38))
                    g.setColorAt(1.0, _alpha(P['on_art'], 0.0))
                    pt.fillRect(QRectF(r.x(), 0.0, width, h), g)
            pt.restore()
        font = self._font()
        pt.setFont(font)
        fm = QFontMetrics(font)
        ly = h + scaled(SP['1_5'])
        for lab in self.labels():
            st = lab['status']
            color = {'active': QColor(P['text']), 'failed': QColor(P['danger']),
                     'skipped': _alpha(P['muted'], 0.6)}.get(st, QColor(P['muted']))
            pt.setPen(color)
            pt.drawText(QRectF(lab['x'], ly, lab['w'] + 1.0, float(fm.height())),
                        int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                        lab['text'])
        pt.end()


class Sparkline(QWidget):
    """The last two minutes of speed under the ETA: the download rate in the
    accent, the disk's write rate in the info colour, a faint fill below the
    line and a dot at the end. Drawn by time (a late poll leaves a gap), from
    0 to its own peak. All QPointF/QRectF floats: a TypeError in a paint
    handler is a qFatal."""
    FILL = 0.16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.kind: str | None = None
        self.pts: list[tuple[float, float]] = []
        self.now = 0.0
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._sizes()

    def _sizes(self):
        self.setFixedSize(scaled(132), scaled(28))

    def restyle(self):
        self._sizes()
        self.update()

    def set_series(self, kind: str | None, pts: list, now: float):
        self.kind, self.pts, self.now = kind, list(pts), float(now)
        self.setToolTip({'download': "Download speed, last 2 min",
                         'disk': "Disk write speed, last 2 min"}.get(kind or '', ''))
        self.update()

    def points(self) -> list[QPointF]:
        """The line in widget coordinates: x by time over the last SPARK_S
        seconds, y from the bottom (0) to the top (the window's peak)."""
        pad = float(scaled(3))
        w, h = float(self.width()) - 2 * pad, float(self.height()) - 2 * pad
        shown = [(float(t), float(v)) for t, v in self.pts if self.now - t <= pipeline.SPARK_S]
        peak = max((v for _t, v in shown), default=0.0)
        out = []
        for t, v in shown:
            x = pad + w * (1.0 - max(0.0, self.now - t) / pipeline.SPARK_S)
            y = pad + h * (1.0 - (v / peak if peak > 0 else 0.0))
            out.append(QPointF(x, y))
        return out

    def paintEvent(self, ev):
        pts = self.points()
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = P['info'] if self.kind == 'disk' else P['accent']
        if len(pts) >= 2:
            base = float(self.height()) - float(scaled(3))
            fill = QPainterPath(QPointF(pts[0].x(), base))
            for p in pts:
                fill.lineTo(p)
            fill.lineTo(QPointF(pts[-1].x(), base))
            fill.closeSubpath()
            pt.fillPath(fill, _alpha(color, self.FILL))
            line = QPainterPath(pts[0])
            for p in pts[1:]:
                line.lineTo(p)
            pen = QPen(QColor(color), float(scaled(1.5)))
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pt.setPen(pen)
            pt.setBrush(Qt.BrushStyle.NoBrush)
            pt.drawPath(line)
            pt.setPen(Qt.PenStyle.NoPen)
            pt.setBrush(QColor(color))
            r = float(scaled(2.5))
            pt.drawEllipse(pts[-1], r, r)
        pt.end()


class DiskBar(QWidget):
    """One drive: in use now, still to be written by the games in flight
    (hatched, never past the free space), and free after."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.d: dict = {}
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.restyle()

    def restyle(self):
        self.setFixedHeight(scaled(SP['3'] + SP['0_5']))

    def set_drive(self, d: dict):
        self.d = d
        self.update()

    def parts(self) -> tuple[float, float]:
        """(used, to_write) as shares of the drive."""
        total = float(self.d.get('total') or 0)
        if total <= 0:
            return 0.0, 0.0
        free = float(self.d.get('free') or 0)
        used = max(0.0, total - free)
        incoming = min(float(self.d.get('to_write') or 0), free)
        return used / total, incoming / total

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        rad = r.height() / 2
        clip = QPainterPath()
        clip.addRoundedRect(r, rad, rad)
        pt.setClipPath(clip)
        pt.fillRect(r, QColor(P['surface_2']))
        used, incoming = self.parts()
        uw, iw = r.width() * used, r.width() * incoming
        pt.fillRect(QRectF(0.0, 0.0, uw, r.height()), _alpha(P['subtext'], 0.55))
        if iw > 0:
            band = QRectF(uw, 0.0, iw, r.height())
            pt.save()
            pt.setClipRect(band, Qt.ClipOperation.IntersectClip)
            pt.fillRect(band, _alpha(P['accent'], 0.45))
            pt.setPen(QPen(QColor(P['accent']), float(scaled(SP['1']))))
            step = float(scaled(SP['3']))
            x = uw - r.height()
            while x < uw + iw + r.height():
                pt.drawLine(QLineF(x, r.height(), x + r.height(), 0.0))
                x += step
            pt.restore()
        pt.end()


class Swatch(QWidget):
    """The forecast key's little square."""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.restyle()

    def restyle(self):
        self.setFixedSize(scaled(SP['2_5']), scaled(SP['2_5']))

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        fill = {'used': _alpha(P['subtext'], 0.55), 'incoming': QColor(P['accent']),
                'free': QColor(P['surface_2'])}[self.kind]
        pt.setPen(QPen(QColor(P['border']), 1.0) if self.kind == 'free' else Qt.PenStyle.NoPen)
        pt.setBrush(fill)
        rad = min(float(R['sm']), r.height() / 3)
        pt.drawRoundedRect(r, rad, rad)
        pt.end()


class ForecastStrip(QFrame):
    """The disk forecast: a row per drive the downloads and installs in
    flight write to, "X free now · about Y free after" (or "short by Z" in
    danger), and the key. Hidden when nothing is left to write."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.drives: list[dict] | None = None
        self.rows: dict[str, tuple[QWidget, QLabel, QLabel, DiskBar]] = {}
        self.swatches: list[Swatch] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SP['5'], SP['4'], SP['5'], SP['4'])
        lay.setSpacing(SP['3'])
        self.list = QVBoxLayout()
        self.list.setSpacing(SP['3'])
        lay.addLayout(self.list)
        key = QHBoxLayout()
        key.setSpacing(SP['1_5'])
        for kind, text in (('used', "In use now"), ('incoming', "Still to be written"),
                           ('free', "Free after")):
            sw = Swatch(kind, self)
            self.swatches.append(sw)
            key.addWidget(sw, 0, Qt.AlignmentFlag.AlignVCenter)
            lab = QLabel(text, self)
            lab.setObjectName("PipeKey")
            key.addWidget(lab)
            key.addSpacing(SP['3'])
        key.addStretch()
        lay.addLayout(key)
        self.hide()                     # nothing to forecast until the first poll

    def restyle(self):
        for sw in self.swatches:
            sw.restyle()
        for *_x, bar in self.rows.values():
            bar.restyle()

    def _row(self, did: str):
        w = QWidget(self)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(SP['2'])
        top = QHBoxLayout()
        top.setSpacing(SP['4'])
        head = kicker("")
        head.setParent(w)
        head.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(head, 1)
        text = QLabel(w)
        text.setObjectName("PipeFigure")
        text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(text)
        v.addLayout(top)
        bar = DiskBar(w)
        v.addWidget(bar)
        self.list.addWidget(w)
        w.show()
        self.rows[did] = (w, head, text, bar)
        return self.rows[did]

    def set_drives(self, drives: list[dict]):
        drives = [d for d in drives or () if d.get('total')]
        if drives != self.drives:
            self.drives = drives
            self._fill(drives)
        self.setVisible(any(d.get('to_write') for d in drives))

    def _fill(self, drives: list[dict]):
        keep = {str(d['id']) for d in drives}
        for did in [k for k in self.rows if k not in keep]:
            w = self.rows.pop(did)[0]
            self.list.removeWidget(w)
            w.hide()
            w.deleteLater()
        for i, d in enumerate(drives):
            did = str(d['id'])
            w, head, text, bar = self.rows.get(did) or self._row(did)
            if self.list.indexOf(w) != i:
                self.list.removeWidget(w)
                self.list.insertWidget(i, w)
            head.setText(f"{d.get('mount') or ''} forecast".upper())
            head.setToolTip(d.get('mount') or '')
            line = f"{fmt_bytes(d.get('free'))} free now · "
            if d.get('short_by'):
                line += f"short by {fmt_bytes(d['short_by'])}"
            else:
                line += f"about {fmt_bytes(d.get('free_after'))} free after"
            text.setText(line)
            text.setProperty('short', bool(d.get('short_by')))
            repolish(text)
            bar.set_drive(d)


class CardArt(QWidget):
    """The cover, rounded: the game's own once the library knows it, else
    the initials stand-in."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._key = None
        self._pm = None
        self.restyle()

    def restyle(self):
        n = scaled(ART_PX)
        self.setFixedSize(n, n)
        if self._key is not None:
            key, self._key = self._key, None
            self.set_game(*key)

    def set_game(self, title: str, game: dict | None):
        key = (title, game)
        if self._key is not None and key[0] == self._key[0] \
                and (game or {}).get('slug') == (self._key[1] or {}).get('slug'):
            return
        self._key = key
        n = self.width()
        self._pm = art.placeholder_pixmap(' '.join(title.split()[:2]), n, n)
        if game:
            path, _url, _kind = art.art_lookup(game, art.COVER_KINDS)
            if path:
                art.ART.pixmap_async(path, n, n, R['card'],
                                     lambda pm, me=self, k=key: alive(me) and me._got(k, pm))
        self.update()

    def _got(self, key, pm):
        if key == self._key:
            self._pm = pm
            self.update()

    def paintEvent(self, ev):
        if self._pm is None:
            return
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r, float(R['card']), float(R['card']))
        pt.setClipPath(clip)
        pt.drawPixmap(r, self._pm, QRectF(self._pm.rect()))   # source: device pixels
        pt.end()


class PipelineCard(QFrame):
    """One game on its way: cover, title and source, the state line, the
    four-stage track; on the right the overall percent, the ETA and the
    buttons for its state, and ⋮ for the torrent's own choices. A queued
    one is dimmed. `row_sink(action, row)` gets the ⋮ choices: the page's,
    which outlives a card a poll may drop while the pad's sheet is open."""
    action = pyqtSignal(str, dict)          # a buttons() action, the card

    def __init__(self, c: dict, parent=None, row_sink=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)     # a last resort for focus
        self.row_sink = row_sink or (lambda a, r: None)
        self.reorder = False                # the ⋮ menu's queue moves (the page sets it)
        self.c: dict = {}
        self.row: dict = {}
        self._sig = None
        self._args: tuple = ()
        self.narrow = False
        self.btns: dict[str, QPushButton] = {}
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(SP['4'], SP['4'], SP['4'], SP['4'])
        self.grid.setHorizontalSpacing(SP['4'])
        self.grid.setVerticalSpacing(SP['3'])
        self.art = CardArt(self)
        self.grid.addWidget(self.art, 0, 0, Qt.AlignmentFlag.AlignVCenter)
        main = QWidget(self)
        main.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        mv = QVBoxLayout(main)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(SP['2'])
        top = QHBoxLayout()
        top.setSpacing(SP['3'])
        # The source sits next to the title (the mockup's baseline row); a
        # long title ends in … instead of pushing it out.
        self.title = ElideLabel('', "PipeTitle")
        self.title.setParent(main)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        top.addWidget(self.title, 0, Qt.AlignmentFlag.AlignBottom)
        self.source = QLabel(main)
        self.source.setObjectName("PipeSource")
        top.addWidget(self.source, 0, Qt.AlignmentFlag.AlignBottom)
        top.addStretch(1)
        mv.addLayout(top)
        line = QHBoxLayout()
        line.setSpacing(SP['3'])
        self.step = QLabel(main)
        self.step.setObjectName("PipeStep")
        self.step.setTextFormat(Qt.TextFormat.PlainText)
        self.step.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        line.addWidget(self.step, 1)
        self.figure = QLabel(main)
        self.figure.setObjectName("PipeFigure")
        line.addWidget(self.figure)
        self.tag = QLabel(main)
        self.tag.setObjectName("PipeTag")
        self.tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        line.addWidget(self.tag)
        self.tag.setVisible(False)
        mv.addLayout(line)
        self.warn = QLabel(main)
        self.warn.setObjectName("PipeWarn")
        self.warn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        mv.addWidget(self.warn)
        self.warn.setVisible(False)
        self.track = StageTrack(main)
        mv.addWidget(self.track)
        self.main = main
        self.grid.addWidget(main, 0, 1)
        self.grid.setColumnStretch(1, 1)
        side = QWidget(self)
        self.side = side
        self.sidebox = QBoxLayout(QBoxLayout.Direction.TopToBottom, side)
        self.sidebox.setContentsMargins(0, 0, 0, 0)
        self.sidebox.setSpacing(SP['1_5'])
        self.pctbox = QWidget(side)
        pl = QHBoxLayout(self.pctbox)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(SP['0_5'])
        self.pct = QLabel(self.pctbox)
        self.pct.setObjectName("PipePct")
        pl.addWidget(self.pct, 0, Qt.AlignmentFlag.AlignBaseline)
        unit = QLabel("%", self.pctbox)
        unit.setObjectName("PipePctUnit")
        pl.addWidget(unit, 0, Qt.AlignmentFlag.AlignBaseline)
        self.sidebox.addWidget(self.pctbox, 0, Qt.AlignmentFlag.AlignRight)
        self.eta = QLabel(side)
        self.eta.setObjectName("PipeEta")
        self.sidebox.addWidget(self.eta, 0, Qt.AlignmentFlag.AlignRight)
        self.spark = Sparkline(side)
        self.spark.setVisible(False)
        self._spark = pipeline.SPARK_EMPTY
        self.sidebox.addWidget(self.spark, 0, Qt.AlignmentFlag.AlignRight)
        bw = QWidget(side)
        self.btnrow = QHBoxLayout(bw)
        self.btnrow.setContentsMargins(0, 0, 0, 0)
        self.btnrow.setSpacing(SP['1_5'])
        self.more = icon_button(G['more'], "More", scaled(32))
        self.more.setParent(bw)
        self.more.clicked.connect(self._menu)
        self.btnrow.addWidget(self.more)
        self.sidebox.addWidget(bw, 0, Qt.AlignmentFlag.AlignRight)
        self.btnhost = bw
        self.grid.addWidget(side, 0, 2, Qt.AlignmentFlag.AlignVCenter)
        self._sizes()
        self.set(c)

    def _sizes(self):
        """What follows Settings → Size (set in code, not by the QSS)."""
        self.title.setMinimumWidth(scaled(80))
        self.tag.setFixedHeight(scaled(PILL_H))
        n = scaled(32)
        self.more.setFixedSize(n, n)
        self.spark.restyle()
        self.side.setMinimumWidth(0 if self.narrow else scaled(SIDE_W))

    def restyle(self):
        """A theme or Size change: sizes, art and paint, keeping the widgets
        (and focus)."""
        self._sizes()
        self.art.restyle()
        self.track.restyle()
        for w in self.findChildren(QLabel):
            repolish(w)

    # ---- data ----
    def set(self, c: dict, row: dict | None = None, *, stalled: bool = False,
            has_log: bool = False, game: dict | None = None, client: str = ''):
        sig = (c, row, stalled, has_log, bool(game), (game or {}).get('slug'), client)
        if sig == self._sig:
            return
        self._sig = sig
        self.c, self.row = c, row or {}
        title = c.get('title') or ''
        self.title.setText(title)
        self.title.setToolTip(title)
        src = ["FitGirl"] if (row or {}).get('repack_bytes') or c.get('repack_bytes') else []
        self.source.setText(" · ".join(src + ([client] if client else [])))
        self.art.set_game(title, game)
        what, fig, tag = state_line(c)
        st = c['state']
        self.step.setText(what)
        skipped = any(t.get('stage') == 'verify' and t.get('status') == 'skipped'
                      for t in c.get('track') or ())
        self.step.setToolTip(what + (f"\nVerify: {SKIPPED_VERIFY}" if skipped else ''))
        self.step.setProperty('role', 'danger' if st in ('failed', 'error') else '')
        repolish(self.step)
        self.figure.setText(fig)
        self.figure.setVisible(bool(fig))
        self.tag.setText(tag)
        self.tag.setProperty('kind', tag)
        repolish(self.tag)
        self.tag.setVisible(bool(tag))
        self.warn.setText(f"No new data for {pipeline.STALL_S // 60} min. The torrent may "
                          "have no one sharing it." if stalled else '')
        self.warn.setToolTip(self.warn.text())
        self.warn.setVisible(stalled)
        self.track.set_track(c.get('track') or [])
        self.track.shimmer = st in MOVING and not stalled and st != 'checking'
        overall = c.get('overall')
        show_pct = overall is not None and st not in ('ready', 'queued')
        self.pct.setText(str(int(overall * 100)) if show_pct else '')
        self.pctbox.setVisible(show_pct)
        eta = eta_line(c)
        self.eta.setText(eta)
        self.eta.setVisible(bool(eta))
        self.more.setVisible(bool(self.row))
        self._set_buttons(buttons(c, stalled, has_log, bool(game)))
        self._dim(st == 'queued')

    def sample(self, c: dict, now: float):
        """One poll's speed for the sparkline (the page calls it after set()):
        shown from two samples on, gone when the card stops moving."""
        self._spark = pipeline.spark_push(self._spark, c, now)
        pts = self._spark['pts']
        self.spark.set_series(self._spark['kind'], pts, now)
        self.spark.setVisible(len(pts) >= 2)

    def _dim(self, on: bool):
        """A queued card fades back (the mockup's .card.dim)."""
        if on and self.graphicsEffect() is None:
            fx = QGraphicsOpacityEffect(self)
            fx.setOpacity(0.75)
            self.setGraphicsEffect(fx)
        elif not on and self.graphicsEffect() is not None:
            self.setGraphicsEffect(None)

    def _set_buttons(self, spec: list[tuple[str, str, bool]]):
        want = [a for a, _t, _p in spec]
        for i, (act, text, primary) in enumerate(spec):
            b = self.btns.get(act)
            if b is None:
                b = QPushButton(self.btnhost)
                b.clicked.connect(self._clicked)
                b.setProperty('act', act)
                self.btns[act] = b
                self.btnrow.insertWidget(i, b)
            elif self.btnrow.indexOf(b) != i:
                self.btnrow.removeWidget(b)
                self.btnrow.insertWidget(i, b)
            b.setText(text.replace('&', '&&'))          # a literal &, not a mnemonic
            name = "Primary" if primary else ''
            if b.objectName() != name:
                b.setObjectName(name)
                repolish(b)
            b.show()                            # in its layout already: never a window
        leaving = [b for a, b in self.btns.items() if a not in want and not b.isHidden()]
        if any(b.hasFocus() for b in leaving):
            # Focus first, then hide (w-60): the new first action, else ⋮ if
            # it stays, else the card itself.
            self.default_focus(want).setFocus(Qt.FocusReason.OtherFocusReason)
        for b in leaving:
            b.hide()

    def _clicked(self):
        b = self.sender()
        if b is not None:
            self.action.emit(b.property('act'), self.c)

    def default_focus(self, want: list | None = None) -> QWidget:
        acts = want if want is not None else [a for a, b in self.btns.items()
                                              if not b.isHidden()]
        if acts:
            return self.btns[acts[0]]
        return self.more if not self.more.isHidden() else self

    # ---- layout ----
    def set_narrow(self, on: bool):
        """Below NARROW the side column goes under the card, in one row,
        and the track names only the current stage."""
        if on == self.narrow:
            return
        self.narrow = on
        self.track.compact = on
        self.track.update()
        self.grid.removeWidget(self.side)
        if on:
            self.grid.addWidget(self.side, 1, 0, 1, 2)
            self.sidebox.setDirection(QBoxLayout.Direction.LeftToRight)
            align = Qt.AlignmentFlag.AlignVCenter
            self.sidebox.setSpacing(SP['3'])
        else:
            self.grid.addWidget(self.side, 0, 2, Qt.AlignmentFlag.AlignVCenter)
            self.sidebox.setDirection(QBoxLayout.Direction.TopToBottom)
            align = Qt.AlignmentFlag.AlignRight
            self.sidebox.setSpacing(SP['1_5'])
        self._sizes()
        for w in (self.pctbox, self.eta, self.spark):
            self.sidebox.setAlignment(w, align)
        self.sidebox.setAlignment(self.btnhost, Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
        if on:
            self.sidebox.insertStretch(self.sidebox.indexOf(self.btnhost), 1)
        else:
            for i in range(self.sidebox.count()):
                item = self.sidebox.itemAt(i)
                if item is not None and item.spacerItem() is not None:
                    self.sidebox.takeAt(i)
                    break

    # ---- the torrent's own choices ----
    def items(self) -> list[tuple]:
        return torrent_items(self.row, self.row_sink, self.reorder) if self.row else []

    def _menu(self):
        items = self.items()
        if not items:
            return
        sheet = getattr(self.window(), 'sheet', None)
        if input_mode() == 'pad' and isinstance(sheet, ActionSheet):
            sheet.choose(self.c.get('title') or '', items)
            return
        m = QMenu(self)
        for text, _glyph, fn in items:
            m.addAction(text, fn)
        m.exec(self.more.mapToGlobal(QPoint(0, self.more.height())))
        m.deleteLater()


def ago(iso, now: float) -> str:
    then = pipeline.epoch(iso)
    if then is None:
        return ''
    s = max(0.0, now - then)
    if s < 3600:
        return f"{max(1, int(s // 60))} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    days = int(s // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


def done_line(r: dict, now: float) -> str:
    """Done recently's line: "Finished 3 h ago · 38.0 GB · took 2 h 10 min"
    (from the torrent's add, else from the install's start)."""
    bits = []
    when = ago(r.get('installed_at'), now)
    if when:
        bits.append(f"Finished {when}")
    size = r.get('original_bytes') or r.get('size')
    if size:
        bits.append(fmt_bytes(size))
    end = pipeline.epoch(r.get('installed_at'))
    start = float(r['added']) if isinstance(r.get('added'), (int, float)) and r['added'] \
        else pipeline.epoch(r.get('install_started'))
    if end is not None and start is not None and end > start:
        bits.append(f"took {span(end - start)}")
    return " · ".join(bits)


class DoneRow(QFrame):
    """One finished game in Done recently: title, when, size, how long it
    took from the torrent to Play, and Play / Show."""
    action = pyqtSignal(str, dict)          # play|show, {'slug','appid','title','hash'}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.r: dict = {}
        self._sig = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SP['3'], SP['2'], SP['2'], SP['2'])
        lay.setSpacing(SP['3'])
        col = QVBoxLayout()
        col.setSpacing(0)
        self.title = QLabel(self)
        self.title.setObjectName("PipeRowTitle")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(self.title)
        self.meta = QLabel(self)
        self.meta.setObjectName("PipeEta")
        self.meta.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(self.meta)
        lay.addLayout(col, 1)
        self.btn_play = QPushButton("Play", self)
        self.btn_play.clicked.connect(self._play)
        lay.addWidget(self.btn_play)
        self.btn_show = QPushButton("Show", self)
        self.btn_show.clicked.connect(self._show)
        lay.addWidget(self.btn_show)

    def set(self, r: dict, now: float, has_game: bool):
        sig = (r, int(now // 60), has_game)
        if sig == self._sig:
            return
        self._sig = sig
        self.r = r
        title = title_of(r)
        self.title.setText(title)
        self.title.setToolTip(title)
        self.meta.setText(done_line(r, now))
        if self.btn_play.hasFocus() and not has_game:
            self.btn_show.setFocus(Qt.FocusReason.OtherFocusReason)
        self.btn_play.setVisible(has_game)

    def _ref(self) -> dict:
        r = self.r
        return {'slug': r.get('slug'), 'appid': r.get('appid'), 'hash': r.get('hash'),
                'title': title_of(r) if r.get('title') or r.get('name') else ''}

    def _play(self):
        self.action.emit('play', self._ref())

    def _show(self):
        self.action.emit('show', self._ref())


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .actions import ActionSheet  # noqa: E402
from .common import alive  # noqa: E402
from .details import ElideLabel  # noqa: E402
from .downloads import torrent_items  # noqa: E402
from .input import input_mode  # noqa: E402
from .widgets import icon_button  # noqa: E402
