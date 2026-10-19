"""The freedesktop/KDE desktop entry for a game."""

import shutil
import subprocess
from pathlib import Path

from .common import APPLICATIONS_DIR, atomic_write, log


def _desktop_path(slug: str) -> Path:
    return APPLICATIONS_DIR / f"fitgirl-{slug}.desktop"


def write_desktop_entry(display_name: str, slug: str, launch: Path,
                        artwork: dict | None) -> Path | None:
    """Write a .desktop launcher so the game appears in the app menu."""
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
