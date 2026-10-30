"""packaging/release.py: the export-and-release script (p5-07). The pure parts
directly; the whole dry run against throwaway git repos, with `gh` faked, to
prove it never pushes, tags or releases."""

try:
    import sandbox  # noqa: F401  (first, like every test module)
    from oskind import linux_only
except ImportError:             # run as tests.<module>
    from tests import sandbox  # noqa: F401
    from tests.oskind import linux_only

import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location('nexus_release', ROOT / 'packaging' / 'release.py')
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

SHA = 'a' * 40


class GateTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='nexus-gate-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.dir, True))

    def write(self, text):
        p = self.dir / 'gate.txt'
        p.write_text(text, encoding='utf-8')
        return p

    def test_path_flag_beats_env_beats_default(self):
        env = {release.GATE_ENV: '/env/gate.txt'}
        self.assertEqual(release.gate_path('/flag/g.txt', env), Path('/flag/g.txt'))
        self.assertEqual(release.gate_path(None, env), Path('/env/gate.txt'))
        self.assertEqual(release.gate_path(None, {}),
                         Path(release.GATE_DEFAULT).expanduser())
        # (not "no ~ anywhere": Windows short names like RUNNER~1 have one)
        self.assertFalse(str(release.gate_path(None, {})).startswith('~'))

    def test_missing_file_refuses(self):
        with self.assertRaises(release.ReleaseError) as cm:
            release.load_gate(self.dir / 'nope.txt')
        self.assertIn('refusing', str(cm.exception))
        self.assertIn('nope.txt', str(cm.exception))

    def test_empty_gate_refuses(self):
        for text in ('', '\n\n', '# only a comment\n   \n'):
            with self.subTest(text=text), self.assertRaises(release.ReleaseError):
                release.load_gate(self.write(text))

    def test_bad_regex_names_the_line_not_the_pattern(self):
        p = self.write('# c\nfine\nsecretish(\n')
        with self.assertRaises(release.ReleaseError) as cm:
            release.load_gate(p)
        self.assertIn('line 3', str(cm.exception))
        self.assertNotIn('secretish', str(cm.exception))

    def test_patterns_skip_comments_and_ignore_case(self):
        pats = release.load_gate(self.write('# names\n\nalpha\\d+\n  beta  \n'))
        self.assertEqual([n for n, _ in pats], [3, 4])
        self.assertTrue(pats[0][1].search('ALPHA42'))

    def test_hits_give_place_and_gate_line_never_the_text(self):
        pats = release.load_gate(self.write('zebra\nquokka\n'))
        files = [('ok.py', b'nothing here\n'),
                 ('tests/fixtures/data.json', b'{\n  "who": "Zebra"\n}\n'),
                 ('assets/bin.dat', b'\x00\xff\x00quokka\x00'),
                 ('docs/zebra-notes.txt', b'plain\n')]
        hits = release.gate_hits(files, pats)
        self.assertIn('tests/fixtures/data.json:2 (gate line 1)', hits)
        self.assertIn('assets/bin.dat:1 (gate line 2)', hits)
        self.assertIn('docs/zebra-notes.txt (file name, gate line 1)', hits)
        self.assertFalse(any(h.startswith('ok.py') for h in hits))
        self.assertFalse(any('quokka' in h.lower() for h in hits))

    def test_clean_files_pass(self):
        pats = release.load_gate(self.write('zebra\n'))
        self.assertEqual(release.gate_hits([('a.py', b'x = 1\n')], pats), [])


class VersionTest(unittest.TestCase):
    def test_tag_is_v_plus_version(self):
        self.assertEqual(release.check_version('0.3.0'), 'v0.3.0')
        self.assertEqual(release.check_version('1.2.10rc1'), 'v1.2.10rc1')

    def test_odd_versions_refused(self):
        for v in ('0.3', 'v0.3.0', '0.3.0-dev', '0.3.0 ', 'x.y.z', ''):
            with self.subTest(v=v), self.assertRaises(release.ReleaseError):
                release.check_version(v)

    def test_reads_version_and_app_name(self):
        self.assertEqual(release.read_version('"""doc"""\n\n__version__ = "1.4.2"\n'), '1.4.2')
        self.assertEqual(release.read_app_name("APP_NAME = 'nexus-launcher'\n"), 'nexus-launcher')
        with self.assertRaises(release.ReleaseError):
            release.read_version('version = "1"\n')

    def test_this_checkout_has_a_releasable_version(self):
        init = (ROOT / 'nexus' / '__init__.py').read_text(encoding='utf-8')
        release.check_version(release.read_version(init))
        release.read_app_name((ROOT / 'nexus' / 'platform' / 'windows.py').read_text(encoding='utf-8'))

    def test_asset_names_match_the_build_workflow(self):
        setup, portable = release.asset_names('nexus-launcher', '0.3.0')
        self.assertEqual(setup, 'nexus-launcher-Setup-0.3.0.exe')
        self.assertEqual(portable, 'nexus-launcher-0.3.0-portable.zip')
        wf = (ROOT / '.github' / 'workflows' / 'build-windows.yml').read_text(encoding='utf-8')
        meta = '${{ steps.meta.outputs.%s }}'
        self.assertIn(f"{meta % 'name'}-Setup-{meta % 'version'}.exe", wf)
        self.assertIn(f"{meta % 'name'}-{meta % 'version'}-portable.zip", wf)
        self.assertIn(f"name: {meta % 'name'}-{meta % 'version'}-installers", wf)
        self.assertEqual(release.artifact_name('nexus-launcher', '0.3.0'),
                         'nexus-launcher-0.3.0-installers')


def tests_run(conclusion='success', status='completed', sha=SHA, created='2026-10-23T10:00:00Z',
              id_=1, workflow='tests', branch='windows-port'):
    return {'databaseId': id_, 'workflowName': workflow, 'headSha': sha, 'headBranch': branch,
            'status': status, 'conclusion': conclusion, 'createdAt': created}


GOOD_JOBS = [{'name': 'ubuntu-latest / py3.14', 'conclusion': 'success'},
             {'name': 'windows-latest / py3.12', 'conclusion': 'success'}]


class NtfyTest(unittest.TestCase):
    def test_flag_beats_env_and_unset_is_none(self):
        env = {release.NTFY_ENV: 'https://ntfy.sh/env'}
        self.assertEqual(release.ntfy_url('https://ntfy.sh/flag', env), 'https://ntfy.sh/flag')
        self.assertEqual(release.ntfy_url(None, env), 'https://ntfy.sh/env')
        self.assertIsNone(release.ntfy_url(None, {}))

    def test_plain_http_refused(self):
        with self.assertRaises(release.ReleaseError):
            release.ntfy_url('http://ntfy.sh/topic', {})


CARDS = [{'kind': 'new', 'icon': 'pin', 'title': 'Pins', 'text': 'Pin things.'},
         {'kind': 'fixed', 'icon': 'info', 'title': 'Fixes', 'text': 'Fewer bugs.',
          'platform': 'windows'}]


class DiscordTest(unittest.TestCase):
    HOOK = 'https://discord.com/api/webhooks/1234/tok-EN_x'

    def test_flag_beats_env_beats_file_and_unset_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'hook'
            f.write_text(self.HOOK.replace('1234', '9999') + '\n')
            env = {release.DISCORD_ENV: self.HOOK.replace('1234', '5555')}
            self.assertEqual(release.discord_url(self.HOOK, env, f), self.HOOK)
            self.assertIn('5555', release.discord_url(None, env, f))
            self.assertIn('9999', release.discord_url(None, {}, f))
            self.assertIsNone(release.discord_url(None, {}, Path(d) / 'missing'))

    def test_other_urls_refused_without_quoting_them(self):
        for bad in ('http://discord.com/api/webhooks/1/tok', 'https://evil.example/api/webhooks/1/tok',
                    'https://discord.com/api/webhooks/1/tok/extra'):
            with self.assertRaises(release.ReleaseError) as cm:
                release.discord_url(bad, {}, None)
            self.assertNotIn('tok', str(cm.exception).replace('<token>', ''))

    def test_payload_lists_the_cards_and_pings_nobody(self):
        body = release.discord_payload('0.5.2', 'https://x/rel', CARDS)
        self.assertEqual(body['allowed_mentions'], {'parse': []})
        embed = body['embeds'][0]
        self.assertEqual((embed['title'], embed['url']), ('Nexus 0.5.2 is out', 'https://x/rel'))
        self.assertIn('**New** · Pins\nPin things.', embed['description'])
        self.assertIn('**Fixed** · Fixes (Windows only)', embed['description'])

    def test_long_payload_is_cut_under_the_cap(self):
        cards = [{'title': f'T{i}', 'text': 'x' * 300} for i in range(30)]
        desc = release.discord_payload('1', 'l', cards)['embeds'][0]['description']
        self.assertLessEqual(len(desc), 4096)
        self.assertTrue(desc.endswith('…'))

    def test_post_sends_json_and_hides_the_url_on_error(self):
        seen = []

        class Ok:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout):
            seen.append(req)
            return Ok()
        with mock.patch.object(release.urllib.request, 'urlopen', fake):
            release.post_discord(self.HOOK, {'a': 1})
        self.assertEqual(json.loads(seen[0].data), {'a': 1})
        self.assertEqual(seen[0].get_header('Content-type'), 'application/json')
        self.assertTrue(seen[0].get_header('User-agent').startswith('nexus-release'))

        def refuse(req, timeout):
            raise release.urllib.error.HTTPError(self.HOOK, 404, 'Not Found', {}, None)
        with mock.patch.object(release.urllib.request, 'urlopen', refuse):
            with self.assertRaises(OSError) as cm:
                release.post_discord(self.HOOK, {})
        self.assertEqual(str(cm.exception), 'HTTP 404 Not Found')

    def test_discord_test_needs_a_hook(self):
        out = []
        with mock.patch.object(release, 'DISCORD_DEFAULT', '/nonexistent/hook'), \
                mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop(release.DISCORD_ENV, None)
            self.assertEqual(release.main(['--discord-test'], out=out.append), release.FAILED)
        self.assertIn('no Discord webhook', '\n'.join(out))


class WhatsNewTest(unittest.TestCase):
    def test_cards_for_the_version_or_none(self):
        text = json.dumps({'1.0.0': CARDS})
        self.assertEqual(release.whats_new_cards(text, '1.0.0'), CARDS)
        self.assertEqual(release.whats_new_cards(text, '2.0.0'), [])

    def test_card_without_text_refused(self):
        with self.assertRaises(release.ReleaseError):
            release.whats_new_cards(json.dumps({'1': [{'title': 'x'}]}), '1')

    def test_markdown_section(self):
        md = release.whats_new_markdown(CARDS)
        self.assertTrue(md.startswith("## What's new\n\n- **New** · Pins: Pin things.\n"))
        self.assertIn('(Windows only): Fewer bugs.', md)

    def test_this_checkout_has_cards_for_its_version(self):
        version = release.read_version((ROOT / 'nexus' / '__init__.py').read_text())
        text = (ROOT / release.WHATS_NEW).read_text(encoding='utf-8')
        self.assertTrue(release.whats_new_cards(text, version))


class CiTest(unittest.TestCase):
    def test_latest_run_of_the_workflow_for_the_commit(self):
        runs = [tests_run(id_=1, created='2026-10-23T09:00:00Z'),
                tests_run(id_=2, created='2026-10-23T11:00:00Z'),
                tests_run(id_=3, created='2026-10-23T12:00:00Z', workflow='build-windows'),
                tests_run(id_=4, created='2026-10-23T13:00:00Z', sha='b' * 40)]
        self.assertEqual(release.latest_run(runs, 'tests', SHA)['databaseId'], 2)
        self.assertIsNone(release.latest_run(runs, 'tests', 'c' * 40))

    def test_passes_only_when_both_oses_passed(self):
        self.assertEqual(release.ci_verdict(tests_run(), GOOD_JOBS), '')
        cases = {
            'no tests run': (None, []),
            'still in_progress': (tests_run(status='in_progress', conclusion=''), GOOD_JOBS),
            'ended failure': (tests_run(conclusion='failure'), GOOD_JOBS),
            # continue-on-error: the run is green, the Windows job isn't
            "jobs didn't pass: windows": (tests_run(), [GOOD_JOBS[0], {
                'name': 'windows-latest / py3.12', 'conclusion': 'failure'}]),
            'no job ran on windows': (tests_run(), GOOD_JOBS[:1]),
        }
        for want, (run, jobs) in cases.items():
            with self.subTest(want):
                self.assertIn(want, release.ci_verdict(run, jobs))

    def test_build_run_prefers_the_tag_build(self):
        runs = [tests_run(id_=1, workflow='build-windows', branch='windows-port',
                          created='2026-10-23T12:00:00Z'),
                tests_run(id_=2, workflow='build-windows', branch='v0.3.0',
                          created='2026-10-23T10:00:00Z'),
                tests_run(id_=3, workflow='build-windows', branch='v0.3.0', conclusion='failure',
                          created='2026-10-23T13:00:00Z')]
        self.assertEqual(release.pick_build_run(runs, SHA, 'v0.3.0')['databaseId'], 2)
        self.assertEqual(release.pick_build_run(runs[:1], SHA, 'v0.3.0')['databaseId'], 1)
        self.assertIsNone(release.pick_build_run(runs[2:], SHA, 'v0.3.0'))


class GuardTest(unittest.TestCase):
    def test_what_counts_as_changing_a_remote(self):
        remote = [['git', 'push', 'origin', 'main'],
                  ['git', '-C', '/x', '-c', 'a=b', 'push', '--tags'],
                  ['git', '-C', '/x', 'tag', 'v1'],
                  ['gh', 'release', 'create', 'v1'], ['gh', 'release', 'upload', 'v1', 'f'],
                  ['gh', 'workflow', 'run', 'build'], ['gh', 'run', 'rerun', '1'],
                  ['gh', 'api', '-X', 'POST', 'repos/x/y/releases'],
                  ['gh', 'api', 'repos/x/y/git/refs', '-f', 'ref=refs/tags/v1'],
                  ['/usr/bin/gh', 'something', 'new']]
        local = [['git', '-C', '/x', 'commit', '-m', 'push it'], ['git', 'ls-remote', '--tags'],
                 ['git', 'rev-parse', 'refs/tags/v1'], ['git', 'clone', 'u', 'd'],
                 ['gh', 'run', 'list', '--commit', SHA], ['gh', 'run', 'view', '1'],
                 ['gh', 'run', 'download', '1'], ['gh', 'release', 'view', 'v1'],
                 ['gh', 'api', 'repos/x/y/commits'], ['python3', '-m', 'unittest']]
        for cmd in remote:
            with self.subTest(cmd=cmd):
                self.assertTrue(release.changes_remote(cmd))
        for cmd in local:
            with self.subTest(cmd=cmd):
                self.assertFalse(release.changes_remote(cmd))

    def test_dry_runner_refuses_before_running_anything(self):
        with mock.patch.object(release.subprocess, 'run') as run:
            for cmd in (('git', 'push', 'origin', 'main'), ('gh', 'release', 'create', 'v1')):
                with self.assertRaises(release.ReleaseError):
                    release.Runner(publish=False)(*cmd)
            run.assert_not_called()


class NotesTest(unittest.TestCase):
    def test_template_explains_smartscreen_and_links_the_guide(self):
        text = release.render_notes(
            release.NOTES_TEMPLATE.read_text(encoding='utf-8'), version='0.3.0', tag='v0.3.0',
            app='nexus-launcher', setup='S.exe', portable='P.zip', repo='o/r',
            guide='https://github.com/o/r/blob/v0.3.0/docs/WINDOWS.md', checksums='    abc  S.exe',
            whats_new="## What's new\n\n- one\n")
        self.assertTrue(text.startswith("## What's new"))
        for want in ('More info', 'Run anyway', 'code-signed', 'S.exe', 'P.zip',
                     'nexus-launcher.exe', '(https://github.com/o/r/blob/v0.3.0/docs/WINDOWS.md)',
                     'abc  S.exe'):
            self.assertIn(want, text)
        self.assertNotIn('$', text)

    def test_missing_value_is_an_error(self):
        with self.assertRaises(release.ReleaseError):
            release.render_notes('$version $nope', version='1')


class TreesTest(unittest.TestCase):
    def setUp(self):
        self.a = Path(tempfile.mkdtemp(prefix='nexus-tree-a-'))
        self.b = Path(tempfile.mkdtemp(prefix='nexus-tree-b-'))
        for d in (self.a, self.b):
            self.addCleanup(lambda d=d: __import__('shutil').rmtree(d, True))
            (d / 'sub').mkdir()
            (d / 'sub' / 'f.txt').write_text('same')
            (d / '.git').mkdir()
            (d / '.git' / 'HEAD').write_text(str(d))     # differs, ignored

    def test_identical(self):
        self.assertEqual(release.compare_trees(self.a, self.b), [])

    def test_extra_and_changed_files(self):
        (self.a / 'extra.py').write_text('x')
        (self.b / 'sub' / 'f.txt').write_text('other')
        diffs = release.compare_trees(self.a, self.b)
        self.assertEqual(len(diffs), 2)
        self.assertIn('extra.py', diffs[0])
        self.assertEqual(diffs[1], 'differs: sub/f.txt')

    @linux_only
    def test_exec_bit_counts(self):
        os.chmod(self.a / 'sub' / 'f.txt', 0o755)
        self.assertEqual(release.compare_trees(self.a, self.b), ['differs: sub/f.txt'])


# -- the whole dry run, against throwaway repos ---------------------------------

GIT_ID = ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
          '-c', 'commit.gpgsign=false']
REAL_RUN = subprocess.run


def git(repo, *args):
    return REAL_RUN(['git', '-C', str(repo), *GIT_ID, *args], check=True,
                    capture_output=True, text=True).stdout.strip()


class FakeGh:
    """Stands in for subprocess.run: records every command, runs git and
    python for real, answers gh from canned data."""

    def __init__(self, sha, tests='success', build=True):
        self.sha, self.tests, self.build = sha, tests, build
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if os.path.basename(cmd[0]) != 'gh':
            return REAL_RUN(cmd, **kw)
        out = ''
        if cmd[1:3] == ['run', 'list']:
            runs = [tests_run(sha=self.sha, conclusion=self.tests, id_=11)]
            if self.build:
                runs.append(tests_run(sha=self.sha, workflow='build-windows', branch='v0.3.0',
                                      id_=12))
            out = json.dumps(runs)
        elif cmd[1:3] == ['run', 'view']:
            out = json.dumps({'jobs': GOOD_JOBS})
        elif cmd[1:3] == ['run', 'download']:
            dest = Path(cmd[cmd.index('-D') + 1])
            dest.mkdir(parents=True, exist_ok=True)
            for name in release.asset_names('nexus-launcher', '0.3.0'):
                (dest / name).write_bytes(b'MZ fake build of ' + name.encode())
        else:
            raise AssertionError(f"unexpected gh call {cmd}")
        return subprocess.CompletedProcess(cmd, 0, out, '')

    def remote_changes(self):
        return [c for c in self.calls if release.changes_remote(c)]


@linux_only
class DryRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='nexus-release-test-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, True))
        src = self.src = self.tmp / 'src'
        (src / 'nexus' / 'platform').mkdir(parents=True)
        (src / 'tests').mkdir()
        (src / 'nexus' / '__init__.py').write_text('__version__ = "0.3.0"\n')
        (src / 'nexus' / 'platform' / 'windows.py').write_text("APP_NAME = 'nexus-launcher'\n")
        (src / 'tests' / 'test_ok.py').write_text(
            'import unittest\n\nclass T(unittest.TestCase):\n'
            '    def test_ok(self):\n        self.assertTrue(True)\n')
        (src / 'run.sh').write_text('#!/bin/sh\n')
        os.chmod(src / 'run.sh', 0o755)
        REAL_RUN(['git', 'init', '-q', '-b', 'windows-port', str(src)], check=True)
        git(src, 'add', '-A')
        git(src, 'commit', '-qm', 'first')
        first = git(src, 'rev-parse', 'HEAD')
        # nexus-launcher: an unrelated history whose tree is the first commit's
        seed = self.tmp / 'seed'
        REAL_RUN(['git', 'init', '-q', '-b', 'main', str(seed)], check=True)
        data = REAL_RUN(['git', '-C', str(src), 'archive', '--format=tar', first],
                        check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(seed, filter='tar')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-qm', 'Nexus, initial release')
        self.target = self.tmp / 'target.git'
        REAL_RUN(['git', 'clone', '-q', '--bare', str(seed), str(self.target)], check=True)
        self.target_head = git(self.target, 'rev-parse', 'main')
        # what's new since that export
        (src / 'nexus' / 'new.py').write_text('X = 1\n')
        (src / 'nexus' / 'data').mkdir()
        (src / 'nexus' / 'data' / 'whats_new.json').write_text(json.dumps({'0.3.0': CARDS}))
        git(src, 'add', '-A')
        git(src, 'commit', '-qm', 'second')
        self.rev = git(src, 'rev-parse', 'HEAD')
        self.gate = self.tmp / 'gate.txt'
        self.gate.write_text('# harmless test patterns\nzebra-quokka\n')
        self.out = []

    def dry_run(self, *extra, fake=None):
        self.fake = fake or FakeGh(self.rev)
        self.work = self.tmp / f'work{len(list(self.tmp.glob("work*")))}'
        argv = ['--source', str(self.src), '--target-url', str(self.target),
                '--gate', str(self.gate), '--workdir', str(self.work), *extra]
        with mock.patch.object(release.subprocess, 'run', self.fake):
            return release.main(argv, out=self.out.append)

    def assert_nothing_published(self):
        self.assertEqual(self.fake.remote_changes(), [])
        self.assertEqual(git(self.target, 'rev-parse', 'main'), self.target_head)
        self.assertEqual(git(self.target, 'tag'), '')

    def test_ready_dry_run_exports_commits_and_publishes_nothing(self):
        rc = self.dry_run()
        text = '\n'.join(self.out)
        self.assertEqual(rc, release.READY, text)
        self.assert_nothing_published()
        clone = self.work / 'nexus-launcher'
        self.assertEqual(git(clone, 'rev-parse', 'HEAD^{tree}'),
                         git(self.src, 'rev-parse', 'HEAD^{tree}'))
        self.assertEqual(git(clone, 'rev-parse', 'HEAD~1'), self.target_head)
        self.assertEqual(git(clone, 'log', '-1', '--format=%an <%ae>|%cn <%ce>'),
                         '{0} <{1}>|{0} <{1}>'.format(*release.IDENTITY))
        self.assertEqual(git(clone, 'log', '-1', '--format=%s'), 'Nexus 0.3.0')
        for want in ('identical', 'clean (6 files)', 'OK', '--publish would go ahead',
                     'push origin HEAD:refs/heads/main', 'gh release create v0.3.0',
                     'nexus-launcher-Setup-0.3.0.exe'):
            self.assertIn(want, text)
        notes = (self.work / 'release-notes.md').read_text()
        self.assertIn('Run anyway', notes)
        self.assertTrue(notes.startswith("## What's new\n\n- **New** · Pins: Pin things."))
        self.assertIn('nexus-launcher-0.3.0-portable.zip', notes)
        self.assertIn('blob/v0.3.0/docs/WINDOWS.md', notes)
        self.assertNotIn('zebra', text + notes)
        sums = (self.work / 'SHA256SUMS').read_text().splitlines()
        self.assertEqual([l.split('  ')[1] for l in sums],
                         list(release.asset_names('nexus-launcher', '0.3.0')))
        self.assertIn(sums[0].split()[0], notes)
        self.assertIn('SHA256SUMS', text)

    def test_no_whats_new_cards_means_not_ready(self):
        (self.src / 'nexus' / 'data' / 'whats_new.json').write_text('{"0.2.0": []}')
        git(self.src, 'commit', '-qam', 'no cards')
        self.rev = git(self.src, 'rev-parse', 'HEAD')
        rc = self.dry_run()
        self.assertEqual(rc, release.NOT_READY)
        self.assertIn("no What's new cards for 0.3.0", '\n'.join(self.out))
        self.assert_nothing_published()

    def test_red_ci_means_not_ready(self):
        rc = self.dry_run(fake=FakeGh(self.rev, tests='failure'))
        self.assertEqual(rc, release.NOT_READY)
        self.assertIn('--publish would REFUSE', '\n'.join(self.out))
        self.assert_nothing_published()

    def test_publish_refuses_without_the_build(self):
        rc = self.dry_run('--publish', '--yes', fake=FakeGh(self.rev, build=False))
        self.assertEqual(rc, release.FAILED)
        self.assertIn('refusing to publish', '\n'.join(self.out))
        self.assert_nothing_published()

    def test_gate_hit_stops_the_export(self):
        (self.src / 'tests' / 'fixture.txt').write_text('a\nby Zebra-Quokka\n')
        git(self.src, 'add', '-A')
        git(self.src, 'commit', '-qm', 'oops')
        rc = self.dry_run()
        text = '\n'.join(self.out)
        self.assertEqual(rc, release.FAILED)
        self.assertIn('tests/fixture.txt:2 (gate line 2)', text)
        self.assertNotIn('quokka', text.lower())
        self.assert_nothing_published()

    def test_no_gate_file_no_release(self):
        self.gate.unlink()
        rc = self.dry_run()
        self.assertEqual(rc, release.FAILED)
        self.assertIn('no gate file', '\n'.join(self.out))
        self.assertEqual(self.fake.calls, [])

    def test_clone_must_match_the_last_export(self):
        rc = self.dry_run('--last-exported', 'HEAD')   # wrong on purpose: HEAD~1 was
        self.assertEqual(rc, release.FAILED)
        self.assertIn("isn't the last export", '\n'.join(self.out))
        self.assert_nothing_published()


if __name__ == '__main__':
    unittest.main()
