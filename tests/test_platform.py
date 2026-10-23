"""nexus.platform: the file lock (exclusive, shared and non-blocking behave
the same on Linux's flock and Windows' LockFileEx), Nexus's folders, the
process list, groups and kill_tree, and the Windows-only pieces (Steam's
registry path, the Recycle Bin, the frozen CLI). nexus.features: the set and its
NEXUS_FEATURES override."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

try:
    import sandbox  # noqa: F401
    from oskind import linux_only, windows_only
except ImportError:             # run as tests.<module>
    from tests import sandbox  # noqa: F401
    from tests.oskind import linux_only, windows_only
from nexus import features, platform


class FileLock(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / 'x.lock'

    def test_exclusive_blocks_second_holder_until_released(self):
        with platform.file_lock(self.path):
            with open(self.path, 'a') as other:
                with self.assertRaises(BlockingIOError):
                    platform.lock(other, blocking=False)
        with platform.file_lock(self.path, blocking=False):
            pass                                    # free again

    def test_shared_locks_coexist_but_exclude_exclusive(self):
        with platform.file_lock(self.path, shared=True):
            with platform.file_lock(self.path, shared=True, blocking=False):
                pass
            with open(self.path, 'a') as other:
                with self.assertRaises(BlockingIOError):
                    platform.lock(other, blocking=False)

    def test_unlock_releases_without_closing(self):
        with open(self.path, 'a') as a, open(self.path, 'a') as b:
            platform.lock(a)
            platform.unlock(a)
            platform.lock(b, blocking=False)

    def test_lock_file_is_created(self):
        with platform.file_lock(self.path):
            self.assertTrue(self.path.exists())


class Paths(unittest.TestCase):
    @linux_only
    def test_linux_dirs_are_unchanged(self):
        home = Path('/h')
        self.assertEqual(platform.data_dir(home), home / '.local/share/nexus')
        self.assertEqual(platform.config_dir(home), home / '.config/nexus')

    @windows_only
    def test_windows_dirs_follow_appdata(self):
        home = Path(sandbox.HOME)
        self.assertEqual(platform.config_dir(home),
                         home / 'AppData' / 'Roaming' / 'nexus-launcher')
        self.assertEqual(platform.data_dir(home),
                         home / 'AppData' / 'Local' / 'nexus-launcher')
        self.assertEqual(platform.cache_dir(home), platform.data_dir(home) / 'Cache')
        self.assertTrue(platform.steam_root(home).name.lower() == 'steam')


class Processes(unittest.TestCase):
    def test_finds_this_process(self):
        me = next(p for p in platform.processes() if p.pid == os.getpid())
        self.assertTrue(me.name.lower().startswith('python'), me.name)
        self.assertTrue(any('unittest' in a or 'test_platform' in a for a in me.cmdline),
                        me.cmdline)
        # A Windows venv's python.exe is a launcher: the process running is
        # the base interpreter it started.
        self.assertIn(Path(me.exe).resolve(),
                      {Path(sys.executable).resolve(),
                       Path(getattr(sys, '_base_executable', sys.executable)).resolve()})

    def test_a_name_scan_stays_cheap(self):
        # gui/config.steam_running runs this every few seconds on the GUI
        # thread: names only, no cmdline/exe reads. A few ms on a desktop;
        # the bound is loose for slow CI runners.
        t = time.perf_counter()
        any(p.name == 'no-such-process' for p in platform.processes())
        self.assertLess(time.perf_counter() - t, 0.25)

    def test_kill_tree_stops_the_child_and_what_it_started(self):
        # The child starts a grandchild and prints its pid; both must go.
        code = ("import subprocess, sys, time;"
                "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
                "print(g.pid, flush=True); time.sleep(60)")
        proc = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                                text=True, **platform.new_group())
        grandchild = int(proc.stdout.readline())
        platform.kill_tree(proc)
        self.assertIsNotNone(proc.poll())
        for _ in range(50):
            if all(p.pid != grandchild for p in platform.processes()):
                break
            time.sleep(0.1)
        else:
            self.fail("grandchild survived")
        proc.stdout.close()

    def test_spawn_detached_returns_a_running_child(self):
        proc = platform.spawn_detached([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            self.assertIsNone(proc.poll())
        finally:
            platform.kill_tree(proc)


class CliCommand(unittest.TestCase):
    def test_runs_the_cli_from_anywhere(self):
        from nexus import paths
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([*paths.cli_command(same_python=True), '--help'], cwd=d,
                               capture_output=True, text=True, timeout=60,
                               env={k: v for k, v in os.environ.items()
                                    if k != 'NEXUS_DEV_WINDOWS'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('usage: nexus-cli', r.stdout)

    @linux_only
    def test_linux_is_the_script(self):
        from nexus import paths
        self.assertEqual(paths.cli_command(), [str(paths.REPO / 'bin' / 'nexus-cli')])


@windows_only
class WindowsOnly(unittest.TestCase):
    """The Windows pieces the shared tests above don't reach."""

    def test_steam_root_env_then_registry_then_default(self):
        win = platform._impl
        with mock.patch.dict(os.environ, {'NEXUS_STEAM_ROOT': r'D:\PortableSteam'}):
            self.assertEqual(win.steam_root(Path.home()), Path(r'D:\PortableSteam'))
        env = {k: v for k, v in os.environ.items() if k != 'NEXUS_STEAM_ROOT'}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(win.winreg, 'OpenKey', side_effect=OSError):
            self.assertEqual(win.steam_root(Path.home()),
                             Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'))
                             / 'Steam')

    def test_trash_goes_to_the_recycle_bin(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'nexus-trash-test.txt'
            f.write_bytes(b'x' * 4096)
            before = platform.trash_size()
            platform.trash(f)
            self.assertFalse(f.exists())
            self.assertGreaterEqual(platform.trash_size(), before + 4096)

    def test_trash_of_nothing_raises(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(OSError):
            platform.trash(Path(d) / 'missing')

    def test_frozen_cli_is_the_console_exe_beside_ours(self):
        from nexus import paths
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, 'executable', r'C:\Apps\nexus-launcher\nexus.exe'):
            self.assertEqual(paths.cli_command(),
                             [r'C:\Apps\nexus-launcher\nexus-cli.exe'])

    def test_source_cli_is_this_python_on_the_package_main(self):
        from nexus import paths
        cmd = paths.cli_command()
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(Path(cmd[1]), paths.REPO / 'nexus' / 'cli' / '__main__.py')

    def test_elevated_child_runs_in_cwd_with_args_and_exit_code(self):
        # CI runners and the VM's ssh session are elevated already, so no
        # prompt shows; declining it is checked by hand (p2-15).
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'out.txt'
            code = 'import os,sys; open(sys.argv[1],"w").write(os.getcwd()+"|"+sys.argv[2]); sys.exit(7)'
            p = platform.run_elevated(sys.executable, ['-c', code, str(out), 'a "q" b'], cwd=d)
            self.assertEqual(p.wait(30), 7)
            self.assertEqual(out.read_text(), f'{d}|a "q" b')

    def test_declined_prompt_is_its_own_error(self):
        import ctypes
        win = platform._impl

        def declined(_info):
            ctypes.set_last_error(1223)
            return 0
        with mock.patch.object(win._shell32, 'ShellExecuteExW', declined), \
                self.assertRaises(platform.ElevationCancelled):
            platform.run_elevated(sys.executable, [])

    def test_elevated_missing_exe_raises(self):
        with self.assertRaises(FileNotFoundError):
            platform.run_elevated(r'C:\nope\missing.exe', [])

    def test_default_install_base_is_never_program_files(self):
        env = {k: v for k, v in os.environ.items() if k != 'NEXUS_INSTALL_BASE'}
        with mock.patch.dict(os.environ, env, clear=True):
            base = platform.default_install_base(Path.home())
        self.assertEqual(base.name, 'Games')
        self.assertEqual(len(base.parent.parts), 1, base)          # a drive root
        self.assertEqual(platform.install_base_warnings(base), [])

    def test_install_base_warnings(self):
        warn = platform.install_base_warnings
        with mock.patch.dict(os.environ, {'OneDrive': r'C:\Users\me\OneDrive'}):
            self.assertIn('OneDrive', warn(Path(r'C:\Users\me\OneDrive\Games'))[0])
            self.assertEqual(warn(Path(r'C:\Users\me\OneDriveNot\Games')), [])
        self.assertIn('network', warn(Path(r'\\nas\share\Games'))[0])
        self.assertIn('Program Files',
                      warn(Path(os.environ['ProgramFiles']) / 'Games')[0])
        self.assertIn('260', warn(Path('C:\\' + 'x' * 70))[0])

    def test_a_process_that_is_gone_reads_empty(self):
        gone = platform._impl.Proc(0x7FFFFFF0, 'ghost.exe')
        self.assertEqual((gone.name, gone.exe, gone.cmdline, gone.environ()),
                         ('ghost', '', [], {}))


class Memory(unittest.TestCase):
    def test_meminfo_has_what_the_memory_check_reads(self):
        m = platform.meminfo()
        for k in ('MemTotal', 'MemAvailable', 'SwapFree'):
            self.assertIn(k, m)
        self.assertGreater(m['MemTotal'], 256 * 1024)                 # kB
        self.assertLessEqual(m['MemAvailable'], m['MemTotal'])


class Features(unittest.TestCase):
    def test_override_turns_features_off_and_on(self):
        got = features._parse(features.ALL, '-proton, -perf,,')
        self.assertNotIn('proton', got)
        self.assertNotIn('perf', got)
        self.assertIn('umu', got)
        self.assertEqual(features._parse(frozenset(), '+proton,umu'), {'proton', 'umu'})

    def test_this_os(self):
        import sys
        self.assertEqual(features.has('proton'), sys.platform != 'win32')

    def test_pause_says_so_when_off(self):
        from nexus.cli import running
        with mock.patch.object(features, 'FEATURES', frozenset()), \
                mock.patch.object(running, 'running_repacks') as scan:
            with self.assertRaises(SystemExit):
                running.cmd_pause('x', 'pause')
        scan.assert_not_called()

    def test_unknown_name_is_a_bug(self):
        with self.assertRaises(KeyError):
            features.has('protn')


class NotifyAndStartup(unittest.TestCase):
    """notify_local, the Startup folder, the GUI's argv and the pages a fix
    button may open."""

    def test_fix_pages_are_windows_settings_and_help_links_only(self):
        for url in ('ms-settings:storagesense', 'windowsdefender://threatsettings',
                    'https://learn.microsoft.com/windows/win32/fileio/'
                    'maximum-file-path-limitation', 'https://fitgirl-repacks.site/faq/'):
            self.assertTrue(platform.fix_page(url), url)
        for url in ('https://example.com/', 'file:///C:/Windows/System32/cmd.exe',
                    'ms-settings:storagesense & calc', 'steam://rungameid/1',
                    'http://learn.microsoft.com/', 'https://learn.microsoft.com.evil.io/x',
                    'windowsdefender://x/../y', ''):
            self.assertFalse(platform.fix_page(url), url)

    def test_doctor_fix_pages_open_from_the_cli_too(self):
        from nexus import steamlib
        from nexus.cli import running
        with mock.patch.object(steamlib, 'open_url', return_value=True) as op, \
                mock.patch('builtins.print'):
            running.cmd_open_url('ms-settings:storagesense')
            with self.assertRaises(SystemExit):
                running.cmd_open_url('https://example.com/')
        op.assert_called_once_with('ms-settings:storagesense')

    @linux_only
    def test_linux_notifies_with_notify_send_and_has_no_startup_folder(self):
        lin = platform._impl
        with mock.patch.object(lin.shutil, 'which', return_value='/usr/bin/notify-send'), \
                mock.patch.object(lin.subprocess, 'run') as run:
            platform.notify_local("T", "B")
        self.assertEqual(run.call_args[0][0], ['notify-send', '-a', 'Nexus', '-i', 'nexus',
                                               'T', 'B'])
        with mock.patch.object(lin.shutil, 'which', return_value='/usr/bin/notify-send'), \
                mock.patch.object(lin.subprocess, 'run',
                                  side_effect=subprocess.TimeoutExpired('notify-send', 5)), \
                self.assertRaises(OSError):
            platform.notify_local("T", "B")
        with mock.patch.object(lin.shutil, 'which', return_value=None), \
                mock.patch.object(lin.subprocess, 'run') as run:
            platform.notify_local("T", "B")             # nothing to notify with: fine
        run.assert_not_called()
        self.assertIsNone(platform.startup_dir(Path('/h')))
        from nexus import paths
        self.assertEqual(platform.gui_command(paths.REPO), [str(paths.REPO / 'bin' / 'nexus')])

    @windows_only
    def test_windows_toast_startup_folder_and_gui(self):
        win = platform._impl
        with mock.patch.object(win.subprocess, 'Popen') as popen:
            platform.notify_local("Title 'x'", "Body $env:PATH")
        argv, kw = popen.call_args
        self.assertEqual(argv[0][:2], ['powershell', '-NoProfile'])
        self.assertEqual((kw['env']['NX_TITLE'], kw['env']['NX_BODY']),
                         ("Title 'x'", "Body $env:PATH"))      # data, never script
        home = Path(sandbox.HOME)
        self.assertEqual(platform.startup_dir(home), home / 'AppData' / 'Roaming' / 'Microsoft'
                         / 'Windows' / 'Start Menu' / 'Programs' / 'Startup')
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, 'executable', r'C:\Apps\nexus-launcher\nexus-cli.exe'):
            self.assertEqual(platform.gui_command(Path('C:/src')),
                             [r'C:\Apps\nexus-launcher\nexus-launcher.exe'])
        from nexus import paths
        cmd = platform.gui_command(paths.REPO)
        self.assertEqual(Path(cmd[1]), paths.REPO / 'nexus' / 'gui' / '__main__.py')


class GuiCancel(unittest.TestCase):
    """Stopping a GUI job: SIGTERM on Linux; on Windows a named event the CLI
    watches from a thread (platform.cancel_event, watch_cancel)."""

    def test_the_cli_takes_the_event_out_of_its_environment(self):
        from nexus.cli import common
        with mock.patch.dict(os.environ, {platform.CANCEL_EVENT_ENV: 'Local\\x'}), \
                mock.patch.object(common.nxplatform, 'watch_cancel') as watch:
            common.watch_gui_cancel()
            self.assertNotIn(platform.CANCEL_EVENT_ENV, os.environ)
        watch.assert_called_once_with('Local\\x', common._cancelled_by_gui)
        with mock.patch.object(common.nxplatform, 'watch_cancel') as watch:
            common.watch_gui_cancel()                   # from a terminal: nothing
        watch.assert_not_called()

    @linux_only
    def test_linux_has_no_event(self):
        self.assertIsNone(platform.cancel_event())
        self.assertFalse(platform.watch_cancel('x', print))

    @windows_only
    def test_setting_the_event_wakes_the_watcher(self):
        import threading
        ev = platform.cancel_event()
        self.addCleanup(ev.close)
        self.assertTrue(ev.name.startswith('Local\\nexus-cancel-'))
        woke = threading.Event()
        self.assertTrue(platform.watch_cancel(ev.name, woke.set))
        self.assertFalse(woke.wait(0.2))
        self.assertTrue(ev.set())
        self.assertTrue(woke.wait(5))
        self.assertFalse(platform.watch_cancel(ev.name + '-nope', woke.set))
        ev.close()
        self.assertFalse(ev.set())                      # closed: nothing to set

    @windows_only
    def test_a_cli_asleep_cancels_at_once_and_stops_its_step(self):
        # The CLI's main thread sleeps (a SIGBREAK handler would wait for it)
        # while its running step, a grandchild, waits on; the event ends both.
        code = ("import os, subprocess, sys, time\n"
                "from nexus import platform\n"
                "from nexus.cli import common\n"
                "common.watch_gui_cancel()\n"
                "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
                " **platform.new_group())\n"
                "common._ACTIVE_PROC = g\n"
                "print(g.pid, os.environ.get(platform.CANCEL_EVENT_ENV), flush=True)\n"
                "time.sleep(60)\n")
        ev = platform.cancel_event()
        self.addCleanup(ev.close)
        from nexus import paths
        env = dict(os.environ, PYTHONPATH=str(paths.REPO), PYTHONUNBUFFERED='1')   # as jobs run
        env[platform.CANCEL_EVENT_ENV] = ev.name
        proc = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                errors='replace', env=env, **platform.new_group())
        self.addCleanup(platform.kill_tree, proc)
        pid, seen = proc.stdout.readline().split()
        self.assertEqual(seen, 'None')                  # not handed on to children
        t = time.monotonic()
        ev.set()
        self.assertEqual(proc.wait(15), 130)
        self.assertLess(time.monotonic() - t, 10)
        self.assertIn('Cancelled', proc.stdout.read())
        proc.stdout.close()
        self.assertFalse(any(p.pid == int(pid) for p in platform.processes()),
                         "the running step survived")


if __name__ == '__main__':
    unittest.main()
