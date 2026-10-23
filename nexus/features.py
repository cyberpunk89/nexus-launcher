"""What this build of Nexus can do. Code that only makes sense on Linux asks
`has('proton')`, never `sys.platform`, so the Windows UI can be checked on
Linux by changing the set (tests, or NEXUS_FEATURES below).

NEXUS_FEATURES adjusts the set for debugging: a comma-separated list where
`-name` turns a feature off and `name` or `+name` turns it on, e.g.
NEXUS_FEATURES=-proton,-perf."""

import os
import sys

# Everything Linux has that Windows doesn't (yet).
LINUX_ONLY = frozenset({
    'proton',           # Proton/Wine runtimes, ProtonDB, the Proton page
    'umu',              # umu-launcher and its GAMEID
    'launch_profiles',  # env vars, wrappers, per-game Proton choice
    'perf',             # lsfg-vk, gamescope, MangoHud presets
    'protongen',
    'compatdata',       # Steam compatdata redirect and orphan cleanup
    'btrfs',            # chattr +C, filesystem checks
    'niri',             # window focus hand-off through niri
    'pause',            # SIGSTOP/SIGCONT a running game
    'tips',             # scx / LACT / system tuning tips
    'systemd_agent',    # the background agent as a systemd user service
    'kdeconnect',
    'desktop_entry',    # freedesktop .desktop launchers (Windows: Start Menu .lnk)
    'sunshine',         # exists on Windows; later
    'heroic',           # Heroic shortcuts and status (Heroic is skipped on Windows)
})

# On both, each its own way: still a feature so a build or test can drop it.
SHARED = frozenset({
    'save_backup',      # uninstall save backup and restore (Windows: the Ludusavi manifest)
})

ALL = LINUX_ONLY | SHARED
WINDOWS = SHARED


def _base() -> frozenset:
    return ALL if sys.platform != 'win32' else WINDOWS


def _parse(base: frozenset, spec: str) -> frozenset:
    out = set(base)
    for item in filter(None, (s.strip() for s in spec.split(','))):
        if item.startswith('-'):
            out.discard(item[1:])
        else:
            out.add(item.lstrip('+'))
    return frozenset(out)


FEATURES = _parse(_base(), os.environ.get('NEXUS_FEATURES', ''))


def has(name: str) -> bool:
    """True when this build supports `name` (one of ALL)."""
    if name not in ALL:
        raise KeyError(f"unknown feature {name!r}")
    return name in FEATURES
