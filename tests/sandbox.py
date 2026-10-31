"""Imported by every test module before anything from nexus: points HOME at
a throwaway directory and clears the XDG overrides. nexus.paths is evaluated from $HOME at
import time, so without this the in-process tests (the GUI ones especially)
read and write the real ~/.local/share/nexus and ~/.cache/nexus. Subprocess
tests inherit it too. Set NEXUS_TEST_HOME yourself to reuse one directory."""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

if not os.environ.get('NEXUS_TEST_HOME'):
    if 'nexus.paths' in sys.modules:
        raise RuntimeError("tests/sandbox.py must be imported before nexus")
    _home = tempfile.mkdtemp(prefix='nexus-test-home-')
    atexit.register(shutil.rmtree, _home, True)
    os.environ['NEXUS_TEST_HOME'] = _home


def home_env(home) -> dict:
    """The variables that say where home is, all pointing into `home`: HOME on
    Linux; USERPROFILE (what Path.home() reads there), APPDATA, LOCALAPPDATA,
    NEXUS_STEAM_ROOT (else the guest's real Steam is found through the
    registry) and NEXUS_INSTALL_BASE (else games go to a real drive's
    \\Games) on Windows. Subprocess tests that give the CLI a home of their
    own spread this into its env, so on Windows it can't fall back to the real
    profile."""
    home = str(home)
    env = {'HOME': home, 'USERPROFILE': home,
           'APPDATA': os.path.join(home, 'AppData', 'Roaming'),
           'LOCALAPPDATA': os.path.join(home, 'AppData', 'Local')}
    if sys.platform == 'win32':
        env['NEXUS_STEAM_ROOT'] = str(steam_root(home))
        env['NEXUS_INSTALL_BASE'] = os.path.join(home, 'Games', 'FitGirl')
    return env


# Where Nexus keeps things under a given home, for tests that build fixtures
# there. Linux: ~/.local/share/nexus, ~/.config/nexus, ~/.cache/nexus;
# Windows: the AppData folders (see nexus.platform). Not imported from nexus:
# this module must load before it.
def _win():
    return sys.platform == 'win32'


def data_dir(home):
    home = Path(home)
    return home / 'AppData/Local/nexus-launcher' if _win() else home / '.local/share/nexus'


def config_dir(home):
    home = Path(home)
    return home / 'AppData/Roaming/nexus-launcher' if _win() else home / '.config/nexus'


def cache_dir(home):
    home = Path(home)
    return data_dir(home) / 'Cache' if _win() else home / '.cache/nexus'


def steam_root(home):
    """The Steam folder fixtures use: ~/.local/share/Steam on both (Windows
    gets there through NEXUS_STEAM_ROOT)."""
    return Path(home) / '.local/share/Steam'


HOME = os.environ['NEXUS_TEST_HOME']
os.environ.update(home_env(HOME))
# The first-run setup page would replace Home in every window test.
os.environ.setdefault('NEXUS_NO_SETUP', '1')
# Theatre's menu sounds (w-65) never open the audio device in a test. Forced,
# not setdefault: a shell that exported 0 must not make the suite beep.
os.environ['NEXUS_NO_SOUND'] = '1'
os.environ.setdefault('NEXUS_NO_MOTION', '1')   # the tests wait on 0 ms timers (CI had them at 140 ms)
# Unset, not pointed at HOME: then every path follows HOME, including in the
# subprocess tests that give the CLI a HOME of their own (a real
# XDG_CACHE_HOME would otherwise leak the user's cache into every test).
for _var in ('XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
    os.environ.pop(_var, None)
