"""Windows side of nexus.platform."""

import contextlib
import ctypes
import functools
import json
import msvcrt
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import winreg
from ctypes import wintypes
from pathlib import Path

# The Windows edition's name: folder names, and later the exe and installer
# names. One constant so a rename is one line.
APP_NAME = 'nexus-launcher'
OS_NAME = 'windows'


def _known(var: str, home: Path, below_home: str) -> Path:
    """The user's %APPDATA% / %LOCALAPPDATA%; for any other home (tests, a
    sandbox) the same folder under that home."""
    if home == Path.home() and os.environ.get(var):
        return Path(os.environ[var])
    return home / below_home


def config_dir(home: Path) -> Path:
    return _known('APPDATA', home, 'AppData/Roaming') / APP_NAME


def data_dir(home: Path) -> Path:
    return _known('LOCALAPPDATA', home, 'AppData/Local') / APP_NAME


def cache_dir(home: Path) -> Path:
    return data_dir(home) / 'Cache'


def app_menu_dir(home: Path) -> Path:
    """Our folder in the user's Start Menu: games' shortcuts go here."""
    return (_known('APPDATA', home, 'AppData/Roaming') / 'Microsoft' / 'Windows'
            / 'Start Menu' / 'Programs' / APP_NAME)


def steam_root(home: Path) -> Path:
    """NEXUS_STEAM_ROOT when set (a portable Steam; the test sandbox), else
    where Steam's installer recorded itself (HKCU SteamPath), else the
    default install folder."""
    if os.environ.get('NEXUS_STEAM_ROOT'):
        return Path(os.environ['NEXUS_STEAM_ROOT'])
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
            return Path(winreg.QueryValueEx(key, 'SteamPath')[0])
    except OSError:
        return Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Steam'

_DRIVE_FIXED = 3


def _fixed_drives() -> list[str]:
    """'C:', 'D:', ... for every local fixed disk (no USB sticks, network or
    optical drives)."""
    buf = ctypes.create_unicode_buffer(512)
    n = _k32.GetLogicalDriveStringsW(len(buf), buf)
    roots = buf[:n].split('\0') if n else []
    return [r[:2] for r in roots if r and _k32.GetDriveTypeW(r) == _DRIVE_FIXED]


def default_install_base(home: Path) -> Path:
    """<the fixed drive with most free space, other than Windows'>:\\Games,
    else C:\\Games. Never Program Files: older games write saves next to
    their exe, which fails there. NEXUS_INSTALL_BASE overrides (the test
    sandbox, so tests never pick a real drive)."""
    if os.environ.get('NEXUS_INSTALL_BASE'):
        return Path(os.environ['NEXUS_INSTALL_BASE'])
    system = os.environ.get('SystemDrive', 'C:').upper()
    best, best_free = None, 0
    for drive in _fixed_drives():
        if drive.upper() == system:
            continue
        try:
            free = shutil.disk_usage(drive + '\\').free
        except OSError:
            continue
        if free > best_free:
            best, best_free = drive, free
    return Path((best or system) + '\\') / 'Games'


_DRIVE_REMOTE = 4


def install_base_warnings(base: Path) -> list[str]:
    """What's wrong with installing games under `base`, worst first: inside
    OneDrive (Documents and Desktop are often redirected there, and it syncs
    or offloads game files), a network drive, Program Files, or a path long
    enough that deep game folders pass the 260-character limit."""
    out = []
    text = str(base)
    low = text.lower()
    onedrive = [os.environ.get(v) for v in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial')]
    if any(d and (low == d.lower() or low.startswith(d.lower().rstrip('\\') + '\\'))
           for d in onedrive) or any(p.lower() == 'onedrive' or p.lower().startswith('onedrive - ')
                                     for p in Path(text).parts):     # "OneDrive - Company"
        out.append("it's inside OneDrive, which syncs or offloads game files")
    if text.startswith('\\\\') or (len(text) > 1 and text[1] == ':'
                                     and _k32.GetDriveTypeW(text[:2] + '\\') == _DRIVE_REMOTE):
        out.append("it's on a network drive")
    pf = [os.environ.get(v) for v in ('ProgramFiles', 'ProgramFiles(x86)')]
    if any(d and (low == d.lower() or low.startswith(d.lower() + '\\')) for d in pf):
        out.append("it's in Program Files, where older games can't write their saves")
    if len(text) > 60:
        out.append(f"the path is {len(text)} characters long; deep game folders can pass "
                   f"Windows' 260-character limit")
    return out


def torrent_client_settings() -> list[tuple[str, Path]]:
    """Where the torrent clients Nexus supports keep their settings here.
    Transmission 4 uses %LOCALAPPDATA%; older builds %APPDATA%."""
    local, roaming = os.environ.get('LOCALAPPDATA'), os.environ.get('APPDATA')
    out = []
    for base in filter(None, (local, roaming)):
        out.append(('transmission', Path(base) / 'transmission' / 'settings.json'))
    if roaming:
        out.append(('qbittorrent', Path(roaming) / 'qBittorrent' / 'qBittorrent.ini'))
    return out


def appx_registered(family: str) -> bool:
    """Whether a packaged (Store) app with this family name is registered
    for this user, so shell:AppsFolder can start it."""
    name = family.rsplit('_', 1)[0]
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                            '(Get-AppxPackage -Name $env:NX_APPX).PackageFamilyName'],
                           env={**os.environ, 'NX_APPX': name}, capture_output=True, text=True,
                           errors='replace', stdin=subprocess.DEVNULL, timeout=60,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return family.lower() in r.stdout.lower().split()


def long_paths_enabled() -> bool:
    """Whether paths past 260 characters work (HKLM ...\\FileSystem
    LongPathsEnabled; off by default, and many installers ignore it anyway)."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'SYSTEM\CurrentControlSet\Control\FileSystem') as key:
            return bool(winreg.QueryValueEx(key, 'LongPathsEnabled')[0])
    except OSError:
        return False


def defender_status() -> dict | None:
    """{'realtime': bool} from Windows Defender, or None when it can't be
    asked (another antivirus took over, PowerShell missing)."""
    import json
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                            'Get-MpComputerStatus | Select-Object AntivirusEnabled,'
                            'RealTimeProtectionEnabled | ConvertTo-Json -Compress'],
                           capture_output=True, text=True, errors='replace',
                           stdin=subprocess.DEVNULL, timeout=60,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        st = json.loads(r.stdout or 'null')
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(st, dict) or not st.get('AntivirusEnabled'):
        return None
    return {'realtime': bool(st.get('RealTimeProtectionEnabled'))}


def steam_running_appid() -> int | None:
    """The Steam game being played (HKCU ...\\Valve\\Steam RunningAppID;
    0 = none), or None."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
            appid = int(winreg.QueryValueEx(key, 'RunningAppID')[0])
    except (OSError, ValueError):
        return None
    return appid or None


_LOCKFILE_FAIL_IMMEDIATELY = 0x1
_LOCKFILE_EXCLUSIVE_LOCK = 0x2
_ERROR_LOCK_VIOLATION = 33
_ERROR_IO_PENDING = 997

# One byte far past any real end of file: a Windows byte-range lock is
# mandatory, so locking the first bytes could make another process's read of
# the file fail. Nothing ever reads these files, but this costs nothing.
_OFFSET = 0x7FFFFFFF


class _Overlapped(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_void_p), ('InternalHigh', ctypes.c_void_p),
                ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                ('hEvent', wintypes.HANDLE)]


_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_k32.GetLogicalDriveStringsW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
_k32.GetLogicalDriveStringsW.restype = wintypes.DWORD
_k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_k32.GetDriveTypeW.restype = wintypes.UINT
_k32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                            wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
_k32.LockFileEx.restype = wintypes.BOOL
_k32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, ctypes.POINTER(_Overlapped)]
_k32.UnlockFileEx.restype = wintypes.BOOL


def _handle(fh):
    return wintypes.HANDLE(msvcrt.get_osfhandle(fh.fileno()))


def lock(fh, shared=False, blocking=True):
    """LockFileEx: shared and non-blocking locks, which msvcrt.locking can't
    do. A held lock with blocking=False raises BlockingIOError, as flock does."""
    flags = 0 if shared else _LOCKFILE_EXCLUSIVE_LOCK
    if not blocking:
        flags |= _LOCKFILE_FAIL_IMMEDIATELY
    ov = _Overlapped(Offset=_OFFSET)
    if not _k32.LockFileEx(_handle(fh), flags, 0, 1, 0, ctypes.byref(ov)):
        err = ctypes.get_last_error()
        if err in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
            raise BlockingIOError(err, 'file is locked')
        raise ctypes.WinError(err)


def unlock(fh):
    ov = _Overlapped(Offset=_OFFSET)
    if not _k32.UnlockFileEx(_handle(fh), 0, 1, 0, ctypes.byref(ov)):
        raise ctypes.WinError(ctypes.get_last_error())


# Processes: a Toolhelp snapshot for pids and exe names (cheap), the full
# image path and command line only when asked for.
_TH32CS_SNAPPROCESS = 0x2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ProcessCommandLineInformation = 60          # Windows 8.1+
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004 - (1 << 32)


class _ProcessEntry(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_void_p),
                ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]


class _UnicodeString(ctypes.Structure):
    _fields_ = [('Length', wintypes.USHORT), ('MaximumLength', wintypes.USHORT),
                ('Buffer', ctypes.c_void_p)]


_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_ntdll = ctypes.WinDLL('ntdll')
_ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                             wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
_ntdll.NtQueryInformationProcess.restype = wintypes.LONG
_shell32 = ctypes.WinDLL('shell32', use_last_error=True)
_shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
_shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
_k32.LocalFree.argtypes = [ctypes.c_void_p]
_INVALID_HANDLE = wintypes.HANDLE(-1).value


class Proc:
    """One process. `name` is the exe's name without .exe (as Linux's comm
    is: 'steam', 'steamwebhelper'). `exe` and `cmdline` open the process
    on first use; a process that's gone, or protected, gives empty ones."""

    def __init__(self, pid: int, exe_file: str, ppid: int = 0):
        self.pid = pid
        self.ppid = ppid
        self.name = exe_file[:-4] if exe_file.lower().endswith('.exe') else exe_file

    def _open(self):
        return _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, self.pid)

    @functools.cached_property
    def exe(self) -> str:
        h = self._open()
        if not h:
            return ''
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            ok = _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            return buf.value if ok else ''
        finally:
            _k32.CloseHandle(h)

    @functools.cached_property
    def cmdline(self) -> list[str]:
        h = self._open()
        if not h:
            return []
        try:
            size = wintypes.ULONG(4096)
            for _ in range(3):
                buf = ctypes.create_string_buffer(size.value)
                st = _ntdll.NtQueryInformationProcess(h, _ProcessCommandLineInformation, buf,
                                                      size, ctypes.byref(size))
                if st != _STATUS_INFO_LENGTH_MISMATCH:
                    break
            if st != 0:
                return []
            us = _UnicodeString.from_buffer(buf)
            line = ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ''
        finally:
            _k32.CloseHandle(h)
        if not line:
            return []
        argc = ctypes.c_int()
        argv = _shell32.CommandLineToArgvW(line, ctypes.byref(argc))
        if not argv:
            return [line]
        try:
            return [argv[i] for i in range(argc.value)]
        finally:
            _k32.LocalFree(argv)

    def environ(self) -> dict[bytes, bytes]:
        """Not read on Windows (only Wine prefixes need it)."""
        return {}


def processes():
    snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return
    out = []
    try:
        e = _ProcessEntry(dwSize=ctypes.sizeof(_ProcessEntry))
        ok = _k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.th32ProcessID:                       # 0 = System Idle Process
                out.append(Proc(e.th32ProcessID, e.szExeFile, e.th32ParentProcessID))
            ok = _k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    yield from out


def new_group() -> dict:
    """Its own process group (Ctrl+C in our console doesn't reach it), and no
    console window of its own."""
    return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}


def kill_tree(proc, timeout=5):
    """There's no SIGTERM to ask nicely with, so taskkill /T /F: the child
    and everything it started."""
    if proc.poll() is not None:
        return
    subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def stop_process(pid: int, force: bool = False) -> bool:
    """Ask one process to quit: taskkill without /F sends its windows
    WM_CLOSE (there's no SIGTERM); with force, /F ends it. False when
    taskkill failed (gone, no window to close, or not ours)."""
    args = ['taskkill', *(['/F'] if force else []), '/PID', str(int(pid))]
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10,
                              creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# Recycle Bin.
_FO_DELETE = 0x3
_FOF_SILENT = 0x4
_FOF_NOCONFIRMATION = 0x10
_FOF_ALLOWUNDO = 0x40
_FOF_NOERRORUI = 0x400
# Too big for the Recycle Bin means deleted for good, and with NOCONFIRMATION
# that happens silently. This flag makes Windows ask first instead (a repack
# folder is easily over the Bin's limit).
_FOF_WANTNUKEWARNING = 0x4000


class _FileOp(ctypes.Structure):
    _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT),
                ('pFrom', wintypes.LPCWSTR), ('pTo', wintypes.LPCWSTR),
                ('fFlags', wintypes.WORD), ('fAnyOperationsAborted', wintypes.BOOL),
                ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wintypes.LPCWSTR)]


class _RecycleBinInfo(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('i64Size', ctypes.c_int64),
                ('i64NumItems', ctypes.c_int64)]


_shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_FileOp)]
_shell32.SHFileOperationW.restype = ctypes.c_int
_shell32.SHQueryRecycleBinW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_RecycleBinInfo)]
_shell32.SHQueryRecycleBinW.restype = ctypes.c_long


def trash(path: Path) -> None:
    """Into the Recycle Bin. Raises OSError when it didn't happen (including
    the user saying no to "too big for the Recycle Bin, delete for good?")."""
    src = str(Path(path).resolve())
    op = _FileOp(wFunc=_FO_DELETE, pFrom=src + '\0',     # a double-NUL-ended list
                 fFlags=_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT
                 | _FOF_NOERRORUI | _FOF_WANTNUKEWARNING)
    rc = _shell32.SHFileOperationW(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted:
        raise OSError(rc or 1223, f"couldn't move {src} to the Recycle Bin")  # 1223: cancelled


def trash_size() -> int:
    """Bytes in the Recycle Bins of all drives."""
    info = _RecycleBinInfo(cbSize=ctypes.sizeof(_RecycleBinInfo))
    if _shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return 0
    return info.i64Size


class _MemoryStatus(ctypes.Structure):
    _fields_ = [('dwLength', wintypes.DWORD), ('dwMemoryLoad', wintypes.DWORD),
                ('ullTotalPhys', ctypes.c_uint64), ('ullAvailPhys', ctypes.c_uint64),
                ('ullTotalPageFile', ctypes.c_uint64), ('ullAvailPageFile', ctypes.c_uint64),
                ('ullTotalVirtual', ctypes.c_uint64), ('ullAvailVirtual', ctypes.c_uint64),
                ('ullAvailExtendedVirtual', ctypes.c_uint64)]


_k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]


def meminfo() -> dict[str, int]:
    """The /proc/meminfo fields callers use, in kB. Windows' "page file"
    figures are the commit limit (RAM + page files), so swap is the
    difference."""
    st = _MemoryStatus(dwLength=ctypes.sizeof(_MemoryStatus))
    if not _k32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return {}
    kb = 1024
    return {'MemTotal': st.ullTotalPhys // kb, 'MemAvailable': st.ullAvailPhys // kb,
            'SwapTotal': max(0, st.ullTotalPageFile - st.ullTotalPhys) // kb,
            'SwapFree': max(0, st.ullAvailPageFile - st.ullAvailPhys) // kb}


def open_url(url: str) -> bool:
    """Whichever app registered the scheme: Steam, Heroic, Epic, GOG,
    Ubisoft, the browser."""
    try:
        os.startfile(url)
    except OSError:
        return False
    return True


def cli_command(repo: Path, same_python: bool) -> list[str]:
    """The bundled console exe next to ours when frozen; from source, this
    Python on nexus/cli/__main__.py (bin/nexus-cli has no use without a
    shebang). Always this interpreter, so same_python changes nothing."""
    if getattr(sys, 'frozen', False):
        return [str(Path(sys.executable).with_name('nexus-cli.exe'))]
    return [sys.executable, str(repo / 'nexus' / 'cli' / '__main__.py')]


# Elevated children: FitGirl's setup.exe needs admin, and Popen of it fails
# with error 740. ShellExecuteEx "runas" shows the UAC prompt and hands back a
# process handle, but no pipes: the caller follows progress some other way.
_SEE_MASK_NOCLOSEPROCESS = 0x40
_SEE_MASK_NOASYNC = 0x100
_SEE_MASK_FLAG_NO_UI = 0x400
_SW_HIDE = 0
_SW_SHOWNORMAL = 1
_ERROR_CANCELLED = 1223
_WAIT_TIMEOUT = 0x102
_INFINITE = 0xFFFFFFFF
_STILL_ACTIVE = 259
_PROCESS_TERMINATE = 0x1


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('fMask', wintypes.ULONG),
                ('hwnd', wintypes.HWND), ('lpVerb', wintypes.LPCWSTR),
                ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
                ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int),
                ('hInstApp', wintypes.HINSTANCE), ('lpIDList', ctypes.c_void_p),
                ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
                ('dwHotKey', wintypes.DWORD), ('hIconOrMonitor', wintypes.HANDLE),
                ('hProcess', wintypes.HANDLE)]


_shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
_shell32.ShellExecuteExW.restype = wintypes.BOOL
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.GetProcessId.argtypes = [wintypes.HANDLE]
_k32.GetProcessId.restype = wintypes.DWORD
_k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]


class Elevated:
    """An elevated child, Popen-shaped where it can be: pid, poll(),
    wait(timeout), returncode. terminate() returns False when we may not
    (a non-elevated Nexus can't stop an elevated process), and always for a
    quiet one: stopping that nexus-cli would leave setup running, and the
    browser setting it holds back with nobody left to put it back."""

    def __init__(self, handle, quiet=False):
        self._h = handle
        self._quiet = quiet
        self.pid = _k32.GetProcessId(handle)
        self.returncode = None

    def poll(self):
        if self.returncode is None and self._h:
            if _k32.WaitForSingleObject(self._h, 0) != _WAIT_TIMEOUT:
                self._finish()
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None and self._h:
            ms = _INFINITE if timeout is None else int(timeout * 1000)
            if _k32.WaitForSingleObject(self._h, ms) == _WAIT_TIMEOUT:
                raise subprocess.TimeoutExpired('elevated process', timeout)
            self._finish()
        return self.returncode

    def _finish(self):
        code = wintypes.DWORD()
        _k32.GetExitCodeProcess(self._h, ctypes.byref(code))
        self.returncode = code.value
        _k32.CloseHandle(self._h)
        self._h = None

    def terminate(self) -> bool:
        if self.poll() is not None:
            return True
        if self._quiet:
            return False
        h = _k32.OpenProcess(_PROCESS_TERMINATE, False, self.pid)
        if not h:
            return False
        try:
            return bool(_k32.TerminateProcess(h, 1))
        finally:
            _k32.CloseHandle(h)

    def __del__(self):
        if getattr(self, '_h', None):
            _k32.CloseHandle(self._h)


def run_elevated(exe, args, cwd=None, quiet=False) -> Elevated:
    """Start `exe args` as administrator: the UAC prompt (none when we're
    elevated already). Raises ElevationCancelled when the user says no,
    OSError for anything else (missing exe, ...). quiet: for a silent
    installer, what's elevated is nexus-cli, hidden, which runs `exe` with
    its music, console windows and closing web page kept away (quiet_setup);
    the handle is then nexus-cli's, and its exit code the installer's."""
    from nexus.platform import ElevationCancelled, QUIET_SETUP_ARG
    show = _SW_SHOWNORMAL
    if quiet:
        cli = cli_command(Path(__file__).resolve().parents[2], True)
        exe, args, show = cli[0], [*cli[1:], QUIET_SETUP_ARG, str(cwd or ''), str(exe),
                                   *args], _SW_HIDE
    info = _ShellExecuteInfo(
        cbSize=ctypes.sizeof(_ShellExecuteInfo),
        fMask=_SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC | _SEE_MASK_FLAG_NO_UI,
        lpVerb='runas', lpFile=str(exe), lpParameters=subprocess.list2cmdline(args),
        lpDirectory=str(cwd) if cwd else None, nShow=show)
    if not _shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == _ERROR_CANCELLED:
            raise ElevationCancelled(err, "cancelled at the admin prompt")
        raise ctypes.WinError(err)
    if not info.hProcess:            # handed to an already-running app: nothing to follow
        raise OSError(f"{exe} didn't start a process we can follow")
    return Elevated(info.hProcess, quiet)


# Shortcuts (.lnk) through WScript.Shell in one PowerShell call. The paths go
# in through the environment, so nothing in them needs escaping.
_LNK_SCRIPT = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:NX_LNK);"
               "$s.TargetPath = $env:NX_TARGET; $s.Arguments = $env:NX_ARGS;"
               "$s.WorkingDirectory = $env:NX_WORKDIR;"
               "if ($env:NX_ICON) { $s.IconLocation = $env:NX_ICON };"
               "$s.Save()")


def create_shortcut(path: Path, target: Path, args: str = '', workdir: Path | None = None,
                    icon: Path | None = None) -> None:
    """Write a .lnk at `path` (its folder is created). Raises OSError when
    PowerShell couldn't."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'NX_LNK': str(path), 'NX_TARGET': str(target), 'NX_ARGS': args,
           'NX_WORKDIR': str(workdir or Path(target).parent),
           'NX_ICON': f'{icon},0' if icon else ''}
    r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', _LNK_SCRIPT],
                       env=env, capture_output=True, text=True, errors='replace',
                       stdin=subprocess.DEVNULL, timeout=60,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0 or not path.is_file():
        raise OSError(f"couldn't write {path}: {(r.stderr or r.stdout).strip()[:300]}")


# Windows Defender's recent detections, read-only (Get-MpThreatDetection
# needs no admin). Each Resources entry looks like "file:_C:\\path\\x.dll".
_DEFENDER_SCRIPT = (
    "$t = @{}; Get-MpThreat -ErrorAction SilentlyContinue | "
    "ForEach-Object { $t[[string]$_.ThreatID] = $_.ThreatName };"
    "Get-MpThreatDetection -ErrorAction SilentlyContinue | "
    "Sort-Object InitialDetectionTime -Descending | Select-Object -First 20 | "
    "ForEach-Object { [pscustomobject]@{ threat = $t[[string]$_.ThreatID];"
    " at = $_.InitialDetectionTime.ToString('s'); resources = @($_.Resources) } } | "
    "ConvertTo-Json -Depth 3 -Compress")


def defender_detections(folders) -> list[dict]:
    """What Defender caught lately inside any of `folders`:
    [{'file', 'threat', 'at'}], newest first; [] when none, or when
    Defender can't be asked (another antivirus, PowerShell missing)."""
    import json
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                            _DEFENDER_SCRIPT], capture_output=True, text=True,
                           errors='replace', stdin=subprocess.DEVNULL, timeout=60,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        rows = json.loads(r.stdout or 'null') or []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    roots = [str(Path(f)).lower().rstrip('\\') + '\\' for f in folders]
    out = []
    for row in rows if isinstance(rows, list) else [rows]:
        for res in row.get('resources') or []:
            path = str(res).split('_', 1)[1] if str(res).lower().startswith('file:_') else str(res)
            if any(path.lower().startswith(r) for r in roots):
                out.append({'file': path, 'threat': row.get('threat') or 'unknown',
                            'at': row.get('at') or ''})
    return out


_k32.GetOEMCP.restype = wintypes.UINT


def console_encoding() -> str:
    """What Windows' console tools print in when not told otherwise."""
    return f'cp{_k32.GetOEMCP()}'


def copy_tree(src: Path, dest: Path) -> None:
    """Everything in src into dest (which may exist), links as links,
    timestamps kept. Raises OSError."""
    try:
        shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
    except shutil.Error as e:
        raise OSError(f"{len(e.args[0])} file(s) not copied, e.g. {e.args[0][0][0]}") from None


# Replacing a file that antivirus or the search indexer has open for a moment
# fails with a sharing violation (PermissionError); it clears within a second.
_REPLACE_RETRIES = (0.05, 0.2, 0.5)


def replace(src, dst) -> None:
    """os.replace, retried a few times on PermissionError."""
    import time
    for wait in _REPLACE_RETRIES:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(wait)
    os.replace(src, dst)


_TOKEN_QUERY = 0x8
_TokenUser = 1
_advapi = ctypes.WinDLL('advapi32', use_last_error=True)
_advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
_k32.GetCurrentProcess.restype = wintypes.HANDLE


@functools.cache
def _my_sid() -> str:
    """This user's SID ('S-1-5-21-...'): what icacls takes without a name
    lookup (a local account's USERDOMAIN is a workgroup, not a domain)."""
    token = wintypes.HANDLE()
    if not _advapi.OpenProcessToken(_k32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        _advapi.GetTokenInformation(token, _TokenUser, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not _advapi.GetTokenInformation(token, _TokenUser, buf, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.c_void_p.from_buffer(buf).value     # TOKEN_USER.User.Sid
        text = wintypes.LPWSTR()
        if not _advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            _k32.LocalFree(text)
    finally:
        _k32.CloseHandle(token)


def keep_private(path) -> None:
    """Only this user may read `path` (what 0600 means on Linux): its
    inherited permissions removed, full control for us. Best-effort: a
    failure leaves the default (the user's profile is private anyway)."""
    try:
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'*{_my_sid()}:F'],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass


# Single instance: QLocalServer listens on the named pipe \\.\pipe\<name>.
_ERROR_PIPE_BUSY = 231
_k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_k32.WaitNamedPipeW.restype = wintypes.BOOL


def instance_address(cache_dir: Path) -> str:
    """A pipe name, per user (pipes are machine-wide)."""
    return f"nexus-{os.environ.get('USERNAME') or 'user'}"


def send_local(address: str, data: bytes, timeout: float) -> None:
    """Open the QLocalServer's pipe and write data. Raises OSError (missing
    pipe: FileNotFoundError; every instance busy past `timeout`)."""
    path = address if address.startswith('\\\\') else '\\\\.\\pipe\\' + address
    end = time.monotonic() + timeout
    while True:
        try:
            with open(path, 'wb', buffering=0) as fh:
                if data:
                    fh.write(data)
            return
        except OSError as e:
            left = end - time.monotonic()
            if getattr(e, 'winerror', None) != _ERROR_PIPE_BUSY or left <= 0:
                raise
            _k32.WaitNamedPipeW(path, max(1, int(left * 1000)))


def discord_ipc_paths() -> list[str]:
    """Discord's IPC named pipes. Whether one is open is only learnt by
    opening it, so all ten names come back."""
    return [rf'\\.\pipe\discord-ipc-{i}' for i in range(10)]


_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x1
_ES_DISPLAY_REQUIRED = 0x2
_k32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
_k32.SetThreadExecutionState.restype = wintypes.DWORD


class _Awake:
    """Holds SetThreadExecutionState on its own thread (the state belongs to
    the thread that set it) until release(), or until this process exits."""

    def __init__(self):
        import threading
        self.returncode = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._hold, name='nexus-awake', daemon=True)
        self._thread.start()

    def _hold(self):
        _k32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED)
        self._done.wait()
        _k32.SetThreadExecutionState(_ES_CONTINUOUS)

    def release(self):
        self._done.set()
        self._thread.join(5)
        self.returncode = 0


def inhibit_sleep(why: str):
    """Keep the PC and its display awake until .release()."""
    return _Awake()


_powrprof = ctypes.WinDLL('powrprof')
_powrprof.CallNtPowerInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
                                             ctypes.c_void_p, ctypes.c_ulong]
_powrprof.CallNtPowerInformation.restype = ctypes.c_long
_powrprof.SetSuspendState.argtypes = [wintypes.BOOLEAN, wintypes.BOOLEAN, wintypes.BOOLEAN]
_powrprof.SetSuspendState.restype = wintypes.BOOLEAN
_SYSTEM_POWER_CAPABILITIES = 4          # CallNtPowerInformation level
_S3_OFFSET = 5                          # SYSTEM_POWER_CAPABILITIES.SystemS3


def power_caps() -> set[str]:
    """Restart and Shut down always; Sleep only with classic S3 sleep. Modern
    Standby PCs report no S3, and SetSuspendState isn't dependable there."""
    caps = {'restart', 'shutdown'}
    buf = ctypes.create_string_buffer(128)
    try:
        if _powrprof.CallNtPowerInformation(_SYSTEM_POWER_CAPABILITIES, None, 0, buf, 128) == 0 \
                and buf.raw[_S3_OFFSET]:
            caps.add('sleep')
    except OSError:
        pass
    return caps


def power(action: str) -> bool:
    """`shutdown /r|/s /t 0` with no console window; Sleep through
    SetSuspendState (rundll32 would hibernate) on a thread, since the call
    returns only after the PC wakes."""
    if action == 'sleep':
        threading.Thread(target=_powrprof.SetSuspendState, args=(False, False, False),
                         daemon=True).start()
        return True
    flag = {'restart': '/r', 'shutdown': '/s'}.get(action)
    if flag is None:
        return False
    try:
        subprocess.Popen(['shutdown', flag, '/t', '0'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        return False
    return True


_user32 = ctypes.WinDLL('user32', use_last_error=True)
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_k32.GetCurrentThreadId.restype = wintypes.DWORD
_GW_OWNER = 4
_SW_RESTORE = 9
_ASFW_ANY = 0xFFFFFFFF


def top_windows() -> list[dict]:
    """Visible, unowned top-level windows (what a taskbar shows), in
    winfocus's shape: id (the HWND), pid, title, exe (full path) and app_id
    (the exe's name, lower case, without .exe)."""
    found = []

    def each(hwnd, _lparam):
        if _user32.IsWindowVisible(hwnd) and not _user32.GetWindow(hwnd, _GW_OWNER):
            found.append(hwnd)
        return True
    _user32.EnumWindows(_WNDENUMPROC(each), 0)
    out, exes = [], {}
    focused = _user32.GetForegroundWindow()
    for hwnd in found:
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            continue
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in exes:
            exes[pid.value] = Proc(pid.value, '').exe
        exe = exes[pid.value]
        out.append({'id': int(hwnd), 'pid': pid.value, 'title': buf.value, 'exe': exe,
                    'app_id': Path(exe).stem.lower() if exe else '',
                    'is_focused': hwnd == focused})
    return out


def focus_window(window_id: int) -> bool:
    """Bring a top-level window to the front. Windows only lets the
    foreground app hand focus over, so this borrows the foreground thread's
    input state for the call (the usual way round it); restores it from the
    taskbar first if it's minimised."""
    hwnd = wintypes.HWND(int(window_id))
    _user32.AllowSetForegroundWindow(_ASFW_ANY)
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)
    mine = _k32.GetCurrentThreadId()
    theirs = _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), None)
    attached = bool(theirs and theirs != mine and _user32.AttachThreadInput(mine, theirs, True))
    try:
        _user32.BringWindowToTop(hwnd)
        return bool(_user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            _user32.AttachThreadInput(mine, theirs, False)


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD),
                ('szDevice', wintypes.WCHAR * 32)]


class _DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('DeviceName', wintypes.WCHAR * 32),
                ('DeviceString', wintypes.WCHAR * 128), ('StateFlags', wintypes.DWORD),
                ('DeviceID', wintypes.WCHAR * 128), ('DeviceKey', wintypes.WCHAR * 128)]


_user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
_user32.MonitorFromPoint.restype = wintypes.HANDLE
_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFOEXW)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_user32.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                        ctypes.POINTER(_DISPLAY_DEVICEW), wintypes.DWORD]
_user32.EnumDisplayDevicesW.restype = wintypes.BOOL
_MONITOR_DEFAULTTONEAREST = 2
_EDD_GET_DEVICE_INTERFACE_NAME = 1


def screen_size_mm(x: int, y: int) -> tuple[int, int] | None:
    """The monitor at (x, y): its display device (\\\\.\\DISPLAY1), that
    device's monitor interface (\\\\?\\DISPLAY#<model>#<instance>#{...}),
    then the EDID Windows keeps under Enum\\DISPLAY\\<model>\\<instance>.
    None when any step fails."""
    try:
        mon = _user32.MonitorFromPoint(wintypes.POINT(x, y), _MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not mon or not _user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return None
        dev = _DISPLAY_DEVICEW()
        dev.cb = ctypes.sizeof(dev)
        if not _user32.EnumDisplayDevicesW(info.szDevice, 0, ctypes.byref(dev),
                                           _EDD_GET_DEVICE_INTERFACE_NAME):
            return None
        parts = dev.DeviceID.split('#')
        if len(parts) < 3 or not parts[0].upper().endswith('DISPLAY'):
            return None
        key = '\\'.join((r'SYSTEM\CurrentControlSet\Enum\DISPLAY', parts[1], parts[2],
                         'Device Parameters'))
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            blob = winreg.QueryValueEx(k, 'EDID')[0]
    except OSError:
        return None
    from . import edid_size_mm
    return edid_size_mm(bytes(blob)) if isinstance(blob, (bytes, bytearray)) else None


def sdl_library_paths() -> list[str]:
    """SDL2.dll shipped beside nexus-launcher.exe (or beside python.exe when
    run from source), then whatever the DLL search path finds."""
    here = Path(sys.executable).resolve().parent
    return [str(here / 'SDL2.dll'), 'SDL2.dll']


def sdl_hints() -> dict[bytes, bytes]:
    """HIDAPI is how SDL reads PlayStation and Switch pads on Windows (there
    is no evdev). PS5 rumble off keeps a DualSense in its simple mode, so
    its lightbar and player LEDs are left alone."""
    return {b"SDL_JOYSTICK_HIDAPI": b"1", b"SDL_JOYSTICK_HIDAPI_PS5_RUMBLE": b"0"}


# A toast from a process without a window (the agent, the CLI): one short
# PowerShell, through the WinRT toast API under PowerShell's own
# AppUserModelID (Windows shows toasts only for a registered app id). Title
# and body go in through the environment, so nothing in them needs escaping.
_TOAST_SCRIPT = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
    " ContentType = WindowsRuntime] > $null;"
    "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$t = $x.GetElementsByTagName('text');"
    "$t.Item(0).AppendChild($x.CreateTextNode($env:NX_TITLE)) > $null;"
    "$t.Item(1).AppendChild($x.CreateTextNode($env:NX_BODY)) > $null;"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")


def notify_local(title: str, body: str) -> None:
    """Show a toast and return at once (PowerShell takes a second to start).
    Raises OSError when PowerShell can't be started."""
    env = {**os.environ, 'NX_TITLE': title, 'NX_BODY': body}
    subprocess.Popen(['powershell', '-NoProfile', '-NonInteractive', '-Command', _TOAST_SCRIPT],
                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)


def startup_dir(home: Path) -> Path:
    """The user's Startup folder (shell:startup): shortcuts here run at
    sign-in."""
    return (_known('APPDATA', home, 'AppData/Roaming') / 'Microsoft' / 'Windows'
            / 'Start Menu' / 'Programs' / 'Startup')


def gui_command(repo: Path) -> list[str]:
    """argv that starts the Nexus window: our exe when frozen; from source,
    pythonw (no console window) on nexus/gui/__main__.py."""
    if getattr(sys, 'frozen', False):
        return [str(Path(sys.executable).with_name(f'{APP_NAME}.exe'))]
    py = Path(sys.executable)
    pyw = py.with_name('pythonw.exe')
    return [str(pyw if pyw.is_file() else py), str(repo / 'nexus' / 'gui' / '__main__.py')]


# Stopping a GUI job. PyQt6 doesn't wrap QProcess's CreateProcess modifier, so
# a job can't have a process group of its own for Ctrl+Break, and terminate()
# posts WM_CLOSE, which a console CLI never sees. Instead the GUI makes a
# named event per job and the CLI waits on it in a thread (watch_cancel). The
# thread wakes at once, where a SIGBREAK handler waits for the main thread
# (stuck in a long sleep, say). A console window needs no help: QProcess
# already adds CREATE_NO_WINDOW when the GUI has no console.
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_k32.OpenEventW.restype = wintypes.HANDLE
_k32.SetEvent.argtypes = [wintypes.HANDLE]
_k32.SetEvent.restype = wintypes.BOOL


class CancelEvent:
    """A manual-reset event named Local\\nexus-cancel-<our pid>-<random>.
    Kept open until the job ends: while it is, a CLI that opens it late still
    finds it set."""

    def __init__(self):
        import secrets
        self.name = f'Local\\nexus-cancel-{os.getpid()}-{secrets.token_hex(8)}'
        self._h = _k32.CreateEventW(None, True, False, self.name)
        if not self._h:
            raise ctypes.WinError(ctypes.get_last_error())

    def set(self) -> bool:
        return bool(self._h) and bool(_k32.SetEvent(self._h))

    def close(self) -> None:
        if self._h:
            _k32.CloseHandle(self._h)
            self._h = None

    def __del__(self):
        self.close()


def cancel_event() -> CancelEvent | None:
    """A new CancelEvent for one job; None when Windows wouldn't make one."""
    try:
        return CancelEvent()
    except OSError:
        return None


def watch_cancel(name: str, on_cancel) -> bool:
    """Call on_cancel() on a daemon thread once the event `name` is set.
    False when there is no such event."""
    import threading
    h = _k32.OpenEventW(_SYNCHRONIZE, False, name)
    if not h:
        return False

    def wait():
        if _k32.WaitForSingleObject(h, _INFINITE) == _WAIT_OBJECT_0:
            on_cancel()
    threading.Thread(target=wait, name='nexus-cancel', daemon=True).start()
    return True


# DwmSetWindowAttribute ids: a dark title bar (20 since Windows 10 2004; 19
# on 1809-1909, which refuse 20), then the border, caption and title text
# colours (Windows 11 only; Windows 10 refuses them and keeps its own dark or
# light bar, which is fine).
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
_DWMWA_BORDER_COLOR, _DWMWA_CAPTION_COLOR, _DWMWA_TEXT_COLOR = 34, 35, 36


TITLE_BAR_COLORS = True
# Qt gives popup windows the CS_DROPSHADOW class style: a square shadow that
# shows around the rounded corners of a translucent menu.
SQUARE_POPUP_SHADOW = True


def style_title_bar(hwnd: int, caption: str, text: str, border: str, dark: bool) -> bool:
    """Paint the window's own title bar in the theme's colours ('#rrggbb').
    The bar stays the native one, so snapping and the buttons keep working.
    True when Windows took the colours."""
    try:
        dwm = ctypes.WinDLL('dwmapi')
    except OSError:
        return False

    def put(attr, value) -> bool:
        v = wintypes.DWORD(value)
        return dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(v),
                                         ctypes.sizeof(v)) == 0

    def colorref(hexcolor: str) -> int:     # COLORREF is 0x00bbggrr
        r, g, b = (int(hexcolor.lstrip('#')[i:i + 2], 16) for i in (0, 2, 4))
        return r | g << 8 | b << 16
    if not put(_DWMWA_USE_IMMERSIVE_DARK_MODE, int(dark)):
        put(_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, int(dark))
    return all([put(_DWMWA_CAPTION_COLOR, colorref(caption)),
                put(_DWMWA_TEXT_COLOR, colorref(text)),
                put(_DWMWA_BORDER_COLOR, colorref(border))])


# A silent install, kept quiet. FitGirl's setup plays music from its [Code]
# (BASS.dll, /VERYSILENT or not), and its [Run] section ends in postinstall
# entries that /VERYSILENT runs too, as no switch turns them off: a shellexec
# of her site, and `cmd.exe /c host.cmd` (hosts-file entries for the fake
# sites) in a console window for a few seconds. run_elevated(quiet=True)
# therefore elevates nexus-cli, hidden, and its quiet_setup() runs setup.exe
# at the same integrity level (a non-elevated Nexus may not touch an elevated
# setup's windows). There it:
#   - starts setup in a job object: "setup's processes" is then the job's own
#     list, whatever starts what
#   - mutes their audio sessions (the volume mixer's per-app ones), and only
#     theirs: Windows' volume and other apps are left alone
#   - hides their console windows as they appear; the commands still run
#   - points this user's http/https handler at a no-op until setup exits, and
#     makes new console windows conhost ones, not Windows Terminal tabs (those
#     belong to WindowsTerminal.exe, not to the job)
# The registry values are put back afterwards (_override); a run that died
# midway is undone by the next one.
_CREATE_SUSPENDED = 0x4
_JobObjectBasicProcessIdList = 3
_k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.AssignProcessToJobObject.restype = wintypes.BOOL
_k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_k32.QueryInformationJobObject.restype = wintypes.BOOL
_ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
_ntdll.NtResumeProcess.restype = wintypes.LONG


class _JobPids(ctypes.Structure):
    _fields_ = [('assigned', wintypes.DWORD), ('listed', wintypes.DWORD),
                ('pids', ctypes.c_size_t * 1024)]


def _job_pids(job) -> set[int]:
    info = _JobPids()
    if not _k32.QueryInformationJobObject(job, _JobObjectBasicProcessIdList, ctypes.byref(info),
                                          ctypes.sizeof(info), None):
        return set()
    return set(info.pids[:info.listed])


# The no-op that http/https links run while setup does: rundll32 with no
# arguments does nothing and shows nothing (a command without %1 isn't given
# the URL). Conhost's class id, for the default-terminal setting.
_NO_OP = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'rundll32.exe')
_CONHOST = '{B23D10C0-E52E-411E-9D5B-C09FDF709C7D}'
_URL_ASSOC = r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations'
_SAVED = 'NexusSaved'          # beside what an override changed: what was there before


def _url_progids() -> list[str]:
    """The ProgIds this user's http and https links open with: the default
    browser's (UserChoice; UserChoiceLatest on newer Windows 11), else the
    scheme's own key."""
    out = []
    for scheme in ('http', 'https'):
        found = []
        for sub in ('UserChoiceLatest', r'UserChoiceLatest\ProgId', 'UserChoice'):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    rf'{_URL_ASSOC}\{scheme}\{sub}') as key:
                    found.append(str(winreg.QueryValueEx(key, 'ProgId')[0]))
            except OSError:
                pass
        for progid in found or [scheme]:
            if progid and '\\' not in progid and progid not in out:
                out.append(progid)
    return out


def _quiet_overrides() -> list[tuple[str, dict]]:
    """(HKCU key, {value: data, or None to remove}) for a quiet setup."""
    return [(r'Console\%%Startup', {'DelegationConsole': _CONHOST, 'DelegationTerminal': _CONHOST}),
            *((rf'Software\Classes\{p}\shell\open\command', {'': _NO_OP, 'DelegateExecute': None})
              for p in _url_progids())]


def _to_json(data):
    return list(data) if isinstance(data, bytes) else data


def _from_json(data, kind):
    return bytes(data) if isinstance(data, list) and kind != winreg.REG_MULTI_SZ else data


def _override(path: str, values: dict) -> None:
    """Set `values` in HKCU\\path, first writing down beside them (as
    NexusSaved) what they replace and which keys had to be made."""
    _restore(path)                          # one left by a run that died first
    hkcu = winreg.HKEY_CURRENT_USER
    parts = path.split('\\')
    created = []
    for i in range(1, len(parts) + 1):
        sub = '\\'.join(parts[:i])
        try:
            winreg.OpenKey(hkcu, sub).Close()
        except OSError:
            created.append(sub)
    with winreg.CreateKeyEx(hkcu, path, 0, winreg.KEY_ALL_ACCESS) as key:
        before = {}
        for name in values:
            try:
                data, kind = winreg.QueryValueEx(key, name)
                before[name] = [_to_json(data), kind]
            except OSError:
                before[name] = None
        winreg.SetValueEx(key, _SAVED, 0, winreg.REG_SZ, json.dumps(
            {'before': before, 'set': values, 'created': created}))
        for name, data in values.items():
            if data is not None:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, data)
            elif before[name] is not None:
                winreg.DeleteValue(key, name)


def _restore(path: str) -> None:
    """Undo _override(path): each value it set goes back to what it was,
    unless something else has changed it since; the keys it made go when
    they're empty again. Nothing to do without a NexusSaved there."""
    hkcu = winreg.HKEY_CURRENT_USER
    try:
        key = winreg.OpenKey(hkcu, path, 0, winreg.KEY_ALL_ACCESS)
    except OSError:
        return
    with key:
        try:
            saved = json.loads(winreg.QueryValueEx(key, _SAVED)[0])
        except (OSError, ValueError, TypeError):
            return
        for name, ours in (saved.get('set') or {}).items():
            try:
                now = winreg.QueryValueEx(key, name)[0]
            except OSError:
                now = None
            if now != ours:
                continue                    # changed since (a browser update): theirs stays
            old = (saved.get('before') or {}).get(name)
            if old is not None:
                winreg.SetValueEx(key, name, 0, old[1], _from_json(*old))
            elif now is not None:
                winreg.DeleteValue(key, name)
        winreg.DeleteValue(key, _SAVED)
    for sub in reversed(saved.get('created') or []):
        if not path.startswith(sub):
            break
        try:
            with winreg.OpenKey(hkcu, sub) as key:
                subkeys, nvalues, _ = winreg.QueryInfoKey(key)
            if subkeys or nvalues:
                break
            winreg.DeleteKey(hkcu, sub)
        except OSError:
            break


@contextlib.contextmanager
def _overridden(overrides: list[tuple[str, dict]]):
    """The overrides for the block, each put back afterwards. One that can't
    be made is skipped: the install goes ahead regardless."""
    done = []
    try:
        for path, values in overrides:
            try:
                _override(path, values)
                done.append(path)
            except OSError:
                pass
        yield done
    finally:
        for path in reversed(done):
            try:
                _restore(path)
            except OSError:
                pass


# Core Audio through its COM interfaces by vtable slot (no comtypes): the
# device enumerator, each active output device's session manager, and per
# session its process id, state and mute.
_ole32 = ctypes.WinDLL('ole32')
_GUID = ctypes.c_ubyte * 16
_PP = ctypes.POINTER(ctypes.c_void_p)
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = ctypes.HRESULT
_ole32.CoUninitialize.argtypes = []
_ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(_GUID), _PP]
_ole32.CoCreateInstance.restype = ctypes.HRESULT
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_ALL = 0x17
_E_RENDER = 0
_DEVICE_STATE_ACTIVE = 0x1
_SESSION_EXPIRED = 2


def _guid(text: str):
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


_CLSID_MMDEVICE_ENUMERATOR = _guid('BCDE0395-E52F-467C-8E3D-C4579291692E')
_IID_IMMDEVICE_ENUMERATOR = _guid('A95664D2-9614-4F35-A746-DE8DB63617E6')
_IID_IAUDIO_SESSION_MANAGER2 = _guid('77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F')
_IID_IAUDIO_SESSION_CONTROL2 = _guid('BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D')
_IID_ISIMPLE_AUDIO_VOLUME = _guid('87CE5498-68D6-44E5-9215-6DA47EF883D8')


def _method(obj, slot: int, *argtypes, restype=ctypes.HRESULT):
    """Method number `slot` of COM object `obj`: a callable taking obj first.
    An HRESULT failure raises OSError."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[slot])


def _out(obj, slot: int, argtypes=(), args=()):
    """Call a method whose last parameter receives an interface; return it."""
    got = ctypes.c_void_p()
    _method(obj, slot, *argtypes, _PP)(obj, *args, ctypes.byref(got))
    return got


def _release(obj) -> None:
    if obj and obj.value:
        _method(obj, 2, restype=wintypes.ULONG)(obj)


def _audio_sessions() -> list[tuple[int, ctypes.c_void_p, ctypes.c_void_p]]:
    """(pid, IAudioSessionControl2, ISimpleAudioVolume) for every session on
    every active output device. The caller releases both interfaces."""
    found, held = [], []
    try:
        enum = ctypes.c_void_p()
        _ole32.CoCreateInstance(ctypes.byref(_CLSID_MMDEVICE_ENUMERATOR), None, _CLSCTX_ALL,
                                ctypes.byref(_IID_IMMDEVICE_ENUMERATOR), ctypes.byref(enum))
        held.append(enum)
        devices = _out(enum, 3, (ctypes.c_int, wintypes.DWORD), (_E_RENDER, _DEVICE_STATE_ACTIVE))
        held.append(devices)
        count = wintypes.UINT()
        _method(devices, 3, ctypes.POINTER(wintypes.UINT))(devices, ctypes.byref(count))
        for i in range(count.value):
            try:
                device = _out(devices, 4, (wintypes.UINT,), (i,))
                held.append(device)
                manager = _out(device, 3, (ctypes.POINTER(_GUID), wintypes.DWORD, ctypes.c_void_p),
                               (ctypes.byref(_IID_IAUDIO_SESSION_MANAGER2), _CLSCTX_ALL, None))
                held.append(manager)
                sessions = _out(manager, 5)
                held.append(sessions)
                n = ctypes.c_int()
                _method(sessions, 3, ctypes.POINTER(ctypes.c_int))(sessions, ctypes.byref(n))
            except OSError:
                continue
            for j in range(n.value):
                control = control2 = volume = None
                try:
                    control = _out(sessions, 4, (ctypes.c_int,), (j,))
                    control2 = _out(control, 0, (ctypes.POINTER(_GUID),),
                                    (ctypes.byref(_IID_IAUDIO_SESSION_CONTROL2),))
                    pid = wintypes.DWORD()
                    # S_OK, or AUDCLNT_S_NO_SINGLE_PROCESS (a success code) for
                    # a session several processes share: pid 0 then
                    _method(control2, 14, ctypes.POINTER(wintypes.DWORD))(control2,
                                                                          ctypes.byref(pid))
                    volume = _out(control, 0, (ctypes.POINTER(_GUID),),
                                  (ctypes.byref(_IID_ISIMPLE_AUDIO_VOLUME),))
                    found.append((pid.value, control2, volume))
                    control2 = volume = None
                except OSError:
                    pass
                finally:
                    for obj in (control, control2, volume):
                        _release(obj)
    except OSError:
        pass
    finally:
        for obj in reversed(held):
            _release(obj)
    return found


def _is_muted(volume) -> bool:
    muted = wintypes.BOOL()
    _method(volume, 6, ctypes.POINTER(wintypes.BOOL))(volume, ctypes.byref(muted))
    return bool(muted.value)


def _set_mute(volume, mute: bool) -> None:
    _method(volume, 5, wintypes.BOOL, ctypes.c_void_p)(volume, mute, None)


def _session_state(control) -> int:
    state = ctypes.c_int()
    _method(control, 3, ctypes.POINTER(ctypes.c_int))(control, ctypes.byref(state))
    return state.value


# Console windows of the job, hidden as they're shown: an out-of-context
# WinEvent hook (no DLL in anyone's process) on a thread with a message loop.
_WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                   wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
_user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                    _WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
_user32.SetWinEventHook.restype = wintypes.HANDLE
_user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
_user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
_user32.SetTimer.restype = ctypes.c_size_t
_user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                wintypes.UINT]
_user32.GetMessageW.restype = wintypes.BOOL
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM,
                                       wintypes.LPARAM]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.IsWindow.argtypes = [wintypes.HWND]
_EVENT_OBJECT_SHOW = 0x8002
_WINEVENT_SKIPOWNPROCESS = 0x2
_OBJID_WINDOW = 0
_WM_QUIT = 0x12
_WM_TIMER = 0x113
_SW_SHOWNOACTIVATE = 4
_TICK_MS = 100
_FAST_TICKS = 600           # the first minute every tick (the music starts early), then every 5th
_STUCK_AFTER = 120          # a console hidden this long may be waiting for a key: shown again


class _Quiet:
    """Mutes and hides what the processes in `job` play and show, on its
    own thread, until stop(). Sessions it muted that still play then (a
    browser setup started, say) get their sound back."""

    def __init__(self, job):
        self._job = job
        self._muted = []                    # (IAudioSessionControl2, ISimpleAudioVolume)
        self._hidden = {}                   # console hwnd -> when hidden
        self._tid = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name='nexus-quiet', daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _run(self):
        try:
            _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
            com = True
        except OSError:
            com = False
        on_show = _WINEVENTPROC(self._on_show)          # referenced until unhooked
        hook = _user32.SetWinEventHook(_EVENT_OBJECT_SHOW, _EVENT_OBJECT_SHOW, None, on_show,
                                       0, 0, _WINEVENT_SKIPOWNPROCESS)
        timer = _user32.SetTimer(None, 0, _TICK_MS, None)
        self._tid = _k32.GetCurrentThreadId()
        self._ready.set()
        msg, ticks = wintypes.MSG(), 0
        try:
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message != _WM_TIMER:
                    continue
                ticks += 1
                if ticks <= _FAST_TICKS or ticks % 5 == 0:
                    self._mute()
                self._show_stuck()
        finally:
            if hook:
                _user32.UnhookWinEvent(hook)
            if timer:
                _user32.KillTimer(None, timer)
            self._unmute()
            if com:
                _ole32.CoUninitialize()

    def _mute(self):
        pids = _job_pids(self._job)
        for pid, control, volume in _audio_sessions():
            kept = False
            try:
                if pid and pid in pids and not _is_muted(volume):
                    _set_mute(volume, True)
                    self._muted.append((control, volume))
                    kept = True
            except OSError:
                pass
            if not kept:
                _release(control)
                _release(volume)

    def _unmute(self):
        for control, volume in self._muted:
            try:
                if _session_state(control) != _SESSION_EXPIRED:
                    _set_mute(volume, False)
            except OSError:
                pass
            _release(control)
            _release(volume)
        self._muted = []

    def _on_show(self, _hook, _event, hwnd, id_object, id_child, _thread, _time):
        try:
            if not hwnd or id_object != _OBJID_WINDOW or id_child != 0:
                return
            name = ctypes.create_unicode_buffer(64)
            _user32.GetClassNameW(hwnd, name, 64)
            if name.value != 'ConsoleWindowClass':
                return
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in _job_pids(self._job):
                _user32.ShowWindow(hwnd, _SW_HIDE)
                self._hidden[hwnd] = time.monotonic()
        except Exception:                   # noqa: BLE001 — never into ctypes' callback
            pass

    def _show_stuck(self):
        now = time.monotonic()
        for hwnd, since in list(self._hidden.items()):
            if not _user32.IsWindow(hwnd):
                del self._hidden[hwnd]
            elif now - since > _STUCK_AFTER:
                _user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
                del self._hidden[hwnd]

    def stop(self):
        if self._tid:
            _user32.PostThreadMessageW(self._tid, _WM_QUIT, 0, 0)
        self._thread.join(5)


def quiet_setup(argv: list[str]) -> int:
    """The elevated half of run_elevated(quiet=True), in nexus-cli: argv is
    [cwd, exe, *args]. Runs exe in a job object with the overrides above
    and _Quiet watching, and returns its exit code (1 when it couldn't be
    started). Whatever part of the quieting fails, the setup still runs."""
    cwd, exe, *args = argv
    with _overridden(_quiet_overrides()):
        job = _k32.CreateJobObjectW(None, None)
        try:
            proc = subprocess.Popen([exe, *args], cwd=cwd or None,
                                    creationflags=_CREATE_SUSPENDED)
        except OSError:
            if job:
                _k32.CloseHandle(job)
            return 1
        quiet = None
        if job and _k32.AssignProcessToJobObject(job, int(proc._handle)):
            quiet = _Quiet(job)
        _ntdll.NtResumeProcess(int(proc._handle))
        try:
            return proc.wait()
        finally:
            if quiet:
                quiet.stop()
            if job:
                _k32.CloseHandle(job)
