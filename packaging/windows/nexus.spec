# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows edition: one folder, two exes.

    python packaging/windows/fetch_sdl2.py           # once: vendor/SDL2.dll
    pyinstaller --noconfirm packaging/windows/nexus.spec

builds (relative to the current folder)

    dist/<APP_NAME>/<APP_NAME>.exe     the GUI (windowed)
    dist/<APP_NAME>/nexus-cli.exe      the CLI (console); the GUI runs it as
                                       [*CLI, ...] (platform.cli_command)
    dist/<APP_NAME>/_internal/         shared: Python, Qt, assets/, SDL2.dll

One folder, not one file: one-file unpacks itself to %TEMP% on every run,
which costs seconds per CLI job and trips more antivirus heuristics.

Frozen layout as the code sees it: nexus/ lives in _internal, so paths.REPO
and gui.common._REPO (parents of the package) are _internal, where
assets/ and nexus/data/ are collected. SDL2.dll sits at the top of
_internal, where PyInstaller's ctypes.util.find_library looks first.
"""
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[1]
HERE = ROOT / 'packaging' / 'windows'
IS_WIN = sys.platform == 'win32'


def _read(pattern: str, path: Path) -> str:
    m = re.search(pattern, path.read_text(encoding='utf-8'), re.M)
    if not m:
        raise SystemExit(f"nexus.spec: no match for {pattern!r} in {path}")
    return m.group(1)


# One source each: the exe/folder name is platform.windows.APP_NAME (read,
# not imported: that module needs winreg), the version nexus.__version__.
APP_NAME = _read(r"^APP_NAME\s*=\s*['\"]([^'\"]+)['\"]", ROOT / 'nexus' / 'platform' / 'windows.py')
VERSION = _read(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", ROOT / 'nexus' / '__init__.py')
CLI_NAME = 'nexus-cli'          # platform.windows.cli_command expects nexus-cli.exe

# ---- icon ------------------------------------------------------------------
ICON = Path(os.environ.get('NEXUS_ICON') or Path(workpath) / f'{APP_NAME}.ico')
if not ICON.exists():
    subprocess.run([sys.executable, str(HERE / 'make_icon.py'), str(ICON)], check=True)

# ---- SDL2.dll (controller input, nexus/gamepad.py) -------------------------
SDL2 = Path(os.environ.get('NEXUS_SDL2_DLL') or HERE / 'vendor' / 'SDL2.dll')
binaries = []
if SDL2.exists():
    binaries.append((str(SDL2), '.'))
    if (SDL2.parent / 'README-SDL.txt').exists():           # its zlib licence note
        binaries.append((str(SDL2.parent / 'README-SDL.txt'), '.'))
elif IS_WIN and not os.environ.get('NEXUS_ALLOW_NO_SDL2'):
    raise SystemExit(f"nexus.spec: {SDL2} is missing; run packaging/windows/fetch_sdl2.py "
                     f"(or set NEXUS_ALLOW_NO_SDL2=1 for a build without controller support)")
else:
    print(f"nexus.spec: WARNING: {SDL2} missing, building without SDL2", file=sys.stderr)

datas = [
    (str(ROOT / 'assets'), 'assets'),                 # logo, wordmark, fonts/lucide.ttf
    (str(ROOT / 'nexus' / 'data'), 'nexus/data'),     # bundled recipes.toml
    (str(ROOT / 'LICENSE'), '.'),
]

# ---- what to leave out -----------------------------------------------------
# Qt modules Nexus never imports. PyInstaller only collects what is imported,
# so these mostly guard against a stray import pulling in hundreds of MB
# (QtWebEngine alone is ~200 MB). QtMultimedia(+Widgets) stays: trailers.
QT_EXCLUDES = [f'PyQt6.{m}' for m in (
    'QtWebEngineCore', 'QtWebEngineWidgets', 'QtWebEngineQuick', 'QtWebChannel',
    'QtWebSockets', 'QtQml', 'QtQuick', 'QtQuick3D', 'QtQuickWidgets', 'QtDesigner',
    'QtHelp', 'QtPdf', 'QtPdfWidgets', 'QtSql', 'QtTest', 'QtBluetooth', 'QtNfc',
    'QtPositioning', 'QtSensors', 'QtSerialPort', 'QtRemoteObjects', 'QtSpatialAudio',
    'QtTextToSpeech', 'QtXml', 'QtDBus', 'Qt3DCore', 'Qt3DRender', 'Qt3DInput',
    'Qt3DLogic', 'Qt3DAnimation', 'Qt3DExtras', 'QtCharts', 'QtDataVisualization',
    'QtOpenGL', 'QtOpenGLWidgets', 'QtPrintSupport', 'QtStateMachine', 'uic',
)]
PY_EXCLUDES = ['tkinter', '_tkinter', 'test', 'unittest', 'pydoc_data', 'lib2to3',
               'idlelib', 'ensurepip', 'venv',
               # the other platform's half of nexus.platform
               'nexus.platform.linux' if IS_WIN else 'nexus.platform.windows']

# Qt files that come in as dependencies but that nothing here loads. Matched
# against the destination path, lower-case, with forward slashes.
DROP = re.compile(r'(qt6(webengine|quick|qml|pdf|designer|3d|charts|datavis|virtualkeyboard)'
                  r'|/qml/|/translations/qtwebengine|/plugins/(sqldrivers|designer|qmltooling'
                  r'|virtualkeyboard|webview|sceneparsers|renderers|geometryloaders'
                  r'|position|sensors|texttospeech|canbus))')


def _trim(toc):
    return [e for e in toc if not DROP.search('/' + e[0].replace('\\', '/').lower())]


def _analysis(script):
    a = Analysis(
        [str(script)],
        pathex=[str(ROOT)],
        binaries=binaries,
        datas=datas,
        hiddenimports=['tomllib'],          # also reached through __import__('tomllib')
        excludes=QT_EXCLUDES + PY_EXCLUDES,
        noarchive=False,
        optimize=0,                         # keep asserts and docstrings (argparse help)
    )
    a.binaries = _trim(a.binaries)
    a.datas = _trim(a.datas)
    return a


# ---- Windows version resource ----------------------------------------------
def _version_info(desc, exe):
    if not IS_WIN:
        return None
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct,
        VSVersionInfo)
    nums = tuple((list(map(int, re.findall(r'\d+', VERSION)[:4])) + [0, 0, 0, 0])[:4])
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[StringFileInfo([StringTable('040904B0', [
                  StringStruct('CompanyName', 'Nexus'),
                  StringStruct('FileDescription', desc),
                  StringStruct('FileVersion', VERSION),
                  StringStruct('InternalName', exe),
                  StringStruct('OriginalFilename', exe + '.exe'),
                  StringStruct('ProductName', 'Nexus'),
                  StringStruct('ProductVersion', VERSION)])]),
              VarFileInfo([VarStruct('Translation', [1033, 1200])])])


# -X utf8: UTF-8 mode, as the source build gets from PYTHONUTF8=1. open(),
# pipes and os.fsencode default to UTF-8 instead of the ANSI code page.
OPTIONS = [('X utf8', None, 'OPTION')]

gui = _analysis(HERE / 'gui_entry.py')          # nexus.gui.main()
cli = _analysis(HERE / 'cli_entry.py')          # nexus.cli.main()

gui_exe = EXE(
    PYZ(gui.pure), gui.scripts, OPTIONS,
    exclude_binaries=True,
    name=APP_NAME,
    console=False,                          # windowed: no console behind the GUI
    icon=str(ICON),
    version=_version_info('Nexus', APP_NAME),
    upx=False,                              # UPX'd Qt DLLs break and look like malware
)
cli_exe = EXE(
    PYZ(cli.pure), cli.scripts, OPTIONS,
    exclude_binaries=True,
    name=CLI_NAME,
    console=True,
    icon=str(ICON),
    version=_version_info('Nexus command line', CLI_NAME),
    upx=False,
)
COLLECT(
    gui_exe, gui.binaries, gui.datas,
    cli_exe, cli.binaries, cli.datas,
    name=APP_NAME,
    upx=False,
)
