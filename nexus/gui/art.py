"""Artwork: which image a game shows, the decode cache, pixmap helpers."""

import hashlib
import os
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QRect, QRectF, QSize, Qt, QUrl
from PyQt6.QtGui import (QBrush, QColor, QFont, QImage, QImageReader, QLinearGradient,
    QPainter, QPainterPath, QPixmap)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from nexus import net, paths as NX

from .theme import FONTS, P


# NEXUS_NO_MOTION=1 turns animations off (deterministic screenshots, or for
# anyone who prefers a still UI).
MOTION = os.environ.get('NEXUS_NO_MOTION') != '1'


def anim_ms(ms: int) -> int:
    return ms if MOTION else 0


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


def cover_pixmap(path: str, w: int, h: int) -> QPixmap | None:
    """Load and center-crop cover art to fill w x h (see cover_image)."""
    img = cover_image(path, w, h)
    return QPixmap.fromImage(img) if img is not None else None


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
    clip.addRoundedRect(QRectF(0, 0, src.width(), src.height()), radius, radius)
    pt.setClipPath(clip)
    pt.drawPixmap(0, 0, src)
    pt.end()
    return out


_PLACEHOLDERS: dict[tuple, QPixmap] = {}


def placeholder_pixmap(name: str, w: int, h: int) -> QPixmap:
    """Stand-in art: the spec's `{color.surface-2}` tile fill with a faint accent
    wash and the game's initials in muted text. Memoised (it's painted on
    every hero repaint while a game has no art)."""
    key = (name, w, h, P['surface_2'], P['accent'])
    pm = _PLACEHOLDERS.get(key)
    if pm is None:
        if len(_PLACEHOLDERS) > 400:
            _PLACEHOLDERS.clear()
        pm = _PLACEHOLDERS[key] = _paint_placeholder(name, w, h)
    return pm


def _paint_placeholder(name: str, w: int, h: int) -> QPixmap:
    out = QPixmap(w, h)
    out.fill(QColor(P['surface_2']))
    pt = QPainter(out)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, 0, h)
    top = QColor(P['accent']); top.setAlphaF(0.12)
    g.setColorAt(0.0, top)
    g.setColorAt(1.0, QColor(P['surface_2']))
    pt.fillRect(0, 0, w, h, QBrush(g))

    initials = ''.join(word[0] for word in name.split()[:3] if word).upper()[:3]
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
    network. Decoded pixmaps are memoised per (path, size)."""

    def __init__(self):
        super().__init__()
        self.dir = NX.CACHE_DIR / 'art'
        self.net = None
        self.waiting: dict[str, list] = {}
        # url -> when it failed: not asked again for RETRY_AFTER (offline,
        # every tile would wait out a timeout), but not given up on for good.
        self.failed: dict[str, float] = {}
        self.pix: dict[tuple, QPixmap] = {}    # insertion order = recency (LRU)
        # Decodes asked for by tiles: on the thread pool, so a grid of a few
        # hundred games appears at once and fills in without stalling.
        self.queue: dict[tuple, list] = {}
        self.thumbs = THUMB_DIR

    MAX_PIX = 900                              # decoded pixmaps kept (~a few hundred MB max)
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

    @staticmethod
    def _key(path, w, h, radius, grey):
        try:
            return (path, Path(path).stat().st_mtime, w, h, radius, grey)
        except (OSError, TypeError):
            return None

    def peek(self, path: str, w: int, h: int, grey: bool = False) -> QPixmap | None:
        """Already decoded? (No decoding here.)"""
        key = self._key(path, w, h, 0, grey)
        return self.pix.get(key) if key else None

    def request(self, path: str, w: int, h: int, callback, grey: bool = False):
        """Decode on the thread pool, then callback(pixmap or None) here."""
        key = self._key(path, w, h, 0, grey)
        if key is None:
            callback(None)
            return
        first = key not in self.queue
        self.queue.setdefault(key, []).append(callback)
        if first:
            thumbs = self.thumbs
            in_thread(lambda k=key: thumb_image(k[0], k[1], k[2], k[3], k[5], thumbs),
                      lambda img, _err, k=key: self._decoded(k, img))

    def _decoded(self, key, img):
        cbs = self.queue.pop(key, [])
        pm = QPixmap.fromImage(img) if img is not None else None
        if pm is not None:
            self.pix.pop(key, None)
            while len(self.pix) >= self.MAX_PIX:
                self.pix.pop(next(iter(self.pix)))
            self.pix[key] = pm
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
        key = (path, mtime, w, h, radius, grey)
        pm = self.pix.pop(key, None)
        if pm is None:
            pm = cover_pixmap(path, w, h)
            if pm is None:
                return None
            if grey:
                pm = QPixmap.fromImage(pm.toImage().convertToFormat(
                    QImage.Format.Format_Grayscale8))
            pm = rounded_pixmap(pm, radius)
            while len(self.pix) >= self.MAX_PIX:
                self.pix.pop(next(iter(self.pix)))
        self.pix[key] = pm
        return pm

    def pixmap_async(self, path: str, w: int, h: int, radius: float, callback):
        """pixmap(), decoded on the thread pool: callback(pixmap) here, now if
        it's already decoded, else once it is (never with None)."""
        try:
            key = (path, Path(path).stat().st_mtime, w, h, radius, False)
        except (OSError, TypeError):
            return
        if key in self.pix:
            callback(self.pixmap(path, w, h, radius))
            return

        def done(pm, key=key):
            if pm is None:
                return
            pm = rounded_pixmap(pm, radius)
            while len(self.pix) >= self.MAX_PIX:
                self.pix.pop(next(iter(self.pix)))
            self.pix[key] = pm
            callback(pm)
        self.request(path, w, h, done)


ART = ArtCache()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .jobs import in_thread  # noqa: E402
