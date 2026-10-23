"""Render assets/nexus.svg to a multi-size Windows .ico (the exe's icon and
the installer's). Uses only PyQt6, which the build has anyway: sizes below
256 are stored as 32-bit DIBs, 256 as PNG (what Explorer expects).

    python packaging/windows/make_icon.py <out.ico>
"""
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SVG = ROOT / 'assets' / 'nexus.svg'
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
RADIUS = 8 / 64          # the logo's corner radius, as a share of its side


def _render(renderer, size):
    from PyQt6.QtCore import QByteArray, QBuffer, QRectF, Qt
    from PyQt6.QtGui import QImage, QPainter, QPainterPath
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # Qt's SVG renderer ignores the logo's rounded clip-path: clip here.
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, size, size), size * RADIUS, size * RADIUS)
    p.setClipPath(clip)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    if size >= 256:
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        img.save(buf, 'PNG')
        buf.close()
        return bytes(data)
    # BITMAPINFOHEADER with the height doubled (XOR + AND masks), pixels
    # bottom-up in BGRA (Format_ARGB32's byte order on little-endian), then
    # an all-zero AND mask (the alpha channel does the work).
    header = struct.pack('<IiiHHIIiiII', 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    stride = size * 4
    raw = bytes(img.constBits().asstring(img.sizeInBytes()))
    rows = [raw[y * img.bytesPerLine():y * img.bytesPerLine() + stride] for y in range(size)]
    mask_row = ((size + 31) // 32) * 4
    return header + b''.join(reversed(rows)) + b'\0' * (mask_row * size)


def build_ico(out: Path) -> Path:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtSvg import QSvgRenderer
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 (kept alive)
    renderer = QSvgRenderer(str(SVG))
    if not renderer.isValid():
        raise SystemExit(f"can't render {SVG}")
    images = [(s, _render(renderer, s)) for s in SIZES]
    offset = 6 + 16 * len(images)
    head = struct.pack('<HHH', 0, 1, len(images))
    entries, blobs = b'', b''
    for size, blob in images:
        dim = 0 if size >= 256 else size            # 0 means 256
        entries += struct.pack('<BBBBHHII', dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(head + entries + blobs)
    return out


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(build_ico(Path(sys.argv[1])))
