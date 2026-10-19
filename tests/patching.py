"""Patching names in nexus.cli and nexus.gui, which are packages split into
modules that each import the names they use. Patching the package alone
(mock.patch.object(nexus.cli, ...)) would miss every module already holding
its own reference, so these patch the name in each module of the package
that holds the same object.

    with patch_cli('load_config', return_value={}): ...
    p = patch_gui('cli_json', fake); p.start(); self.addCleanup(p.stop)
    set_gui('cli_json', fake)        # a plain rebind, undone by another set_gui
"""

import importlib
import sys
from unittest import mock

_MISSING = object()


def _holders(package: str, name: str, extra=()):
    """(module, current value) for each module of `package` holding the
    object `package.<name>` holds now."""
    facade = importlib.import_module(package)
    orig = getattr(facade, name)                # AttributeError for a typo, like patch
    out = []
    for modname, mod in list(sys.modules.items()):
        if mod is None or not (modname == package or modname.startswith(package + '.')
                               or modname in extra):
            continue
        if vars(mod).get(name, _MISSING) is orig:
            out.append(mod)
    return out


class _Patch:
    PACKAGE, EXTRA = '', ()

    def __init__(self, name: str, new=_MISSING, **kwargs):
        self.name, self.new, self.kwargs = name, new, kwargs
        self._patches = []

    def start(self):
        new = mock.MagicMock(**self.kwargs) if self.new is _MISSING else self.new
        for mod in _holders(self.PACKAGE, self.name, self.EXTRA):
            p = mock.patch.object(mod, self.name, new)
            p.start()
            self._patches.append(p)
        return new

    def stop(self):
        while self._patches:
            self._patches.pop().stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


class patch_cli(_Patch):
    PACKAGE, EXTRA = 'nexus.cli', ('nexus.inno',)


class patch_gui(_Patch):
    PACKAGE = 'nexus.gui'


def set_gui(name: str, value) -> None:
    """`nexus.gui.<name> = value`, in every GUI module that holds it."""
    for mod in _holders('nexus.gui', name):
        setattr(mod, name, value)
