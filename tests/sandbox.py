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

if not os.environ.get('NEXUS_TEST_HOME'):
    if 'nexus.paths' in sys.modules:
        raise RuntimeError("tests/sandbox.py must be imported before nexus")
    _home = tempfile.mkdtemp(prefix='nexus-test-home-')
    atexit.register(shutil.rmtree, _home, True)
    os.environ['NEXUS_TEST_HOME'] = _home
os.environ['HOME'] = HOME = os.environ['NEXUS_TEST_HOME']
# The first-run setup page would replace Home in every window test.
os.environ.setdefault('NEXUS_NO_SETUP', '1')
# Unset, not pointed at HOME: then every path follows HOME, including in the
# subprocess tests that give the CLI a HOME of their own (a real
# XDG_CACHE_HOME would otherwise leak the user's cache into every test).
for _var in ('XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
    os.environ.pop(_var, None)
