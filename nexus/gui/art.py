"""Artwork: which image a game shows, the decode cache, pixmap helpers."""

import hashlib
import os
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QRect, QRectF, QSize, Qt, QUrl
from PyQt6.QtGui import (QBrush, QColor, QFont, QGuiApplication, QImage, QImageReader,
    QLinearGradient, QPainter, QPainterPath, QPixmap)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from nexus import net, paths as NX

from .theme import FONTS, P


# NEXUS_NO_MOTION=1 turns animations off (deterministic screenshots, or for
# anyone who prefers a still UI).
MOTION = os.environ.get('NEXUS_NO_MOTION') != '1'


def anim_ms(ms: int) -> int:
    return ms if MOTION else 0


def screen_dpr() -> float:
    """Device pixels per logical pixel (1.25 at 125% display scaling): art
    is decoded at w*dpr x h*dpr and tagged with it, so it stays sharp and
    is still drawn at w x h."""
    app = QGuiApplication.instance()
    return app.devicePixelRatio() if app is not None else 1.0


def _device(n: int, dpr: float) -> int:
    return max(1, round(n * dpr))


def hidpi_pixmap(img: QImage, dpr: float) -> QPixmap:
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm


def pixmap_bytes(pm: QPixmap | None) -> int:
    """What a decoded pixmap costs in memory (device pixels x depth)."""
    if pm is None or pm.isNull():
        return 0
    return pm.width() * pm.height() * max(8, pm.depth()) // 8


_TINTS: dict[str, QColor] = {}


def dominant_color(path: str | None) -> QColor | None:
    """Average colour of an image, lifted into a usable glow: saturated and
    bright enough to read on the dark base. Cached per path."""
    if not path:
        return None
    try:
        key = f"{path}:{Path(path).stat().st_mtime}"
    except OSError:
        return None
    if key in _TINTS:
        return _TINTS[key]
    reader = QImageReader(path)
    reader.setScaledSize(QSize(48, 48))     # a JPEG decodes at a fraction of the cost
    img = reader.read()
    if img.isNull():
        return None
    c = img.scaled(24, 24, Qt.AspectRatioMode.IgnoreAspectRatio,
                   Qt.TransformationMode.SmoothTransformation).scaled(
        1, 1, Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation).pixelColor(0, 0)
    hsv_h, hsv_s, hsv_v, _ = c.getHsvF()
    if hsv_h < 0:                           # greyscale art: keep the brand glow
        out = QColor(P['accent'])
    else:
        out = QColor.fromHsvF(hsv_h, max(0.45, min(0.75, hsv_s * 1.4)),
                              max(0.75, hsv_v))
    _TINTS[key] = out
    return out


def fit_image(path: str, w: int, h: int, cover: bool) -> QImage | None:
    """The image at `path` decoded at about w x h, aspect kept: covering the
    box (cover=True) or fitting inside it. No crop. Worker-safe (QImage)."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > 0 and size.height() > 0:
        kw, kh = w / size.width(), h / size.height()
        k = max(kw, kh) if cover else min(kw, kh)
        if k < 1:
            reader.setScaledSize(QSize(max(1, round(size.width() * k)),
                                       max(1, round(size.height() * k))))
    img = reader.read()
    return None if img.isNull() else img


def screen_pixmap(path: str | None, widget) -> QPixmap | None:
    """Full-screen art (Theatre): the image decoded to cover the widget's
    screen, never larger. A 3840x1240 hero on a 1080p screen is then a
    third of the pixels, and nothing bigger is kept."""
    if not path:
        return None
    screen = widget.screen() if widget is not None else None
    screen = screen or QGuiApplication.primaryScreen()
    if screen is None:
        img = fit_image(path, 1920, 1080, cover=True)
    else:
        size, dpr = screen.size(), screen.devicePixelRatio()
        img = fit_image(path, _device(size.width(), dpr), _device(size.height(), dpr),
                        cover=True)
    return QPixmap.fromImage(img) if img is not None else None


def cover_pixmap(path: str, w: int, h: int) -> QPixmap | None:
    """Load and center-crop cover art to fill w x h logical pixels (see
    cover_image), at the screen's device pixel ratio."""
    dpr = screen_dpr()
    img = cover_image(path, _device(w, dpr), _device(h, dpr))
    return hidpi_pixmap(img, dpr) if img is not None else None


def cover_image(path: str, w: int, h: int, grey: bool = False) -> QImage | None:
    """Load and center-crop cover art to fill w x h. Corners stay square — the
    tile clips to its own radius when painting. Decoded straight at (about)
    the size it's shown at: a JPEG then skips most of its pixels, which is
    several times faster than decoding a 600x900 cover and scaling it down.
    QImage only, so it's safe on a worker thread."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > w and size.height() > h:
        k = max(w / size.width(), h / size.height())
        reader.setScaledSize(QSize(max(w, round(size.width() * k)),
                                   max(h, round(size.height() * k))))
    img = reader.read()
    if img.isNull():
        img = QImage(path)                   # formats the reader can't size
        if img.isNull():
            return None
    if img.width() != w and img.height() != h or img.width() < w or img.height() < h:
        img = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                         Qt.TransformationMode.SmoothTransformation)
    x = max(0, (img.width() - w) // 2)
    y = max(0, (img.height() - h) // 2)
    img = img.copy(x, y, w, h)
    if grey:
        img = img.convertToFormat(QImage.Format.Format_Grayscale8)
    return img


THUMB_DIR = NX.CACHE_DIR / 'thumbs'
THUMB_MIN_BYTES = 150_000          # smaller sources decode about as fast as a thumbnail


def thumb_image(path: str, mtime: float, w: int, h: int, grey: bool = False,
                thumbs: Path | None = None) -> QImage | None:
    """cover_image() through a disk cache of the scaled result — for big
    sources (SteamGridDB PNGs run to megabytes and can't be decoded at a
    smaller size), so the next start skips the full decode. Worker-safe."""
    thumbs = thumbs or THUMB_DIR
    try:
        big = os.stat(path).st_size >= THUMB_MIN_BYTES
    except OSError:
        return None
    if not big:
        return cover_image(path, w, h, grey)
    name = hashlib.sha1(f"{path}|{mtime}|{w}x{h}|{int(grey)}".encode()).hexdigest()
    cached = thumbs / f"{name}.png"
    img = QImage(str(cached))
    if not img.isNull():
        return img
    img = cover_image(path, w, h, grey)
    if img is not None:
        try:
            thumbs.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_suffix(f'.{os.getpid()}.part')
            if img.save(str(tmp), 'PNG', 80):
                os.replace(tmp, cached)
        except OSError:
            pass
    return img


def rounded_pixmap(src: QPixmap, radius: float) -> QPixmap:
    """`src` with transparent rounded corners — for images shown in widgets
    that can't clip (QLabel pixmaps, item-view icons): a QSS radius never
    clips the image itself."""
    if radius <= 0 or src.isNull():
        return src
    out = QPixmap(src.size())
    out.setDevicePixelRatio(src.devicePixelRatio())
    out.fill(Qt.GlobalColor.transparent)
    pt = QPainter(out)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    size = src.deviceIndependentSize()           # logical: the painter's units
    clip.addRoundedRect(QRectF(0, 0, size.width(), size.height()), radius, radius)
    pt.setClipPath(clip)
    pt.drawPixmap(0, 0, src)
    pt.end()
    return out


_PLACEHOLDERS: dict[tuple, QPixmap] = {}     # oldest first
PLACEHOLDER_BYTES = 16 << 20


def _initials(name: str) -> str:
    return ''.join(word[0] for word in name.split()[:3] if word).upper()[:3]


def placeholder_pixmap(name: str, w: int, h: int) -> QPixmap:
    """Stand-in art: the spec's `{color.surface-2}` tile fill with a faint accent
    wash and the game's initials in muted text. Memoised per initials (it's
    painted on every hero repaint while a game has no art, and by every tile
    of a game without art), the most recent PLACEHOLDER_BYTES of them."""
    key = (_initials(name), w, h, P['surface_2'], P['accent'], screen_dpr())
    pm = _PLACEHOLDERS.pop(key, None)
    if pm is None:
        pm = _paint_placeholder(name, w, h, key[-1])
        used = sum(pixmap_bytes(p) for p in _PLACEHOLDERS.values()) + pixmap_bytes(pm)
        while _PLACEHOLDERS and used > PLACEHOLDER_BYTES:
            used -= pixmap_bytes(_PLACEHOLDERS.pop(next(iter(_PLACEHOLDERS))))
    _PLACEHOLDERS[key] = pm
    return pm


def _paint_placeholder(name: str, w: int, h: int, dpr: float = 1.0) -> QPixmap:
    out = QPixmap(_device(w, dpr), _device(h, dpr))
    out.setDevicePixelRatio(dpr)
    out.fill(QColor(P['surface_2']))
    pt = QPainter(out)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, 0, h)
    top = QColor(P['accent']); top.setAlphaF(0.12)
    g.setColorAt(0.0, top)
    g.setColorAt(1.0, QColor(P['surface_2']))
    pt.fillRect(0, 0, w, h, QBrush(g))

    initials = _initials(name)
    pt.setPen(QColor(P['muted']))
    pt.setFont(QFont(FONTS.ui, 26, QFont.Weight.Medium))
    pt.drawText(QRect(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, initials or '?')
    pt.end()
    return out


# --------------------------------------------------------------------------
# Artwork lookup — one place decides which image a game shows for each role
# --------------------------------------------------------------------------

def art_lookup(game: dict, kinds) -> tuple[str | None, str | None, str | None]:
    """First available image for the given kinds, in order: a local file the
    library knows about, a cached remote URL, or SteamGridDB art in the
    game's artwork dir. Returns (path, url_to_fetch, kind): when nothing is
    on disk yet, the first remote URL is handed back so the caller can fetch
    it."""
    pending = None
    adir = game.get('artwork_dir')
    for kind in kinds:
        p = game.get(kind)
        if p and Path(p).exists():
            return p, None, kind
        url = game.get(f'{kind}_url')
        if url:
            cached = ART.cached(url)
            if cached:
                return cached, None, kind
            pending = pending or url
        if adir and Path(adir).is_dir():
            hits = sorted(Path(adir).glob(f'{kind}.*'))
            if hits:
                return str(hits[0]), None, kind
    return None, pending, None


def art_source(game: dict, kinds) -> tuple[str | None, str | None]:
    path, url, _kind = art_lookup(game, kinds)
    return path, url


HERO_KINDS = ('hero', 'wide', 'cover')
WIDE_KINDS = ('wide', 'hero', 'cover')
COVER_KINDS = ('cover',)
LOGO_KINDS = ('logo',)


class ArtCache(QObject):
    """Remote cover/hero art (Steam CDN, Epic/GOG URLs) cached on disk under
    ~/.cache/nexus/art, fetched asynchronously so a tile never waits on the
    network. Decoded pixmaps are memoised per (path, size) in an LRU capped
    by bytes (BUDGET), not by count: a hero costs ten covers. Widgets ask
    for theirs when they paint (get / request) and don't keep their own copy,
    or the cap wouldn't cap anything."""

    def __init__(self):
        super().__init__()
        self.dir = NX.CACHE_DIR / 'art'
        self.net = None
        self.waiting: dict[str, list] = {}
        # url -> when it failed: not asked again for RETRY_AFTER (offline,
        # every tile would wait out a timeout), but not given up on for good.
        self.failed: dict[str, float] = {}
        self.pix: dict[tuple, QPixmap] = {}    # insertion order = recency (LRU)
        self.used = 0                          # bytes in self.pix
        self.budget = self.BUDGET
        # Decodes asked for by tiles: on the thread pool, so a grid of a few
        # hundred games appears at once and fills in without stalling.
        self.queue: dict[tuple, list] = {}
        self.queued = 0                        # bytes those decodes will take
        self.thumbs = THUMB_DIR

    # Decoded art kept: about 680 grid tiles at 100 % scaling, 170 at 200 %;
    # a screenful is a few dozen.
    BUDGET = 160 << 20
    RETRY_AFTER = 600                          # seconds before a failed download is retried

    def forget_failures(self):
        """A manual refresh tries every failed image again."""
        self.failed.clear()

    def _path(self, url: str) -> Path:
        return self.dir / (hashlib.sha1(url.encode()).hexdigest() + '.img')

    def cached(self, url: str) -> str | None:
        p = self._path(url)
        return str(p) if p.exists() and p.stat().st_size > 0 else None

    def fetch(self, url: str, callback):
        if not url.startswith('https://') or \
                time.monotonic() - self.failed.get(url, -1e9) < self.RETRY_AFTER:
            return
        if url in self.waiting:
            self.waiting[url].append(callback)
            return
        self.waiting[url] = [callback]
        if self.net is None:
            self.net = QNetworkAccessManager(self)
        req = QNetworkRequest(QUrl(url))
        req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, 'nexus')
        req.setTransferTimeout(30000)
        reply = self.net.get(req)
        # Stop an oversized "image" as it arrives, not after it's all in memory.
        reply.downloadProgress.connect(
            lambda got, _total, r=reply: r.abort() if got >= self.MAX_BYTES else None)
        reply.finished.connect(lambda r=reply, u=url: self._done(r, u))

    MAX_BYTES = 16 << 20

    def _done(self, reply, url):
        data = bytes(reply.readAll())
        ok = (reply.error() == QNetworkReply.NetworkError.NoError
              and 0 < len(data) < self.MAX_BYTES)
        reply.deleteLater()
        if not ok:
            self.waiting.pop(url, None)
            self.failed[url] = time.monotonic()
            return
        path = self._path(url)

        def store():                       # worker: check it's an image, write it
            if QImage.fromData(data).isNull():
                return None
            net.write_atomic(path, data)
            return str(path)
        in_thread(store, lambda saved, _err, u=url: self._stored(u, saved))

    def _stored(self, url, saved):
        cbs = self.waiting.pop(url, [])
        if not saved:
            self.failed[url] = time.monotonic()
            return
        for cb in cbs:
            try:
                cb(saved)
            except RuntimeError:                 # the tile is gone
                pass

    # ---- the decoded LRU ----
    def _keep(self, key, pm: QPixmap):
        """Store pm as the most recent entry, then drop the oldest until the
        total fits the budget (the newest always stays, however big)."""
        old = self.pix.pop(key, None)
        self.used -= pixmap_bytes(old)
        self.pix[key] = pm
        self.used += pixmap_bytes(pm)
        while self.used > self.budget and len(self.pix) > 1:
            self.used -= pixmap_bytes(self.pix.pop(next(iter(self.pix))))

    def get(self, key) -> QPixmap | None:
        """The pixmap decoded for key (see key()), now the most recent; None
        if it isn't decoded (or was dropped). Cheap: no file access."""
        pm = self.pix.pop(key, None)
        if pm is not None:
            self.pix[key] = pm
        return pm

    def has_room(self) -> bool:
        """Decoding ahead (art not on screen yet) is only worth it while what
        is decoded and on its way still fits: past that it would push out
        art that is on screen."""
        return self.used + self.queued < self.budget * 3 // 4

    @staticmethod
    def key(path, w, h, grey=False):
        """The cache key of path decoded at w x h logical px (None: no such
        file). It holds the file's mtime, so a newly picked image replaces
        the old one."""
        return ArtCache._key(path, w, h, 0, grey)

    @staticmethod
    def _key(path, w, h, radius, grey):
        try:
            return (path, Path(path).stat().st_mtime, w, h, radius, grey, screen_dpr())
        except (OSError, TypeError):
            return None

    def peek(self, path: str, w: int, h: int, grey: bool = False) -> QPixmap | None:
        """Already decoded? (No decoding here.)"""
        key = self._key(path, w, h, 0, grey)
        return self.get(key) if key else None

    def request(self, path: str, w: int, h: int, callback, grey: bool = False):
        """Decode on the thread pool, then callback(pixmap or None) here."""
        key = self._key(path, w, h, 0, grey)
        if key is None:
            callback(None)
            return
        self.request_key(key, callback)

    def request_key(self, key, callback=None):
        """request() for a key from key(); no callback: just decode it into
        the cache (decoding ahead)."""
        first = key not in self.queue
        cbs = self.queue.setdefault(key, [])
        if callback is not None:
            cbs.append(callback)
        if first:
            self.queued += _device(key[2], key[6]) * _device(key[3], key[6]) * 4
            thumbs = self.thumbs
            in_thread(lambda k=key: thumb_image(k[0], k[1], _device(k[2], k[6]),
                                                _device(k[3], k[6]), k[5], thumbs),
                      lambda img, _err, k=key: self._decoded(k, img))

    def _decoded(self, key, img):
        cbs = self.queue.pop(key, [])
        self.queued = max(0, self.queued - _device(key[2], key[6]) * _device(key[3], key[6]) * 4)
        pm = hidpi_pixmap(img, key[6]) if img is not None else None
        if pm is not None:
            self._keep(key, pm)
        for cb in cbs:
            try:
                cb(pm)
            except RuntimeError:                 # the tile is gone
                pass

    def pixmap(self, path: str, w: int, h: int, radius: float = 0,
               grey: bool = False) -> QPixmap | None:
        """Centre-cropped w x h art, optionally with rounded corners or in
        greyscale. Keyed by mtime too, so a newly picked image replaces the
        old one."""
        try:
            mtime = Path(path).stat().st_mtime if path else None
        except OSError:
            mtime = None
        if mtime is None:
            return None
        key = (path, mtime, w, h, radius, grey, screen_dpr())
        pm = self.get(key)
        if pm is None:
            pm = cover_pixmap(path, w, h)
            if pm is None:
                return None
            if grey:
                pm = hidpi_pixmap(pm.toImage().convertToFormat(
                    QImage.Format.Format_Grayscale8), pm.devicePixelRatio())
            pm = rounded_pixmap(pm, radius)
            self._keep(key, pm)
        return pm

    def pixmap_async(self, path: str, w: int, h: int, radius: float, callback):
        """pixmap(), decoded on the thread pool: callback(pixmap) here, now if
        it's already decoded, else once it is (never with None)."""
        try:
            key = (path, Path(path).stat().st_mtime, w, h, radius, False, screen_dpr())
        except (OSError, TypeError):
            return
        if key in self.pix:
            callback(self.pixmap(path, w, h, radius))
            return

        def done(pm, key=key):
            if pm is None:
                return
            pm = rounded_pixmap(pm, radius)
            self._keep(key, pm)
            callback(pm)
        self.request(path, w, h, done)


ART = ArtCache()


def return_freed_images():
    """Linux (glibc): give a freed decoded image's memory back to the system
    at once. glibc raises its mmap threshold after the first big free, so
    later tile-sized buffers (a few hundred KB) come from heap arenas that
    keep freed memory: scrolling a 3,000-game grid with the cache capped at
    160 MB still grew RSS to ~900 MB. A fixed threshold keeps every buffer of
    128 KB or more in its own mapping, unmapped on free. Called once, from
    main(); harmless where it doesn't apply."""
    if not sys.platform.startswith('linux'):
        return
    try:
        import ctypes
        M_MMAP_THRESHOLD = -3
        ctypes.CDLL('libc.so.6').mallopt(M_MMAP_THRESHOLD, 128 * 1024)
    except (OSError, AttributeError):          # not glibc (musl has no mallopt)
        pass


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .jobs import in_thread  # noqa: E402
