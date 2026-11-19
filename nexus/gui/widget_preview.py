"""Live previews for the Customise gallery (w-158): each one is the widget's
real DashWidget class, filled from sample data (sample_home.py) and grabbed
to a pixmap, then thrown away.

    cache = PreviewCache(gallery)            # a QObject child of the gallery
    key = cache.key('week', 'small', QSize(130, 84))
    cache.pixmap(key)                        # None until it is rendered
    cache.queue([('week', 'small', box), ...])   # one render per event-loop tick
    cache.ready.connect(on_key)              # a key's pixmap is there

A preview card is parented to a hidden "bench" inside the gallery from the
moment it exists (never a window, it inherits the Home page's stylesheet),
never shown, and deleted right after grab(). While it fills, the sample
world stands in for the real one: a PreviewHub instead of the window, a
sample FRIENDS and COLLECTION_NAMES, and art fetches dropped, so a preview
never asks the CLI, the network or the user's files. A card that raises or
draws nothing becomes a plain tile (its badge and title), logged once."""

import sys
import time
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

from PyQt6.QtCore import pyqtSignal, QObject, QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap
from PyQt6.QtWidgets import QWidget

from . import sample_home
from .art import ART
from .theme import FONTS, FS, GRAD, LOOK, P, R, scaled, SP
from .widget_spec import SPEC

# A preview is the card laid out wide enough that it shrinks by SCALE into its
# box (the board's ~0.39): text stays close to legible, a one-column card is
# about as wide as on Home, a wide or full one a little narrower.
SCALE = 0.39


def ref_width(box: QSize) -> int:
    return round(box.width() / SCALE)


# ---- the sample world ----
def _noop(*_a, **_k):
    return None


class PreviewView:
    """Home's view as the shelf widgets and the Stage use it: rows and tiles
    are built the same way, but nothing is tracked and nothing is wired."""
    SHELF_TILE, WIDE_TILE = 150, 300

    def __init__(self, bench: QWidget):
        self._bench = bench
        self.tiles: dict = {}
        self.rows = None
        self.play = self.activated = _noop

    def viewport(self) -> QWidget:
        return self._bench

    def make_shelf(self, title, games, kind='cover', link=None, smart=None, tip='', look=None):
        from .library import HomeView
        return HomeView._shelf(self, title, games, (), kind, link, add=False, smart=smart,
                               tip=tip, look=look)

    def _tile(self, g, w, kind):
        from .tiles import GameTile
        return GameTile(g, width=w, kind=kind)

    def _track(self, _row):
        pass

    def _untrack(self, _row):
        pass

    def __getattr__(self, name):                  # signals the rows connect to lazily
        if name.startswith('__'):
            raise AttributeError(name)
        return SimpleNamespace(emit=_noop, connect=_noop)


class _Moments:
    """For you now's model, with the sample moments."""

    def __init__(self, moments):
        self.moments = moments

    def refresh(self) -> bool:
        return False

    def snoozed(self) -> int:
        return 0


class PreviewHub:
    """What a widget's fill reads from the window, made of sample data. Every
    action a widget can call is here and does nothing."""
    NEWS, DEALS = 'news', 'deals'

    def __init__(self, bench: QWidget, today: date):
        self.today = today
        self._games = sample_home.games(today)
        self._facts = sample_home.facts()
        self.sample = {'history': sample_home.history(today), 'moments': sample_home.moments(),
                       'insights': sample_home.insights(today)}
        items = [{'id': k, 'size': s.default, 'hidden': False} for k, s in SPEC.items()]
        for it in items:
            if it['id'] == 'foryou':
                it['style'] = 'strip'                 # its card, not the pill on the Stage
        self.prefs = SimpleNamespace(
            dashboard=items, kinds_off=[], snoozes={}, daily_rerolled=None, layouts=[],
            density='comfortable', smart_shelves=(), save=_noop, snooze=_noop,
            clear_snoozes=_noop, quiet_tomorrow=_noop, clear_quiet=_noop, save_as=_noop,
            user_shelves=lambda: [])
        self.config = {'news': True, 'gamedb_enabled': True, 'friends_enabled': True}
        self.homeview = PreviewView(bench)
        by_app = {str(g['store_appid']): g for g in self._games if g.get('store_appid')}
        by_app.update({g['store_appid']: g for g in self._games if g.get('store_appid')})
        self.newspage = SimpleNamespace(data={'items': sample_home.news(today)}, error='',
                                        games_by_appid=by_app, refresh=_noop, open_item=_noop,
                                        _image_for=lambda _it: (None, None))
        deals = sample_home.deals(today)
        self.dealspage = SimpleNamespace(_data=lambda: deals, error='', retry=_noop,
                                         refresh=_noop, _loaded=True, _base=lambda: '',
                                         _open=_noop)
        sysd = sample_home.system()
        self.jobs = SimpleNamespace(jobs=[])
        self._disks = sysd['disks']
        self.health = SimpleNamespace(checks=sysd['checks'])
        self.protonpage = SimpleNamespace(info=sysd['proton'], _install=_noop)
        self._native_crash = None
        self.storage = SimpleNamespace(sort_by=_noop)
        self.modal = SimpleNamespace(prompt=_noop, ask=_noop)
        stage_game = self._games[0]
        stage = SimpleNamespace(art=SimpleNamespace(games=[stage_game], game=stage_game),
                                picked=None, pin_title=lambda: None,
                                has_content=lambda _hub: True)
        self.dashboard = SimpleNamespace(
            feed=SimpleNamespace(data=self.sample['insights']), stage=stage, editing=False,
            items=items, hub=self, _cols=3, refresh=_noop, _undoable=_nothing)
        self.friends = sample_home.friends(time.time())
        self.collections = sorted({c for g in self._games for c in g.get('collections') or ()})

    def game(self, slug):
        return next((g for g in self._games if g.get('slug') == slug), None)

    def _smart_facts(self):
        return self._facts

    def update_split(self, games):
        return list(games), []

    go = _open_settings = _reload_friends = update_all = _tile_clicked = spin = _noop
    _show_menu = _reveal_game = open_report = _open_on_play = _open_job = _noop
    on_game_action = _moment_menu = moment_action = dash_update = _noop


@contextmanager
def _nothing():
    yield


@contextmanager
def _swap(name: str, value):
    """`nexus.gui.<module>.<name>` is `value` in every GUI module that holds
    the same object, until the block ends."""
    held = []
    orig = None
    for modname, mod in list(sys.modules.items()):
        if mod is None or not modname.startswith('nexus.gui'):
            continue
        cur = vars(mod).get(name)
        if cur is None:
            continue
        if orig is None:
            orig = cur
        if cur is orig:
            held.append(mod)
    try:
        for mod in held:
            setattr(mod, name, value)
        yield
    finally:
        for mod in held:
            setattr(mod, name, orig)


@contextmanager
def sample_world(hub: PreviewHub):
    """While a preview fills: the sample friends and collections, and no
    art is fetched (a cached file still shows)."""
    from .friends import FriendsData
    fr = FriendsData()
    fr.set_answer(hub.friends)
    mine = vars(ART)
    had = mine.get('fetch', _MISSING)
    ART.fetch = _noop                                   # this instance only
    try:
        with _swap('FRIENDS', fr), _swap('COLLECTION_NAMES', list(hub.collections)):
            yield
    finally:
        if had is _MISSING:
            del ART.fetch
        else:
            ART.fetch = had
        fr.deleteLater()


# ---- one preview ----
_LOGGED: set = set()
_MISSING = object()


def render(kind: str, size: str, box: QSize, bench: QWidget, hub: PreviewHub,
           options: dict | None = None) -> tuple[QPixmap, bool]:
    """The preview of `kind` at `size`, scaled to `box` (top of the card,
    cropped). Returns (pixmap, real): real is False for the plain fallback."""
    from .dashboard import _dash_options, DASH_WIDGETS
    cls = DASH_WIDGETS[kind]
    w = None
    try:
        with sample_world(hub):
            w = cls(bench)                              # never a window, not for an instant
            w.size_ = size
            w.options = dict(options) if options is not None else _dash_options(kind, {})
            w.bind(hub)
            w.set_editing(False, size)
            w.preview_seed(hub)
            w.fill(hub)
            pm = _grab(w, box)
    except Exception as e:                              # a preview must never break Customise
        if kind not in _LOGGED:
            _LOGGED.add(kind)
            sys.stderr.write(f"nexus: preview of {kind} failed: {e!r}\n")
        pm = None
    finally:
        if w is not None:
            w.hide()
            w.deleteLater()
    if pm is None:
        return fallback(kind, box, bench.devicePixelRatioF() if bench else 1.0), False
    return pm, True


def _grab(w: QWidget, box: QSize) -> QPixmap | None:
    width = ref_width(box)
    w.ensurePolished()
    lay = w.layout()
    h = w.heightForWidth(width) if w.hasHeightForWidth() else -1
    if h <= 0 and lay is not None:
        h = lay.totalHeightForWidth(width) if lay.hasHeightForWidth() \
            else lay.totalSizeHint().height()
    h = max(h, w.minimumSizeHint().height(), 1)
    k = box.width() / width
    w.resize(width, h)
    if lay is not None:
        lay.activate()
    src = w.grab()
    if src.isNull() or _blank(src):
        return None
    dpr = src.devicePixelRatio()
    out = QPixmap(QSize(int(box.width() * dpr), int(box.height() * dpr)))
    out.setDevicePixelRatio(dpr)
    out.fill(Qt.GlobalColor.transparent)
    pt = QPainter(out)
    pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    shown = min(h, box.height() / k)                   # the card's top, as much as fits
    pt.drawPixmap(QRectF(0, 0, box.width(), shown * k), src,
                  QRectF(0, 0, src.width(), shown * dpr))
    pt.end()
    return out


def _blank(pm: QPixmap) -> bool:
    """Nothing drawn: every sampled pixel the same."""
    img = pm.toImage().scaled(24, 24)
    first = img.pixel(0, 0)
    return all(img.pixel(x, y) == first for x in range(24) for y in range(24))


def fallback(kind: str, box: QSize, dpr: float = 1.0) -> QPixmap:
    """The plain tile: the widget's badge and title on the card surface."""
    from .dashboard import DASH_WIDGETS
    cls = DASH_WIDGETS[kind]
    out = QPixmap(QSize(max(1, int(box.width() * dpr)), max(1, int(box.height() * dpr))))
    out.setDevicePixelRatio(dpr)
    out.fill(QColor(P['surface']))
    pt = QPainter(out)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    d = scaled(22)
    r = QRectF(SP['2'], SP['2'], d, d)
    g = QLinearGradient(r.topLeft(), r.bottomRight())
    a, b = (QColor(c) for c in GRAD.get(cls.GRAD, GRAD['dusk']))
    g.setColorAt(0.0, a)
    g.setColorAt(1.0, b)
    pt.setPen(Qt.PenStyle.NoPen)
    pt.setBrush(g)
    pt.drawRoundedRect(r, R['sm'], R['sm'])
    if cls.GLYPH:
        f = QFont(FONTS.icon)
        f.setPixelSize(int(d * 0.5))
        pt.setFont(f)
        pt.setPen(QColor(P['on_accent']))
        pt.drawText(r, Qt.AlignmentFlag.AlignCenter, cls.GLYPH)
    f = QFont()
    f.setPixelSize(FS['xs'])
    pt.setFont(f)
    pt.setPen(QColor(P['subtext']))
    pt.drawText(QRectF(r.right() + SP['2'], r.top(), box.width() - r.right() - SP['3'], d),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                SPEC[kind].title)
    pt.end()
    return out


# ---- the cache ----
def theme_sig() -> int:
    return hash((tuple(sorted(P.items())), LOOK.get('scale')))


class PreviewCache(QObject):
    """Previews by key (kind, size, box, options, theme, scale, DPR, day).
    Tiles keep keys, never pixmaps (w-46): they ask `pixmap(key)` when they
    paint. `queue()` renders the missing ones lazily, one per event-loop tick,
    on a child timer; `ready` fires with each key that arrived."""
    ready = pyqtSignal(object)
    MAX = 160

    def __init__(self, parent: QWidget, today=None):
        super().__init__(parent)
        self._host = parent
        self._today = today or (lambda: date.today())
        self._pm: dict = {}
        self.real: dict = {}                    # key -> False for a fallback tile
        self._todo: list = []
        self._hub: PreviewHub | None = None
        self._bench: QWidget | None = None
        self.rendered = 0                       # renders done (tests, timings)
        self.active = False                     # the gallery is open: changed keys queue
        self.paused = False                     # the Layouts tab shows: hold the queue
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._step)

    def bench(self) -> QWidget:
        if self._bench is None:
            b = self._bench = QWidget(self._host)
            b.setObjectName("PreviewBench")
            b.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            b.hide()
        return self._bench

    def key(self, kind: str, size: str, box: QSize, options: dict | None = None) -> tuple:
        dpr = self._host.devicePixelRatioF() if self._host is not None else 1.0
        opts = tuple(sorted((options or {}).items()))
        return (kind, size, box.width(), box.height(), opts, theme_sig(), dpr,
                self._today().isoformat())

    def pixmap(self, key) -> QPixmap | None:
        return self._pm.get(key)

    def has(self, key) -> bool:
        return key in self._pm

    def get(self, kind: str, size: str, box: QSize, options: dict | None = None):
        return self._pm.get(self.key(kind, size, box, options))

    def invalidate(self):
        """Drop every preview (a theme or Size change): the next queue redraws."""
        self._pm.clear()
        self.real.clear()
        self._todo.clear()
        self._hub = None

    def queue(self, jobs, front: bool = False):
        """(kind, size, box[, options]) to render when missing, in order."""
        new = []
        for job in jobs:
            kind, size, box = job[:3]
            opts = job[3] if len(job) > 3 else None
            k = self.key(kind, size, box, opts)
            if k in self._pm or any(t[0] == k for t in self._todo + new):
                continue
            new.append((k, kind, size, box, opts))
        self._todo = new + self._todo if front else self._todo + new
        if self._todo and not self.paused and not self._timer.isActive():
            self._timer.start()

    def pause(self, on: bool):
        """Hold the queue (nothing to see) or carry on."""
        self.paused = bool(on)
        if on:
            self._timer.stop()
        elif self._todo and not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._timer.stop()
        self._todo.clear()

    def pending(self) -> int:
        return len(self._todo)

    def _step(self):
        if not self._todo or self.paused:
            self._timer.stop()
            return
        k, kind, size, box, opts = self._todo.pop(0)
        if k not in self._pm:
            self.render_now(k, kind, size, box, opts)
        if not self._todo:
            self._timer.stop()

    def render_now(self, k, kind, size, box, opts=None):
        today = self._today()
        if self._hub is None or self._hub.today != today:
            self._hub = PreviewHub(self.bench(), today)
        pm, real = render(kind, size, box, self.bench(), self._hub, opts)
        if len(self._pm) >= self.MAX:
            self._pm.pop(next(iter(self._pm)))
        self._pm[k] = pm
        self.real[k] = real
        self.rendered += 1
        self.ready.emit(k)

    def flush(self):
        """Render everything queued now (tests, PNGs)."""
        while self._todo:
            self._step()
