"""Windows side of nexus.platform."""

import ctypes
import functools
import msvcrt
import os
import shutil
import subprocess
import sys
import time
import winreg
from ctypes import wintypes
from pathlib import Path

# The Windows edition's name: folder names, and later the exe and installer
# names. One constant so a rename is one line.
APP_NAME = 'nexus-launcher'


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

    def __init__(self, pid: int, exe_file: str):
        self.pid = pid
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
                out.append(Proc(e.th32ProcessID, e.szExeFile))
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
    (a non-elevated Nexus can't stop an elevated process)."""

    def __init__(self, handle):
        self._h = handle
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


def run_elevated(exe, args, cwd=None) -> Elevated:
    """Start `exe args` as administrator: the UAC prompt (none when we're
    elevated already). Raises ElevationCancelled when the user says no,
    OSError for anything else (missing exe, ...)."""
    from nexus.platform import ElevationCancelled
    info = _ShellExecuteInfo(
        cbSize=ctypes.sizeof(_ShellExecuteInfo),
        fMask=_SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC | _SEE_MASK_FLAG_NO_UI,
        lpVerb='runas', lpFile=str(exe), lpParameters=subprocess.list2cmdline(args),
        lpDirectory=str(cwd) if cwd else None, nShow=_SW_SHOWNORMAL)
    if not _shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == _ERROR_CANCELLED:
            raise ElevationCancelled(err, "cancelled at the admin prompt")
        raise ctypes.WinError(err)
    if not info.hProcess:            # handed to an already-running app: nothing to follow
        raise OSError(f"{exe} didn't start a process we can follow")
    return Elevated(info.hProcess)


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


# DwmSetWindowAttribute ids: dark caption buttons (Windows 10 2004+), then the
# border, caption and title text colours (Windows 11 only; Windows 10 refuses
# them and keeps its own bar, which is fine).
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_BORDER_COLOR, _DWMWA_CAPTION_COLOR, _DWMWA_TEXT_COLOR = 34, 35, 36


TITLE_BAR_COLORS = True


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
    put(_DWMWA_USE_IMMERSIVE_DARK_MODE, int(dark))
    return all([put(_DWMWA_CAPTION_COLOR, colorref(caption)),
                put(_DWMWA_TEXT_COLOR, colorref(text)),
                put(_DWMWA_BORDER_COLOR, colorref(border))])
