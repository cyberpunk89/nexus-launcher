"""Reading an Inno Setup installer's header (FitGirl's setup.exe is always
one) without running it: stdlib only, so the safety check doesn't have to
load the whole CLI."""

import struct
import zlib
from pathlib import Path


_INNO_TAG = b'Inno Setup Setup Data ('
_INNO_MAX = 64 << 20                     # a setup header is KBs; never inflate past this


def inno_header(setup_exe: Path) -> bytes | None:
    """The decompressed setup header of an Inno installer (its components,
    [Files] and [Run] entries), or None when it isn't one we can read. The
    header follows the 64-byte "Inno Setup Setup Data (x.y.z)" tag: a CRC of
    the next 5 bytes, the stored size, a compressed flag, then 4 KiB chunks
    each led by their own CRC — LZMA1, 5 bytes of properties first. Mapped,
    not read: a single-file installer can be gigabytes. Every CRC is checked,
    so a stray copy of the tag (or a format we don't know) reads as None."""
    import lzma
    import mmap
    try:
        with open(setup_exe, 'rb') as fh, \
                mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as data:
            p = data.find(_INNO_TAG)
            while p >= 0:
                out = _inno_block(data, p + 64, lzma)
                if out is not None:
                    return out
                p = data.find(_INNO_TAG, p + 1)
    except (OSError, ValueError, MemoryError):
        pass
    return None


def _inno_block(data, p: int, lzma) -> bytes | None:
    try:
        crc, size, compressed = struct.unpack_from('<IIB', data, p)
        if zlib.crc32(data[p + 4:p + 9]) != crc or size > _INNO_MAX:
            return None
        p += 9
        end, raw = p + size, bytearray()
        while p < end:
            ccrc = struct.unpack_from('<I', data, p)[0]
            chunk = data[p + 4:min(p + 4 + 4096, end)]
            if zlib.crc32(chunk) != ccrc:
                return None
            raw += chunk
            p += 4 + 4096
        if not compressed:
            return bytes(raw)
        lclppb, dict_size = raw[0], struct.unpack_from('<I', raw, 1)[0]
        lc, lp, pb = lclppb % 9, lclppb // 9 % 5, lclppb // 45
        dec = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[
            {'id': lzma.FILTER_LZMA1, 'dict_size': dict_size, 'lc': lc, 'lp': lp, 'pb': pb}])
        return dec.decompress(bytes(raw[5:]), max_length=_INNO_MAX)
    except (struct.error, IndexError, lzma.LZMAError, ValueError, MemoryError):
        return None
