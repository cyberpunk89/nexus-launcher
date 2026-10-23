"""Fetch SDL2.dll (x64) for the Windows build into packaging/windows/vendor/.

Pinned to one SDL2 release and the SHA-256 of its zip; a mismatch stops the
build. nexus/gamepad.py loads it with ctypes for controller input. SDL2 is
zlib-licensed; its README-SDL.txt is kept beside the DLL.

    python packaging/windows/fetch_sdl2.py [dest-dir]

To update: change VERSION, take the new zip's sha256 (GitHub shows it on the
release page, or `gh api repos/libsdl-org/SDL/releases/tags/release-<v>`).
"""
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = '2.32.10'
SHA256 = '6cf9706eefd0a4a06dc764007934d428afaf029fabdd408a9e646048c91e18fb'
URL = (f'https://github.com/libsdl-org/SDL/releases/download/release-{VERSION}/'
       f'SDL2-{VERSION}-win32-x64.zip')
WANT = ('SDL2.dll', 'README-SDL.txt')


def fetch(dest: Path) -> Path:
    dll = dest / 'SDL2.dll'
    stamp = dest / 'SDL2.version'
    if dll.exists() and stamp.exists() and stamp.read_text().strip() == f'{VERSION} {SHA256}':
        print(f"{dll} already at SDL2 {VERSION}")
        return dll
    with urllib.request.urlopen(URL, timeout=120) as r:
        blob = r.read()
    got = hashlib.sha256(blob).hexdigest()
    if got != SHA256:
        raise SystemExit(f"SDL2 {VERSION}: sha256 {got} != pinned {SHA256}")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in WANT:
            (dest / name).write_bytes(z.read(name))
    stamp.write_text(f'{VERSION} {SHA256}\n')
    print(f"{dll}: SDL2 {VERSION}")
    return dll


if __name__ == '__main__':
    fetch(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / 'vendor')
