"""A game's entry in the app menu: the freedesktop/KDE desktop entry, or a
Start Menu shortcut on Windows."""

import os
import re
import shutil
import subprocess
from pathlib import Path

from nexus import features, platform

from .common import APPLICATIONS_DIR, atomic_write, log

# Not allowed in a Windows file name, plus control characters.
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r'^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$', re.I)


def _lnk_name(display_name: str) -> str:
    """The game's name as a Windows file name."""
    name = _BAD_NAME.sub('', display_name).strip().rstrip('. ') or 'Game'
    return f'{name} (game)' if _RESERVED.match(name) else name[:120]


def _desktop_path(slug: str) -> Path:
    if features.has('desktop_entry'):
        return APPLICATIONS_DIR / f"fitgirl-{slug}.desktop"
    entry = registry_get(slug) or {}
    if entry.get('menu_entry'):
        return Path(entry['menu_entry'])
    return APPLICATIONS_DIR / f"{_lnk_name(entry.get('display_name') or slug)}.lnk"


def write_start_menu_entry(display_name: str, slug: str, exe: Path,
                           aumid: str | None = None) -> Path:
    """A Start Menu shortcut to the game's exe, run from its folder (many
    games break when started from anywhere else); for a Store app (`aumid`)
    explorer.exe on its shell:AppsFolder URI. A rename replaces the old
    shortcut; the registry keeps the path when the game is registered."""
    path = APPLICATIONS_DIR / f"{_lnk_name(display_name)}.lnk"
    old = _desktop_path(slug)
    if aumid:
        from nexus import appx
        platform.create_shortcut(path, Path(os.environ.get('SystemRoot', r'C:\Windows'))
                                 / 'explorer.exe', args=appx.shell_uri(aumid),
                                 workdir=Path(exe).parent, icon=exe)
    else:
        platform.create_shortcut(path, exe, workdir=Path(exe).parent, icon=exe)
    if old != path and old.suffix == '.lnk':
        old.unlink(missing_ok=True)
    if registry_get(slug):
        update_registry_entry(slug, menu_entry=str(path))
    log.print(f"  Start Menu: {path}")
    return path


def write_desktop_entry(display_name: str, slug: str, launch: Path,
                        artwork: dict | None) -> Path | None:
    """Write a .desktop launcher so the game appears in the app menu. On
    Windows `launch` is the game's exe and this writes a Start Menu shortcut."""
    if not features.has('desktop_entry'):
        return write_start_menu_entry(display_name, slug, launch,
                                      (registry_get(slug) or {}).get('appx'))
    icon = 'applications-games'
    if artwork:
        icon_src = artwork.get('icon') or artwork.get('cover')
        if icon_src:
            icon = str(icon_src)

    # Desktop Entry spec: string values escape backslash and line breaks;
    # Exec args are double-quoted with " ` $ \ backslash-escaped inside the
    # quotes, then the whole value gets the string escaping, and a literal %
    # must be written %%.
    def esc(s: str) -> str:
        return (s.replace('\\', '\\\\').replace('\n', '\\n')
                 .replace('\r', '\\r'))

    def exec_arg(a: str) -> str:
        for ch in ('\\', '"', '`', '$'):
            a = a.replace(ch, '\\' + ch)
        return f'"{a}"'

    exec_line = esc(f"bash {exec_arg(str(launch))}").replace('%', '%%')
    path = _desktop_path(slug)
    atomic_write(path,
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={esc(display_name)}\n"
        f"Comment=Installed by Nexus\n"
        f"Exec={exec_line}\n"
        f"Icon={esc(icon)}\n"
        "Categories=Game;\n"
        "Terminal=false\n"
        f"X-FitGirl-Slug={slug}\n"
    )
    path.chmod(0o644)
    if shutil.which('update-desktop-database'):
        try:
            subprocess.run(['update-desktop-database', str(APPLICATIONS_DIR)],
                           capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
    log.print(f"  Desktop entry: {path}")
    return path


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .registry import registry_get, update_registry_entry  # noqa: E402
