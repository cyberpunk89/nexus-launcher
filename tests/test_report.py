"""Bug reports, phase 1a (nexus/report, nexus-cli --report-*): the scrubber,
the draft, the issue text and link, crash files and the launch log's
system block. Nothing here touches the network."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests.sandbox  # noqa: E402,F401  (HOME -> a temp dir; before any nexus import)
from nexus import features  # noqa: E402
from nexus.report import crash, render as RD, scrub as S, sysinfo  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / 'bin' / 'nexus-cli'
NEXUS = REPO / 'nexus'

# A machine full of decoys: none of these may be in anything a report shows.
CTX = {'home': '/home/zelda', 'user': 'zelda',
       'hosts': ['hyrule-castle', 'Triforce Rig'],
       'paths': ['/mnt/games/Repacks', '/home/zelda/.local/share/Steam', r'D:\Repacks',
                 r'C:\Program Files (x86)\Steam'],
       'games': ['majoras-mask', "Majora's Mask", 'MajoraLauncher', 'ocarina-of-time',
                 'Ocarina of Time'],
       'secrets': ['sgdb-0123456789abcdef', 'gdb_tok_9f8e7d6c5b4a']}
DECOYS = ('zelda', 'hyrule', 'triforce', 'repacks', 'majora', 'ocarina', 'sgdb-0123',
          'gdb_tok', 'link@example.org', '192.168.1.44', 'fe80::1ff:fe23:4567:890a',
          '123e4567-e89b-12d3-a456-426614174000', '76561198012345678', 'eyJhbGciOi',
          'hunter2', 'Bearer abc', 'secretkey123', '/home/sheik', 'Users\\Impa',
          'aa:bb:cc:dd:ee:ff')

TRACEBACK = '''Traceback (most recent call last):
  File "/home/zelda/src/nexus-launcher/nexus/cli/entry.py", line 23, in main
    _main()
  File "/home/zelda/src/nexus-launcher/nexus/cli/install.py", line 412, in cmd_install
    run(game_dir / 'setup.exe')
  File "/usr/lib/python3.13/subprocess.py", line 554, in run
  File "C:\\Program Files\\nexus-launcher\\_internal\\nexus\\gui\\win_play.py", line 9 in stopped
FileNotFoundError: [Errno 2] No such file or directory: '/mnt/games/Repacks/majoras-mask/setup.exe'
KeyError: "Majora's Mask"'''

DOCTOR_ROWS = [
    {'group': 'Requirements', 'status': 'ok', 'title': 'Wine', 'detail': 'wine-10.0'},
    {'group': 'Requirements', 'status': 'ok', 'title': 'Install folder',
     'detail': '/mnt/games/Repacks'},
    {'group': 'Requirements', 'status': 'ok', 'title': 'Proton',
     'detail': 'GE-Proton10-25, proton-cachyos'},
    {'group': 'Integrations', 'status': 'ok', 'title': 'Steam account',
     'detail': 'ZeldaFan (76561198012345678) — the most recent login'},
    {'group': 'Integrations', 'status': 'ok', 'title': 'Heroic',
     'detail': '/home/zelda/.config/heroic'},
    {'group': 'Integrations', 'status': 'ok', 'title': 'Play Proton',
     'detail': 'GE-Proton10-25 ([runner] proton)'},
    {'group': 'Games', 'status': 'warn', 'title': "Majora's Mask: launcher uses a different "
                                                  "Proton", 'detail': 'x'},
    {'group': 'Storage', 'status': 'info', 'title': 'Ocarina of Time takes 60 GB',
     'detail': ''},
]

LOG_TAIL = [
    "[12:30:45] Installing Majora's Mask from /mnt/games/Repacks/Majora's Mask [v1.2]/setup.exe",
    '[12:30:46] GET https://hyrule.example.net/api/v1/games?token=hunter2 -> 200',
    '[12:30:47] Authorization: Bearer abcdefghijklmnop',
    '[12:30:48] user link@example.org from 192.168.1.44 and fe80::1ff:fe23:4567:890a',
    '[12:30:49] device 123e4567-e89b-12d3-a456-426614174000 steam 76561198012345678',
    '[12:30:50] jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk',
    '[12:30:51] api_key=secretkey123 controller aa:bb:cc:dd:ee:ff on hyrule-castle',
    '[12:30:52] config at /home/sheik/.config/nexus and C:\\Users\\Impa\\AppData\\Roaming',
    '[12:30:53] zelda started MajoraLauncher.exe on Triforce Rig',
    '[12:30:54] sgdb key sgdb-0123456789abcdef, gamedb gdb_tok_9f8e7d6c5b4a',
]

PROTON_LOG = [
    'pid 4242: ZELDA environment: WINEPREFIX=/mnt/games/Repacks/majoras-mask/pfx',
    'Proton: /home/zelda/.local/share/Steam/steamapps/common/Proton - Experimental/proton',
    'exe: /home/zelda/.local/share/Steam/steamapps/common/Ocarina of Time/oot.exe',
    "err:module:import_dll Library d3dx9_43.dll (needed by L\"D:\\\\Repacks\\\\Majora's Mask"
    "\\\\majora.exe\") not found",
    'Z:\\home\\zelda\\Games\\x.exe; C:\\Program Files (x86)\\Steam\\steamapps\\common\\'
    'Ocarina of Time\\oot.exe',
]


def survivors(text: str) -> list[str]:
    low = text.lower()
    return [d for d in DECOYS if d.lower() in low]


class Scrub(unittest.TestCase):
    def test_no_decoy_survives(self):
        with mock.patch.object(features, 'FEATURES', features.ALL):
            runtime = sysinfo.runtime_group(CTX, DOCTOR_ROWS)
        texts = {
            'traceback': S.scrub(TRACEBACK, CTX),
            'doctor': '\n'.join(runtime),
            'log': '\n'.join(S.scrub_lines(LOG_TAIL, CTX)),
            'proton': '\n'.join(S.scrub_lines(PROTON_LOG, CTX)),
            'hostname': S.scrub('Built on HYRULE-CASTLE for triforce rig', CTX),
        }
        for name, text in texts.items():
            self.assertEqual(survivors(text), [], f"{name}:\n{text}")
        self.assertNotIn('Games', '\n'.join(runtime))         # no Games/Storage rows
        self.assertIn('Wine: ok, wine-10.0', runtime)
        self.assertIn('Steam account: ok, found', runtime)
        # what makes a report useful is still there
        self.assertIn('GE-Proton10-25', texts['doctor'])
        self.assertIn('d3dx9_43.dll', texts['proton'])
        self.assertIn('[12:30:45]', texts['log'])

    def test_nexus_frames_kept(self):
        out = S.scrub(TRACEBACK, CTX).splitlines()
        self.assertIn('  File "nexus/cli/entry.py", line 23, in main', out)
        self.assertIn('  File "nexus/cli/install.py", line 412, in cmd_install', out)
        self.assertIn('  File "<stdlib>", line 554, in run', out)
        self.assertIn('  File "nexus/gui/win_play.py", line 9 in stopped', out)
        try:
            RD.render(None)
        except AttributeError as e:
            frames = crash.frames_of(e)
        self.assertEqual(frames, ['nexus/report/render.py:render',
                                  'nexus/report/render.py:included'])

    def test_config_secrets_never_appear_even_allow_listed(self):
        from nexus.cli.common import SECRET_ENV
        from nexus.cli.info import CONFIG_SECRETS
        values = {f"{sec}.{key}": f"v4lue-{i}-xyz" for i, (sec, key, _) in
                  enumerate(CONFIG_SECRETS)}
        data = {'runner': {'backend': 'proton'}}
        for (sec, key, _), v in zip(CONFIG_SECRETS, values.values()):
            data.setdefault(sec, {})[key] = v
        env = {var: f"envsecret-{i}-abc" for i, var in enumerate(SECRET_ENV)}
        allow = sysinfo.SETTINGS_ALLOW + tuple((s, k) for s, k, _ in CONFIG_SECRETS)
        with mock.patch.object(sysinfo, 'SETTINGS_ALLOW', allow), \
                mock.patch.object(sysinfo, 'ENV_ALLOW', sysinfo.ENV_ALLOW + SECRET_ENV), \
                mock.patch.dict(os.environ, env):
            out = '\n'.join(sysinfo.settings_group({}, data))
            draft = sysinfo.collect(ctx={'secrets': [*values.values(), *env.values()]},
                                    rows=[], config=data,
                                    log_lines=[f"token is {v}" for v in values.values()])
        self.assertIn('runner.backend = proton', out)
        body = RD.render(draft, {'exclude': []})['body']
        for v in (*values.values(), *env.values()):
            self.assertNotIn(v, out)
            self.assertNotIn(v, body)

    def test_report_package_is_qt_free(self):
        for p in (NEXUS / 'report').glob('*.py'):
            text = p.read_text(encoding='utf-8')
            self.assertNotRegex(text, r'(?m)^\s*(?:from|import)\s.*\b(?:PyQt6?|facts)\b', p.name)


GOLDEN_DRAFT = {
    'schema': 1, 'id': 'a1b2c3d4e5f6', 'kind': 'cli-internal', 'version': '0.5.3',
    'edition': 'linux', 'signature': '3f9a0c1b2d4e',
    'title': '[cli] KeyError in nexus/cli/entry.py:_main (0.5.3, linux)',
    'groups': {'app': ['Version: 0.5.3', 'Edition: linux'],
               'error': ['Type: KeyError', "Message: '<s>'"],
               'log': ['line ``` one', '<home>/x'],
               'game': ['Title: Decoy'],
               'proton_log': ['p1']},
    'default_exclude': ['proton_log']}

GOLDEN_BODY = '''Nexus 0.5.3 (linux), cli-internal, sig:3f9a0c1b2d4e

### What happened

```text
It broke
```

<details><summary>Error</summary>

```text
Type: KeyError
Message: '<s>'
```

</details>

<details><summary>App</summary>

```text
[line removed by reporter]
Edition: linux
```

</details>

<details><summary>Recent log</summary>

```text
line \'\'\' one
<home>/x
```

</details>

''' + ('<sub>Made by Nexus\'s "Report a problem". Names and paths were replaced before it '
       'left the computer.</sub>')


def big_draft(lines: int) -> dict:
    groups = {'app': ['Version: 0.5.3', 'Edition: linux', 'Install: git'],
              'system': ['OS: Test OS', 'Kernel: 6.1'],
              'runtime': ['Wine: ok, wine-10.0', 'umu-run: ok, found'],
              'log': [f"log line {i} " + 'x' * 60 for i in range(lines)],
              'game': ["Title: Majora's Mask", 'Steam appid: 4242']}
    return {'schema': 1, 'id': 'b' * 12, 'kind': 'user-report', 'version': '0.5.3',
            'edition': 'linux', 'signature': None, 'title': '[report] Problem report',
            'groups': groups, 'default_exclude': ['proton_log']}


class Render(unittest.TestCase):
    def test_golden(self):
        out = RD.render(GOLDEN_DRAFT, {'description': 'It broke', 'remove': [4]})
        self.assertEqual(out['body'], GOLDEN_BODY)
        self.assertEqual(out['title'], GOLDEN_DRAFT['title'])
        self.assertEqual([g['key'] for g in out['groups'] if g['included']],
                         ['error', 'app', 'log'])

    def test_with_game_is_only_way_title_appears(self):
        game = {'slug': 'twilight-princess', 'display_name': 'Twilight Princess HD',
                'steam_appid': 4242}
        crash_rec = {'id': '20261105T120000Z-abcd', 'kind': 'game-failure', 'type': 'KeyError',
                     'message': "'Twilight Princess HD'", 'signature': 'abcdef012345',
                     'frames': ['nexus/cli/sunshine.py:cmd_launch'],
                     'traceback': 'KeyError: Twilight Princess HD', 'count': 1}
        draft = sysinfo.collect(ctx=dict(CTX), crash=crash_rec, game=game, rows=[], config={},
                                log_lines=['started Twilight Princess HD (twilight-princess)'])
        for sel in ({}, {'exclude': []}, {'exclude': ['log'], 'remove': [1]}):
            out = RD.render(draft, sel)
            self.assertNotIn('twilight', (out['title'] + out['body']).lower(), sel)
            self.assertNotIn('4242', out['body'])
        out = RD.render(draft, {'with_game': True})
        self.assertIn('Title: Twilight Princess HD', out['body'])
        self.assertIn('Steam appid: 4242', out['body'])
        self.assertNotIn('twilight', out['title'].lower())

    def test_remove_line_stable_across_exclude(self):
        draft = big_draft(5)
        full = RD.numbered(draft)
        target = next(ln for ln in full if ln['text'].startswith('log line 2 '))
        numbers = None
        for exclude in ([], ['runtime'], ['system', 'runtime'], None):
            out = RD.render(draft, {'exclude': exclude, 'remove': [target['n']]})
            self.assertNotIn(target['text'], out['body'])
            self.assertIn(RD.REMOVED, out['body'])
            self.assertIn('log line 3 ', out['body'])
            now = [(ln['n'], ln['text']) for ln in out['lines']]
            self.assertEqual(now, numbers or now)
            numbers = now
        out = RD.render(draft, {'exclude': ['log'], 'remove': [target['n']]})
        self.assertNotIn(RD.REMOVED, out['body'])

    def test_past_7500_uses_clipboard(self):
        small = RD.render(big_draft(3))
        link = RD.issue_url(small, big_draft(3))
        self.assertIsNone(link['clipboard'])
        self.assertTrue(link['url'].startswith(
            'https://github.com/cyberpunk89/nexus-launcher/issues/new?template=bug.yml&'))
        self.assertIn('report=Nexus+0.5.3', link['url'])
        self.assertLessEqual(len(link['url']), RD.MAX_URL)
        big = RD.render(big_draft(200))
        link = RD.issue_url(big, big_draft(200))
        self.assertEqual(link['clipboard'], big['body'])
        self.assertLessEqual(len(link['url']), RD.MAX_URL)
        self.assertNotIn('log+line', link['url'])
        self.assertIn('clipboard', link['url'])


class CrashFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        p = mock.patch.object(crash, '_dir', lambda: self.dir)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def files(self, pattern='crash-*.json'):
        return sorted(self.dir.glob(pattern))

    def error(self):
        try:
            RD.render(None)
        except AttributeError as e:
            return e

    def test_cli_internal_error_writes_file(self):
        from nexus.cli import entry
        argv = ['nexus-cli', '--list', '--json', '--game', 'majoras-mask', '--describe=hunter2']
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(entry, '_main', side_effect=KeyError('boom')), \
                mock.patch.object(sys, 'argv', argv), redirect_stdout(out), \
                redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            entry.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(json.loads(out.getvalue())['error']['kind'], 'internal')
        [f] = self.files()
        rec = json.loads(f.read_text(encoding='utf-8'))
        self.assertEqual((rec['kind'], rec['type'], rec['count']), ('cli-internal', 'KeyError', 1))
        self.assertEqual(rec['flags'], ['--list', '--json', '--game', '--describe'])
        self.assertNotIn('majoras', json.dumps(rec['flags']))
        self.assertNotIn('hunter2', f.read_text(encoding='utf-8'))
        self.assertRegex(rec['signature'], r'^[0-9a-f]{12}$')
        self.assertTrue(crash.CRASH_ID.match(rec['id']))
        if os.name != 'nt':
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_record_never_raises(self):
        with mock.patch.object(crash, '_write', side_effect=OSError('disk full')), \
                mock.patch.object(sys, 'stderr', None):
            self.assertIsNone(crash.record('gui-exception', self.error()))

    def test_same_signature_counts(self):
        a = crash.record('gui-exception', self.error())
        b = crash.record('gui-exception', self.error())
        self.assertEqual(a, b)
        [f] = self.files()
        self.assertEqual(json.loads(f.read_text(encoding='utf-8'))['count'], 2)
        crash.record('gui-exception', ValueError('elsewhere'))      # no frames: another sig
        self.assertEqual(len(self.files()), 2)
        self.assertEqual([r['count'] for r in crash.pending()], [1, 2])

    def test_keeps_newest_10(self):
        ids = []
        for i in range(12):
            cid = crash.record('native-crash', f"Fatal Python error: Kind{i}\n")
            ids.append(cid)
            os.utime(self.dir / f"crash-{cid}.json", (1000 + i, 1000 + i))
        self.assertEqual(len(self.files()), crash.KEEP)
        self.assertEqual({p.stem[6:] for p in self.files()}, set(ids[2:]))

    def native(self, pid, text):
        p = self.dir / f"native-{pid}.txt"
        p.write_text(text, encoding='utf-8')
        return p

    DUMP = ('Fatal Python error: Segmentation fault\n\n'
            'Current thread 0x00007f (most recent call first):\n'
            '  File "/home/zelda/nexus/nexus/gui/tiles.py", line 88 in paintEvent\n'
            '  File "/home/zelda/nexus/nexus/gui/app.py", line 412 in main\n')

    def test_stale_native_becomes_report(self):
        p = self.native(999991, self.DUMP)
        with mock.patch.object(crash, '_alive_pids', return_value={1, 2}):
            [cid] = crash.harvest()
        self.assertFalse(p.exists())
        rec = crash.load(cid)
        self.assertEqual((rec['kind'], rec['type']), ('native-crash', 'Segmentation fault'))
        self.assertEqual(rec['frames'], ['nexus/gui/app.py:main', 'nexus/gui/tiles.py:paintEvent'])

    def test_empty_native_dropped(self):
        p = self.native(999992, '')
        with mock.patch.object(crash, '_alive_pids', return_value=set()):
            self.assertEqual(crash.harvest(), [])
        self.assertFalse(p.exists())
        self.assertEqual(self.files(), [])

    def test_live_pid_skipped(self):
        p = self.native(999993, self.DUMP)
        with mock.patch.object(crash, '_alive_pids', return_value={999993}):
            self.assertEqual(crash.harvest(), [])
            self.assertEqual(crash.pending(), [])
        self.assertTrue(p.exists())

    def test_native_file_lives_until_a_clean_exit(self):
        import faulthandler
        was = faulthandler.is_enabled()
        path = crash.enable_native()
        try:
            self.assertEqual(path, self.dir / f"native-{os.getpid()}.txt")
            self.assertEqual(crash.native_path(), path)
            crash.native_note('qFatal: test')
            with mock.patch.object(crash, '_alive_pids', return_value=set()):
                self.assertEqual(crash.harvest(), [])        # ours: never touched
            self.assertTrue(path.exists())
        finally:
            crash.disable_native()
        self.assertFalse(path.exists())
        self.assertIsNone(crash.native_path())
        self.assertEqual(faulthandler.is_enabled(), was)

    def test_ids_are_checked(self):
        for bad in ('../x', '20261105T120000Z-abcd/../../y', '', 'a' * 12 + '/'):
            self.assertIsNone(crash.load(bad))
            self.assertIsNone(crash.load_draft(bad))
            self.assertFalse(crash.dismiss(bad))


class Editions(unittest.TestCase):
    def test_windows_set_hides_proton_groups(self):
        from nexus.paths import LOG_DIR
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        plog = LOG_DIR / 'steam-4242.log'
        plog.write_text('proton says hi\n', encoding='utf-8')
        self.addCleanup(plog.unlink)
        game = {'slug': 'twilight-princess', 'display_name': 'Twilight Princess HD'}
        with mock.patch.object(features, 'FEATURES', features.WINDOWS):
            rows = sysinfo.runtime_group({}, DOCTOR_ROWS)
            draft = sysinfo.collect(ctx={}, game=game, rows=DOCTOR_ROWS, config={})
        self.assertFalse([r for r in rows if re.search(r'(?i)proton|wine|umu|ntsync', r)], rows)
        self.assertIn('Heroic: ok, <home>/.config/heroic', rows)
        self.assertNotIn('proton_log', draft['groups'])
        body = RD.render(draft, {'exclude': []})['body']
        self.assertNotIn('Proton', body)
        with mock.patch.object(features, 'FEATURES', features.ALL):
            draft = sysinfo.collect(ctx={}, game=game, rows=DOCTOR_ROWS, config={})
        self.assertIn('proton_log', draft['groups'])
        self.assertNotIn('Proton log', RD.render(draft)['body'])     # off until asked for
        self.assertIn('Proton log', RD.render(draft, {'exclude': []})['body'])


class LaunchLog(unittest.TestCase):
    def test_system_block_keeps_exit_code_readable(self):
        from nexus.cli import sunshine
        from nexus.paths import LOG_DIR
        try:
            from nexus.gui.win_play import last_exit_code
        except ImportError:
            self.skipTest("PyQt6 not installed")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        slug = 'system-block-test'
        out = sunshine.launch_log_path(slug)
        self.addCleanup(out.unlink, missing_ok=True)
        gpus = [{'vendor': 'acme', 'name': 'Error Fatal Crash GPU', 'integrated': False,
                 'driver': 'failsafe', 'driver_version': None}]
        with mock.patch.object(sunshine, '_start_game', return_value=3), \
                mock.patch.object(sunshine, 'registry_get', return_value={}), \
                mock.patch.object(sunshine, 'game_processes', return_value=[]), \
                mock.patch('nexus.platform.gpus', return_value=gpus):
            rc, _ = sunshine._run_session(slug, '/nowhere/launch.sh', out, datetime.now())
        self.assertEqual(rc, 3)
        text = out.read_text(encoding='utf-8')
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith(f'# {slug} launched '))
        self.assertTrue(lines[1].startswith('# Nexus '), text)
        block = [ln for ln in lines[1:] if ln.startswith('# ') and 'exited' not in ln]
        self.assertTrue(any(ln.startswith('# GPU ') for ln in block), text)
        for ln in block:          # explain_log's own pattern for trouble
            self.assertNotRegex(ln, r'(?i)\b(err|error|fail|fatal|exception|crash|unhandled)\b')
        self.assertEqual(last_exit_code(slug), 3)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.reports = tests.sandbox.data_dir(self.home) / 'logs' / 'reports'
        self.reports.mkdir(parents=True)

    def cli(self, *args):
        env = {**os.environ, **tests.sandbox.home_env(self.home)}
        return subprocess.run([sys.executable, str(CLI), *args], env=env, text=True,
                              capture_output=True, stdin=subprocess.DEVNULL, timeout=60)

    def test_preview_url_and_pending_json(self):
        draft = big_draft(4)
        (self.reports / f"draft-{draft['id']}.json").write_text(json.dumps(draft),
                                                                encoding='utf-8')
        r = self.cli('--report-preview', draft['id'], '--json', '--exclude', 'runtime',
                     '--remove-line', '1', '--describe', 'It froze')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['draft'], draft['id'])
        self.assertTrue({'title', 'body', 'lines', 'groups', 'length'} <= set(out))
        self.assertNotIn('Wine', out['body'])
        self.assertIn('It froze', out['body'])
        r = self.cli('--report-url', draft['id'], '--json', '--with-game')
        link = json.loads(r.stdout)
        self.assertIsNone(link['clipboard'])
        self.assertIn('Majora', link['url'].replace('%27', "'").replace('+', ' '))
        r = self.cli('--report-pending', '--json')
        self.assertEqual(json.loads(r.stdout), {'reports': []})

    def test_bad_ids_are_refused(self):
        for args in (('--report-preview', '../../etc'), ('--report-url', 'nope'),
                     ('--report-collect', '--crash', '../x'), ('--report-dismiss', 'x/y')):
            r = self.cli(*args, '--json')
            self.assertEqual(r.returncode, 1, args)
            self.assertEqual(json.loads(r.stdout)['error']['kind'], 'bad-id', args)


if __name__ == '__main__':
    unittest.main()
