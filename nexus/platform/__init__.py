"""The operating-system calls Nexus makes, behind one set of plain functions.
`linux.py` is what the code did inline before (behaviour unchanged);
`windows.py` is the Windows side. Shared modules call these and never import
fcntl, msvcrt or winreg themselves (tests/test_portability.py watches for it).

paths (each takes the home directory, so tests can point it elsewhere)
    data_dir(home), config_dir(home), cache_dir(home)
                                            Nexus's own folders: XDG-style on
                                            Linux; %LOCALAPPDATA%, %APPDATA%
                                            and %LOCALAPPDATA%\\...\\Cache on
                                            Windows
    steam_root(home)                        Steam's data folder
    steam_running_appid()                   the Steam game being played, from
                                            Steam's registry key on Windows;
                                            None on Linux (see steamlib.running)
    torrent_client_settings()               [(client, settings file)] to read
                                            for ports and remote access
                                            (Windows); [] on Linux
    app_menu_dir(home)                      where game launchers go: the
                                            freedesktop applications folder;
                                            Start Menu\\Programs\\<APP_NAME>
    default_install_base(home)              where games go by default:
                                            ~/Games/FitGirl; on Windows
                                            <roomiest non-system drive>:\\Games
    install_base_warnings(base) -> [str]    why `base` is a poor place for
                                            games (Windows: OneDrive, network,
                                            Program Files, long path); [] if fine
    APP_NAME                                'nexus' / 'nexus-launcher'
    OS_NAME                                 'linux' / 'windows' (what Nexus tells
                                            a server it runs on, e.g. Game-db)

processes
    processes()                             every process now, as records with
                                            .pid .name (Linux comm; Windows exe
                                            name without .exe) .cmdline (list)
                                            .exe .ppid and .environ() (Linux
                                            only; {} on Windows). Read lazily.
    new_group()                             Popen kwargs: the child leads its
                                            own process group
    spawn_detached(cmd, **popen_kwargs)     start and forget: own group, stdio
                                            on DEVNULL unless given
    kill_tree(proc, timeout=5)              stop a new_group() child and
                                            everything it started
    stop_process(pid, force=False) -> bool  ask one process to quit: SIGTERM
                                            (force: SIGKILL); taskkill (force:
                                            /F) on Windows

power (the quick menu's POWER rows)
    power_caps() -> set[str]                which of 'sleep', 'restart',
                                            'shutdown' this machine offers
                                            (logind's Can* answers; Windows'
                                            power capabilities); empty hides
                                            the section
    power(action) -> bool                   do one of them now, detached; False
                                            when it couldn't be started

console
    console_encoding()                      what other programs' output is in
                                            when it isn't UTF-8: 'utf-8'; the
                                            OEM code page (cp437...) on Windows

running our own CLI
    cli_command(repo, same_python)          argv for nexus-cli: bin/nexus-cli
                                            (behind sys.executable when
                                            same_python) on Linux; nexus-cli.exe
                                            frozen, or this Python on
                                            nexus/cli/__main__.py, on Windows

elevation (Windows: FitGirl's setup.exe needs admin)
    run_elevated(exe, args, cwd=None, quiet=False)
                                            start it through the UAC prompt;
                                            returns a Popen-like handle (pid,
                                            poll, wait, returncode, terminate
                                            -> bool), no pipes. Raises
                                            ElevationCancelled when the user
                                            declines. quiet (a silent install):
                                            nexus-cli is what's elevated,
                                            hidden, and runs it through
                                            quiet_setup. Linux:
                                            NotImplementedError
    quiet_setup(argv) -> exit code          that elevated nexus-cli
                                            (QUIET_SETUP_ARG cwd exe args...):
                                            runs exe with its music muted, its
                                            console windows hidden and web
                                            links going nowhere until it exits

antivirus and limits (Windows)
    defender_status() -> {realtime} | None  Windows Defender's state; None on
                                            Linux or when it can't be asked
    appx_registered(family)                 a Store (UWP) package is registered
                                            for this user (False on Linux)
    long_paths_enabled()                    paths past 260 characters work
                                            (always True on Linux)
    defender_detections(folders) -> [{file, threat, at}]
                                            what Windows Defender quarantined
                                            lately inside `folders`, read-only;
                                            [] on Linux or when it can't tell

urls
    open_url(url) -> bool                   hand a steam:// heroic:// http(s)
                                            URL to its app, detached; False
                                            when nothing can open it

memory
    meminfo()                               {MemTotal, MemAvailable, SwapTotal,
                                            SwapFree, ...} in kB; {} if unknown

shortcuts (Windows)
    create_shortcut(path, target, args='', workdir=None, icon=None)
                                            write a .lnk; Linux:
                                            NotImplementedError (.desktop
                                            files instead)

single instance (the window's QLocalServer)
    instance_address(cache_dir)             where it listens: a Unix socket in
                                            XDG_RUNTIME_DIR (else cache_dir/run);
                                            the pipe name nexus-<user> on Windows
    send_local(address, data, timeout)      connect and send; raises OSError
                                            when nobody listens

file replacement and privacy
    replace(src, dst)                       os.replace; on Windows retried a
                                            few times on PermissionError (an
                                            antivirus or indexer holding it)
    keep_private(path)                      only this user may read it:
                                            nothing on Linux (create it 0600);
                                            an owner-only ACL on Windows

copying
    copy_tree(src, dest)                    src's contents into dest, links as
                                            links: cp -a --reflink=auto; shutil
                                            on Windows. Raises OSError

trash
    trash(path)                             recoverable delete: gio or the
                                            freedesktop home trash; the
                                            Recycle Bin on Windows
    trash_size()                            bytes in the trash

discord and sleep
    discord_ipc_paths()                     where Discord's IPC may listen:
                                            the AF_UNIX sockets that exist;
                                            \\\\.\\pipe\\discord-ipc-0..9 on Windows
    inhibit_sleep(why)                      keep the PC awake until .release();
                                            None on Linux (presence's holder
                                            process does it there)

windows (Windows; Linux asks niri, see winfocus)
    top_windows() -> [{id, pid, title, exe, app_id, is_focused}]
                                            visible top-level windows; []
    focus_window(id) -> bool                bring one to the front; False

controllers
    sdl_library_paths()                     SDL2 libraries to try, in order
    sdl_hints() -> {name: value}            SDL hints set before SDL_Init

file locks
    lock(fh, shared=False, blocking=True)   lock an open file; with
                                            blocking=False a held lock raises
                                            BlockingIOError (an OSError)
    unlock(fh)
    file_lock(path, shared=False, blocking=True)
                                            context manager: opens `path` for
                                            append and holds the lock inside
"""

import contextlib
import re
import subprocess
import sys


class ElevationCancelled(OSError):
    """The user said no at the UAC prompt."""


# nexus-cli's first argument when it is the elevated half of a quiet install.
QUIET_SETUP_ARG = '--quiet-setup'


if sys.platform == 'win32':
    from . import windows as _impl
else:
    from . import linux as _impl

APP_NAME = _impl.APP_NAME
OS_NAME = _impl.OS_NAME
data_dir = _impl.data_dir
config_dir = _impl.config_dir
cache_dir = _impl.cache_dir
steam_root = _impl.steam_root
app_menu_dir = _impl.app_menu_dir
torrent_client_settings = _impl.torrent_client_settings
steam_running_appid = _impl.steam_running_appid
default_install_base = _impl.default_install_base
install_base_warnings = _impl.install_base_warnings
processes = _impl.processes
new_group = _impl.new_group
kill_tree = _impl.kill_tree
stop_process = _impl.stop_process
meminfo = _impl.meminfo
open_url = _impl.open_url
cli_command = _impl.cli_command
console_encoding = _impl.console_encoding
run_elevated = _impl.run_elevated
quiet_setup = _impl.quiet_setup
create_shortcut = _impl.create_shortcut
defender_detections = _impl.defender_detections
defender_status = _impl.defender_status
long_paths_enabled = _impl.long_paths_enabled
appx_registered = _impl.appx_registered
copy_tree = _impl.copy_tree
replace = _impl.replace
keep_private = _impl.keep_private
trash = _impl.trash
trash_size = _impl.trash_size
lock = _impl.lock
unlock = _impl.unlock
instance_address = _impl.instance_address
send_local = _impl.send_local
discord_ipc_paths = _impl.discord_ipc_paths
inhibit_sleep = _impl.inhibit_sleep
top_windows = _impl.top_windows
focus_window = _impl.focus_window
sdl_library_paths = _impl.sdl_library_paths
sdl_hints = _impl.sdl_hints
power_caps = _impl.power_caps
power = _impl.power


# the screen (Theatre's "On a TV" start)
#   screen_size_mm(x, y) -> (w, h) | None   the physical size of the monitor
#                                           at screen point (x, y), from its
#                                           EDID in the registry on Windows;
#                                           None on Linux (Qt's QScreen
#                                           physicalSize() has it there) or
#                                           when it can't be read
#   edid_size_mm(blob) -> (w, h) | None     the size an EDID block gives:
#                                           the first detailed timing
#                                           descriptor's mm, else the basic
#                                           cm bytes; None for a short blob or
#                                           no size
screen_size_mm = _impl.screen_size_mm


def edid_size_mm(blob: bytes) -> tuple[int, int] | None:
    """(width, height) in mm from a 128-byte (or longer) EDID: bytes 66-68
    of the first detailed timing descriptor (54-71; a zero pixel clock means
    it isn't one), else bytes 21-22 in cm. None when neither has a size."""
    if not blob or len(blob) < 128:
        return None
    if blob[54] or blob[55]:
        w = blob[66] | (blob[68] & 0xF0) << 4
        h = blob[67] | (blob[68] & 0x0F) << 8
        if w and h:
            return w, h
    if blob[21] and blob[22]:
        return blob[21] * 10, blob[22] * 10
    return None


def spawn_detached(cmd, **kw) -> subprocess.Popen:
    for stream in ('stdin', 'stdout', 'stderr'):
        kw.setdefault(stream, subprocess.DEVNULL)
    return subprocess.Popen(cmd, **new_group(), **kw)


@contextlib.contextmanager
def file_lock(path, shared=False, blocking=True):
    """Hold a lock on `path` (created if missing) for the block. Closing the
    file releases it, so an exception inside needs no cleanup."""
    with open(path, 'a') as fh:
        lock(fh, shared=shared, blocking=blocking)
        yield fh


# notifications and starting at sign-in
#   notify_local(title, body)               a desktop notification from a
#                                           process with no window:
#                                           notify-send; a PowerShell toast on
#                                           Windows. Raises OSError
#   startup_dir(home)                       the Startup folder (shell:startup)
#                                           for "Start with Windows"; None on
#                                           Linux (a systemd service instead)
#   gui_command(repo)                       argv that starts the Nexus window
#   fix_page(url)                           a page Health's fix buttons may
#                                           open (ms-settings:, windowsdefender:,
#                                           Doctor's help links)
notify_local = _impl.notify_local
startup_dir = _impl.startup_dir
gui_command = _impl.gui_command


# Pages a Doctor/Health fix may open: Windows' own Settings and Security
# pages and the help links Doctor cites. Fixes on Windows only ever open one
# of these (open_url); they never change a setting.
_FIX_PAGE_RE = re.compile(r'^(?:ms-settings:[a-z-]+|windowsdefender://[a-z]+'
                          r'|https://(?:learn\.microsoft\.com|fitgirl-repacks\.site)/[\w./?=#-]*)$')


def fix_page(url: str) -> bool:
    """True when `url` is one of the pages a fix button may open."""
    return bool(_FIX_PAGE_RE.match(url))


# stopping a GUI job (Windows; PyQt6 can't give a QProcess a process group,
# so no Ctrl+Break)
#   cancel_event() -> CancelEvent | None    a named event for one job: .name
#                                           (handed to the CLI in
#                                           NEXUS_CANCEL_EVENT), .set() -> bool,
#                                           .close(); None on Linux (SIGTERM)
#   watch_cancel(name, on_cancel) -> bool   CLI side: on_cancel() on a thread
#                                           once that event is set, even while
#                                           the main thread sleeps; False on
#                                           Linux or when there is no such event
cancel_event = _impl.cancel_event
watch_cancel = _impl.watch_cancel
CANCEL_EVENT_ENV = 'NEXUS_CANCEL_EVENT'


# the window frame
#   style_title_bar(hwnd, caption, text, border, dark) -> bool
#                                           the native title bar in the theme's
#                                           colours ('#rrggbb'; Windows 11, or
#                                           just dark/light on Windows 10);
#                                           False on Linux
#   TITLE_BAR_COLORS                        whether style_title_bar does
#                                           anything (ask before winId())
#   SQUARE_POPUP_SHADOW                     popups get a rectangular system
#                                           shadow (Windows' CS_DROPSHADOW),
#                                           wrong under rounded menus: turn it
#                                           off with NoDropShadowWindowHint
style_title_bar = _impl.style_title_bar
TITLE_BAR_COLORS = _impl.TITLE_BAR_COLORS
SQUARE_POPUP_SHADOW = _impl.SQUARE_POPUP_SHADOW
