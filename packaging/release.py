#!/usr/bin/env python3
"""Release Nexus from the nexus-launcher repo. A DRY RUN unless --publish.

Nexus is developed in nexus-launcher itself (branch main), so releasing HEAD
of main exports nothing new: step 7 finds main already has the tree. The
export steps stay so that a commit made elsewhere (an older checkout, a
branch) is still copied in as one snapshot on top of main, never with its
own history.

    python3 packaging/release.py                 # dry run of HEAD
    python3 packaging/release.py --rev v0.3.0    # dry run of a tag
    python3 packaging/release.py --publish       # the real thing

Steps, all in a scratch directory (--workdir, kept afterwards):

  1. clone nexus-launcher main;
  2. check the clone == `git archive` of the last exported commit (found by
     tree hash, or --last-exported);
  3. `git rm -rq .` and copy `git archive <rev>` in;
  4. prove the trees are identical (file by file, and by tree hash);
  5. the personal-data gate: every tracked file, fixtures included, and every
     path must match none of the patterns in the gate file;
  6. run the unit suite in the clone (Linux features);
  7. commit as the noreply identity;
  then check what publishing needs: the tests workflow passed for <rev> on
  both OSes, the tag is free, and the Windows assets are at hand (from the
  build-windows run for <rev>, or --assets).

A dry run stops there and prints what --publish would do. --publish goes on:

  8. push main, push tag v<version>, create the release with the assets
     (the two Windows builds and their SHA256SUMS);
  9. verify from a fresh clone;
 10. announce it on an ntfy topic, if one is set (--ntfy, else
     $NEXUS_RELEASE_NTFY): best-effort, a failed push doesn't undo anything.

The gate's patterns live OUTSIDE the repo (an exported tree that held them
would fail its own gate): one regex per line, case-insensitive, '#' comments
allowed. Default ~/.config/nexus-release/gate.txt, else $NEXUS_RELEASE_GATE or
--gate. No gate file, no release, dry run included.

Linux, stdlib only; needs git, and gh logged in for the CI and asset checks.
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import string
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

TARGET_REPO = 'cyberpunk89/nexus-launcher'
TARGET_BRANCH = 'main'
SOURCE_REPO = TARGET_REPO       # where CI runs and the Windows build is made
IDENTITY = ('cyberpunk89', '25316058+cyberpunk89@users.noreply.github.com')

GATE_ENV = 'NEXUS_RELEASE_GATE'
NTFY_ENV = 'NEXUS_RELEASE_NTFY'     # e.g. https://ntfy.sh/<the friends' topic>
GATE_DEFAULT = '~/.config/nexus-release/gate.txt'

TESTS_WORKFLOW = 'tests'            # the `name:` of .github/workflows/tests.yml
BUILD_WORKFLOW = 'build-windows'    # ... and of build-windows.yml
REQUIRED_OSES = ('ubuntu', 'windows')   # job names start with the runner image
SUITE_ENV = {'QT_QPA_PLATFORM': 'offscreen', 'NEXUS_NO_GAMEPAD': '1',
             'PYTHONDONTWRITEBYTECODE': '1'}    # leave the clone as it was

NOTES_TEMPLATE = Path(__file__).with_name('release-notes.md')
VERSION_RE = re.compile(r'^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$')

READY, FAILED, NOT_READY = 0, 1, 3      # exit codes


class ReleaseError(Exception):
    """A step failed or a check refused: the message says which."""


# -- pure parts ---------------------------------------------------------------

def gate_path(arg=None, environ=os.environ) -> Path:
    """--gate, else $NEXUS_RELEASE_GATE, else the default under ~/.config."""
    return Path(arg or environ.get(GATE_ENV) or GATE_DEFAULT).expanduser()


def load_gate(path) -> list:
    """The gate's compiled patterns, as (line number, regex). Refuses when the
    file is missing, unreadable, empty or holds a bad regex. Errors name a
    line number, never a pattern: the patterns are the personal data."""
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        raise ReleaseError(
            f"no gate file at {path}: refusing to release without the personal-data "
            f"gate. Create it (one regex per line) or point --gate / ${GATE_ENV} at it.")
    except OSError as e:
        raise ReleaseError(f"can't read the gate file {path}: {e.strerror}")
    patterns = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            patterns.append((n, re.compile(line, re.IGNORECASE)))
        except re.error as e:
            raise ReleaseError(f"gate file {path}, line {n}: not a valid regex ({e.msg})")
    if not patterns:
        raise ReleaseError(f"gate file {path} has no patterns: an empty gate passes "
                           f"everything, refusing")
    return patterns


def gate_hits(files, patterns) -> list:
    """Where `patterns` match. `files` yields (path, bytes). Returns lines like
    'nexus/x.py:12 (gate line 3)' or 'a/b.txt (file name, gate line 1)', never
    the matched text. Bytes are read as latin-1 so binaries are searched too."""
    hits = []
    for path, data in files:
        text = data.decode('latin-1')
        for n, rx in patterns:
            if rx.search(path):
                hits.append(f"{path} (file name, gate line {n})")
            for m in rx.finditer(text):
                hits.append(f"{path}:{text.count(chr(10), 0, m.start()) + 1} (gate line {n})")
    return hits


def ntfy_url(arg=None, environ=os.environ):
    """--ntfy, else $NEXUS_RELEASE_NTFY; None when neither is set. Only https:
    the topic name is the only thing keeping a public topic private."""
    url = (arg or environ.get(NTFY_ENV) or '').strip()
    if url and not url.startswith('https://'):
        raise ReleaseError(f"the ntfy URL must start with https:// (from --ntfy or ${NTFY_ENV})")
    return url or None


def read_version(init_text: str) -> str:
    m = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", init_text, re.M)
    if not m:
        raise ReleaseError("no __version__ in nexus/__init__.py")
    return m.group(1)


def read_app_name(windows_py: str) -> str:
    m = re.search(r"^APP_NAME\s*=\s*['\"]([^'\"]+)['\"]", windows_py, re.M)
    if not m:
        raise ReleaseError("no APP_NAME in nexus/platform/windows.py")
    return m.group(1)


def check_version(version: str) -> str:
    """The tag for `version` (v0.3.0 for 0.3.0); refuses odd versions."""
    if not VERSION_RE.match(version):
        raise ReleaseError(f"__version__ {version!r} isn't X.Y.Z (optionally aN/bN/rcN)")
    return f"v{version}"


def asset_names(app: str, version: str) -> tuple:
    """What build-windows.yml produces: (installer, portable zip)."""
    return f"{app}-Setup-{version}.exe", f"{app}-{version}-portable.zip"


SUMS_NAME = 'SHA256SUMS'


def sha256sums(paths) -> str:
    """`sha256sum` format, so `sha256sum -c SHA256SUMS` (or certutil by hand)
    checks a download."""
    return ''.join(f"{sha256(p)}  {Path(p).name}\n" for p in paths)


def artifact_name(app: str, version: str) -> str:
    """The workflow artifact holding both assets."""
    return f"{app}-{version}-installers"


def latest_run(runs, workflow: str, sha: str):
    """The newest run of `workflow` for commit `sha` in `gh run list --json`
    output, or None."""
    mine = [r for r in runs if r.get('workflowName') == workflow and r.get('headSha') == sha]
    return max(mine, key=lambda r: r.get('createdAt', ''), default=None)


def ci_verdict(run, jobs) -> str:
    """'' when the tests run passed on every required OS, else why not. Checks
    each job, not just the run: a job with continue-on-error can fail inside a
    green run."""
    if run is None:
        return f"no {TESTS_WORKFLOW} run for this commit (is it pushed?)"
    if run.get('status') != 'completed':
        return f"the {TESTS_WORKFLOW} run {run.get('databaseId')} is still {run.get('status')}"
    if run.get('conclusion') != 'success':
        return f"the {TESTS_WORKFLOW} run {run.get('databaseId')} ended {run.get('conclusion')}"
    bad = [j['name'] for j in jobs if j.get('conclusion') != 'success']
    if bad:
        return "jobs didn't pass: " + ', '.join(bad)
    missing = [os_ for os_ in REQUIRED_OSES
               if not any(j.get('name', '').startswith(os_) for j in jobs)]
    if missing:
        return "no job ran on " + ', '.join(missing)
    return ''


def pick_build_run(runs, sha: str, tag: str):
    """The successful build-windows run for `sha`: the tag build if there is
    one, else the newest (a push to packaging/ builds the same commit)."""
    ok = [r for r in runs if r.get('workflowName') == BUILD_WORKFLOW and r.get('headSha') == sha
          and r.get('status') == 'completed' and r.get('conclusion') == 'success']
    tagged = [r for r in ok if r.get('headBranch') == tag]
    return max(tagged or ok, key=lambda r: r.get('createdAt', ''), default=None)


def render_notes(template: str, **values) -> str:
    """The release notes: string.Template, so Markdown's braces are safe and a
    missing value is an error rather than a hole."""
    try:
        return string.Template(template).substitute(values)
    except KeyError as e:
        raise ReleaseError(f"release notes template needs ${e.args[0]}")


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def snapshot(root, ignore=('.git',)) -> dict:
    """{relative path: ('link', target) | ('file', executable, sha256)} for
    every file under `root`. Directories don't count: git doesn't track them."""
    root = Path(root)
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in ignore or Path(dirpath) != root)
        for name in list(filenames) + [d for d in dirnames
                                       if os.path.islink(os.path.join(dirpath, d))]:
            p = Path(dirpath, name)
            rel = p.relative_to(root).as_posix()
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ('link', os.readlink(p))
            else:
                out[rel] = ('file', bool(st.st_mode & 0o111), sha256(p))
    return out


def compare_trees(a, b) -> list:
    """What `diff -r` would complain about, plus exec bits and symlinks; []
    when the trees are identical (.git aside)."""
    sa, sb = snapshot(a), snapshot(b)
    diffs = [f"only in {a}: {p}" for p in sorted(sa.keys() - sb.keys())]
    diffs += [f"only in {b}: {p}" for p in sorted(sb.keys() - sa.keys())]
    diffs += [f"differs: {p}" for p in sorted(sa.keys() & sb.keys()) if sa[p] != sb[p]]
    return diffs


def git_subcommand(cmd):
    """'push' for ['git', '-C', 'x', '-c', 'k=v', 'push', ...]."""
    args = list(cmd[1:])
    while args:
        a = args.pop(0)
        if a in ('-C', '-c', '--git-dir', '--work-tree'):
            args = args[1:]
        elif not a.startswith('-'):
            return a
    return None


GH_READ_ONLY = {('run', 'list'), ('run', 'view'), ('run', 'download'), ('release', 'list'),
                ('release', 'view'), ('repo', 'view'), ('repo', 'clone'), ('auth', 'status')}


def changes_remote(cmd) -> bool:
    """True for the commands only --publish may run: git push and git tag, and
    any gh command that isn't known to be read-only (an allowlist, so a new
    call has to be thought about before a dry run may make it)."""
    cmd = [str(c) for c in cmd]
    if not cmd:
        return False
    tool = os.path.basename(cmd[0])
    if tool == 'git':
        return git_subcommand(cmd) in ('push', 'tag')
    if tool == 'gh':
        if cmd[1:2] == ['api']:
            method = [cmd[i + 1].upper() for i, c in enumerate(cmd[:-1])
                      if c in ('-X', '--method')]
            return (bool(method) and method[-1] != 'GET') or any(
                c in ('-f', '-F', '--field', '--raw-field', '--input') for c in cmd)
        return tuple(cmd[1:3]) not in GH_READ_ONLY
    return False


# -- the run -------------------------------------------------------------------

class Runner:
    """subprocess.run with captured output. Outside --publish it refuses
    anything changes_remote() flags, so a dry run can't push even by mistake."""

    def __init__(self, publish=False, log=print):
        self.publish = publish
        self.log = log

    def __call__(self, *cmd, cwd=None, env=None, check=True, text=True):
        cmd = [str(c) for c in cmd]
        if changes_remote(cmd) and not self.publish:
            raise ReleaseError("dry run refused to run: " + ' '.join(cmd))
        full_env = None if env is None else {**os.environ, **env}
        r = subprocess.run(cmd, cwd=cwd, env=full_env, capture_output=True, text=text)
        if check and r.returncode:
            err = r.stderr if text else r.stderr.decode('utf-8', 'replace')
            raise ReleaseError(f"{' '.join(cmd[:4])} ... exited {r.returncode}:\n"
                               + '\n'.join(err.strip().splitlines()[-15:]))
        return r


class Release:
    def __init__(self, args, run=None, out=print):
        self.a = args
        self.out = out
        self.run = run or Runner(args.publish, out)
        self.src = Path(args.source).resolve()
        self.blockers = []          # why --publish would refuse
        self.assets = []

    # helpers
    def git(self, *args, repo=None, **kw):
        return self.run('git', '-C', repo or self.src, *args, **kw)

    def say(self, msg=''):
        self.out(msg)

    def step(self, n, msg):
        self.out(f"[{n}] {msg}")

    def block(self, why):
        self.blockers.append(why)
        self.out(f"    ✗ {why}")

    def extract(self, rev, dest):
        """`git archive <rev>` unpacked into `dest`."""
        data = self.git('archive', '--format=tar', rev, text=False).stdout
        Path(dest).mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            try:
                tf.extractall(dest, filter='tar')
            except TypeError:       # Python without extraction filters
                tf.extractall(dest)

    def gh_json(self, *args):
        return json.loads(self.run('gh', *args).stdout or 'null')

    # the steps
    def prepare(self):
        a = self.a
        self.patterns = load_gate(gate_path(a.gate))
        self.rev = self.git('rev-parse', '--verify', f"{a.rev}^{{commit}}").stdout.strip()
        show = lambda p: self.git('show', f"{self.rev}:{p}").stdout
        self.version = read_version(show('nexus/__init__.py'))
        self.tag = check_version(self.version)
        self.app = read_app_name(show('nexus/platform/windows.py'))
        self.message = a.message or f"Nexus {self.version}"
        self.ntfy = ntfy_url(a.ntfy)
        self.work = Path(a.workdir or tempfile.mkdtemp(prefix='nexus-release-')).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        if any(self.work.iterdir()):
            raise ReleaseError(f"--workdir {self.work} isn't empty")
        self.clone = self.work / 'nexus-launcher'
        self.say(f"Nexus {self.version} ({self.tag}), app {self.app}: exporting "
                 f"{self.rev[:12]} ({a.rev}) of {self.src}")
        self.say(f"    mode: {'PUBLISH' if a.publish else 'dry run'}; scratch: {self.work}")
        self.say(f"    gate: {len(self.patterns)} patterns from {gate_path(a.gate)}")
        if a.rev == 'HEAD' and self.git('status', '--porcelain').stdout.strip():
            self.say("    note: the checkout has uncommitted changes; they are NOT exported")

    def clone_target(self):
        url = self.a.target_url or f"https://github.com/{self.a.target}.git"
        self.step(1, f"clone {url} ({TARGET_BRANCH})")
        self.run('git', 'clone', '--quiet', '--branch', TARGET_BRANCH, url, self.clone)
        self.old_head = self.git('rev-parse', 'HEAD', repo=self.clone).stdout.strip()

    def check_last_export(self):
        tree = self.git('rev-parse', 'HEAD^{tree}', repo=self.clone).stdout.strip()
        last = self.a.last_exported
        if last:
            last = self.git('rev-parse', '--verify', f"{last}^{{commit}}").stdout.strip()
        else:
            for line in self.git('log', '--all', '--format=%H %T').stdout.splitlines():
                commit, t = line.split()
                if t == tree:
                    last = commit
                    break
        if not last:
            raise ReleaseError(
                f"{TARGET_BRANCH} {self.old_head[:12]} matches no commit here by tree "
                f"hash: it was changed outside this script, or the export is from "
                f"elsewhere. Check it and pass --last-exported REV.")
        self.step(2, f"{TARGET_BRANCH} {self.old_head[:12]} == git archive {last[:12]}?")
        prev = self.work / 'last-export'
        self.extract(last, prev)
        diffs = compare_trees(self.clone, prev)
        if diffs:
            raise ReleaseError("the clone isn't the last export:\n    " + '\n    '.join(diffs[:20]))
        if self.git('merge-base', '--is-ancestor', last, self.rev, check=False).returncode:
            self.say(f"    note: {last[:12]} isn't an ancestor of {self.rev[:12]}")
        self.say("    identical")
        self.last = last

    def copy_in(self):
        self.step(3, f"git rm -rq . and copy in git archive {self.rev[:12]}")
        if self.git('ls-files', repo=self.clone).stdout.strip():
            self.git('rm', '-rq', '.', repo=self.clone)
        self.extract(self.rev, self.clone)
        self.git('add', '-A', '.', repo=self.clone)

    def prove_identical(self):
        self.step(4, "diff the clone against a fresh git archive")
        self.fresh = self.work / 'archive'
        self.extract(self.rev, self.fresh)
        diffs = compare_trees(self.clone, self.fresh)
        if diffs:
            raise ReleaseError("the copy differs from the archive:\n    " + '\n    '.join(diffs[:20]))
        index_tree = self.git('write-tree', repo=self.clone).stdout.strip()
        want = self.git('rev-parse', f"{self.rev}^{{tree}}").stdout.strip()
        if index_tree != want:
            raise ReleaseError(f"staged tree {index_tree[:12]} != {self.rev[:12]}'s tree {want[:12]}")
        self.say(f"    identical, tree {want[:12]}")

    def gate(self):
        self.step(5, "personal-data gate (every tracked file and path, and the message)")
        names = self.git('ls-files', '-z', repo=self.clone).stdout.split('\0')
        files = ((n, (self.clone / n).read_bytes() if not (self.clone / n).is_symlink()
                  else os.readlink(self.clone / n).encode()) for n in names if n)
        hits = gate_hits(files, self.patterns)
        hits += gate_hits([('commit message', self.message.encode())], self.patterns)
        if hits:
            raise ReleaseError(f"the gate matched {len(hits)} time(s):\n    "
                               + '\n    '.join(hits[:50]))
        self.say(f"    clean ({len([n for n in names if n])} files)")

    def suite(self):
        if self.a.skip_suite:
            self.step(6, "unit suite: SKIPPED (--skip-suite)")
            self.block("the suite was skipped (--skip-suite)")
            return
        self.step(6, "unit suite in the clone (this takes a minute)")
        r = self.run(sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
                     cwd=self.clone, env=SUITE_ENV, check=False)
        (self.work / 'suite-output.txt').write_text(r.stdout + r.stderr, encoding='utf-8')
        tail = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()][-2:]
        self.say("    " + ' / '.join(tail))
        if r.returncode:
            raise ReleaseError(f"the suite failed in the clone: see {self.work / 'suite-output.txt'}")
        dirty = (self.git('diff', '--name-only', repo=self.clone).stdout
                 + self.git('ls-files', '--others', '--exclude-standard',
                            repo=self.clone).stdout).strip()
        if dirty:
            raise ReleaseError("the suite changed files in the clone:\n" + dirty)

    def commit(self):
        name, email = IDENTITY
        if self.git('diff', '--cached', '--quiet', repo=self.clone, check=False).returncode == 0:
            self.step(7, f"commit: nothing to commit, {TARGET_BRANCH} already has this tree")
            self.new_head = self.old_head
            return
        self.step(7, f"commit as {name} <{email}>: {self.message!r}")
        env = {'GIT_AUTHOR_NAME': name, 'GIT_AUTHOR_EMAIL': email,
               'GIT_COMMITTER_NAME': name, 'GIT_COMMITTER_EMAIL': email}
        self.git('-c', 'commit.gpgsign=false', 'commit', '-q', '-m', self.message,
                 repo=self.clone, env=env)
        self.new_head = self.git('rev-parse', 'HEAD', repo=self.clone).stdout.strip()
        who = self.git('log', '-1', '--format=%an <%ae>|%cn <%ce>', repo=self.clone).stdout.strip()
        if who != f"{name} <{email}>|{name} <{email}>":
            raise ReleaseError(f"the commit's identity came out as {who}")
        self.say(f"    {self.new_head[:12]} on top of {self.old_head[:12]}")

    # what publishing needs
    def check_ci(self):
        self.say(f"[ci] {TESTS_WORKFLOW} for {self.rev[:12]} on {self.a.source_repo}")
        try:
            runs = self.gh_json('run', 'list', '-R', self.a.source_repo, '--commit', self.rev,
                                '-L', '50', '--json',
                                'databaseId,workflowName,headSha,headBranch,status,conclusion,createdAt')
            run = latest_run(runs or [], TESTS_WORKFLOW, self.rev)
            jobs = [] if run is None else self.gh_json(
                'run', 'view', str(run['databaseId']), '-R', self.a.source_repo,
                '--json', 'jobs')['jobs']
        except (ReleaseError, ValueError, KeyError) as e:
            return self.block(f"couldn't read CI status: {e}")
        self.build_runs = runs or []
        why = ci_verdict(run, jobs)
        if why:
            return self.block(why)
        self.say(f"    ✓ run {run['databaseId']} passed: " + ', '.join(j['name'] for j in jobs))

    def check_tag(self):
        self.say(f"[tag] {self.tag}")
        remote = self.git('ls-remote', '--tags', 'origin', f"refs/tags/{self.tag}",
                          repo=self.clone).stdout.strip()
        if remote:
            self.block(f"{self.tag} already exists on {self.a.target}: bump __version__")
        else:
            self.say(f"    ✓ free on {self.a.target}")
        local = self.git('rev-parse', '-q', '--verify', f"refs/tags/{self.tag}^{{commit}}",
                         check=False).stdout.strip()
        if local and local != self.rev:
            self.block(f"{self.tag} here points at {local[:12]}, not {self.rev[:12]}")
        elif local:
            self.say(f"    ✓ {self.tag} here points at {self.rev[:12]}")
        else:
            self.say(f"    note: no {self.tag} tag here yet (pushing one runs the tag build)")

    def find_assets(self):
        want = asset_names(self.app, self.version)
        self.say("[assets] " + ', '.join(want))
        if self.a.assets:
            paths = [Path(p).expanduser().resolve() for p in self.a.assets]
            self.say("    from --assets")
        else:
            run = None
            if self.a.build_run:
                run = {'databaseId': self.a.build_run}
            else:
                run = pick_build_run(getattr(self, 'build_runs', []), self.rev, self.tag)
            if run is None:
                return self.block(f"no successful {BUILD_WORKFLOW} run for {self.rev[:12]}: "
                                  f"push {self.tag} (or a packaging change), or pass --assets")
            dest = self.work / 'assets'
            self.say(f"    gh run download {run['databaseId']} "
                     f"(artifact {artifact_name(self.app, self.version)})")
            try:
                self.run('gh', 'run', 'download', str(run['databaseId']), '-R', self.a.source_repo,
                         '-n', artifact_name(self.app, self.version), '-D', dest)
            except ReleaseError as e:
                return self.block(f"download failed: {e}")
            paths = [dest / n for n in want]
        names = [p.name for p in paths]
        if sorted(names) != sorted(want):
            return self.block(f"assets are {names}, expected {list(want)}")
        missing = [str(p) for p in paths if not p.is_file() or not p.stat().st_size]
        if missing:
            return self.block("missing or empty: " + ', '.join(missing))
        builds = sorted(paths, key=lambda p: want.index(p.name))
        for p in builds:
            self.say(f"    ✓ {p} ({p.stat().st_size / 1e6:.1f} MB)")
        sums = self.work / SUMS_NAME
        sums.write_text(sha256sums(builds), encoding='utf-8')
        self.say(f"    ✓ {sums}")
        self.assets = builds + [sums]

    def notes(self):
        setup, portable = asset_names(self.app, self.version)
        builds = [p for p in self.assets if p.name != SUMS_NAME]
        if builds:
            sums = '\n'.join(f"    {line}" for line in sha256sums(builds).splitlines())
        else:
            sums = "    (no assets yet)"
        text = render_notes(
            NOTES_TEMPLATE.read_text(encoding='utf-8'), version=self.version, tag=self.tag,
            app=self.app, setup=setup, portable=portable, repo=self.a.target,
            guide=f"https://github.com/{self.a.target}/blob/{self.tag}/docs/WINDOWS.md",
            checksums=sums)
        hits = gate_hits([('release notes', text.encode())], self.patterns)
        if hits:
            raise ReleaseError("the gate matched the release notes:\n    " + '\n    '.join(hits))
        self.notes_file = self.work / 'release-notes.md'
        self.notes_file.write_text(text, encoding='utf-8')

    def plan(self):
        git = f"git -C {self.clone}"
        cmds = [f"{git} push origin HEAD:refs/heads/{TARGET_BRANCH}"
                + ("   (nothing new)" if self.new_head == self.old_head else ''),
                f"{git} tag {self.tag} {self.new_head[:12]}",
                f"{git} push origin refs/tags/{self.tag}",
                f"gh release create {self.tag} -R {self.a.target} --verify-tag "
                f"--title 'Nexus {self.version}' --notes-file {self.notes_file} "
                + ' '.join(p.name for p in self.assets),
                f"then: fresh clone == {self.rev[:12]}, {self.tag} -> {self.new_head[:12]}, "
                f"release lists every asset",
                f"announce on ntfy ({'set' if self.ntfy else f'not set: --ntfy or ${NTFY_ENV}'})"]
        return cmds

    # 8 and 9
    def publish(self):
        if self.blockers:
            raise ReleaseError("refusing to publish:\n    " + '\n    '.join(self.blockers))
        if not self.a.yes:
            self.say(f"\nPublish Nexus {self.version} to {self.a.target}? Type 'publish': ")
            if input().strip() != 'publish':
                raise ReleaseError("not confirmed, nothing published")
        self.step(8, f"push {TARGET_BRANCH}, tag {self.tag}, create the release")
        self.git('push', 'origin', f"HEAD:refs/heads/{TARGET_BRANCH}", repo=self.clone)
        self.git('tag', self.tag, self.new_head, repo=self.clone)
        self.git('push', 'origin', f"refs/tags/{self.tag}", repo=self.clone)
        self.run('gh', 'release', 'create', self.tag, '-R', self.a.target, '--verify-tag',
                 '--title', f"Nexus {self.version}", '--notes-file', self.notes_file,
                 *self.assets)
        self.verify()
        self.announce()

    def verify(self):
        url = self.a.target_url or f"https://github.com/{self.a.target}.git"
        self.step(9, "verify from a fresh clone")
        check = self.work / 'verify'
        self.run('git', 'clone', '--quiet', '--branch', TARGET_BRANCH, url, check)
        problems = compare_trees(check, self.fresh)
        head = self.git('rev-parse', 'HEAD', repo=check).stdout.strip()
        if head != self.new_head:
            problems.append(f"{TARGET_BRANCH} is {head[:12]}, not {self.new_head[:12]}")
        tag = self.git('ls-remote', 'origin', f"refs/tags/{self.tag}", repo=check).stdout.split()
        if not tag or tag[0] != self.new_head:
            problems.append(f"{self.tag} doesn't point at {self.new_head[:12]}")
        rel = self.gh_json('release', 'view', self.tag, '-R', self.a.target, '--json', 'assets')
        have = {x['name'] for x in rel.get('assets', [])}
        problems += [f"release lacks {p.name}" for p in self.assets if p.name not in have]
        if problems:
            raise ReleaseError("published, but the check found:\n    " + '\n    '.join(problems))
        self.say(f"    ✓ {self.a.target} {TARGET_BRANCH} {self.new_head[:12]} == {self.rev[:12]}, "
                 f"{self.tag} released with {len(self.assets)} assets")

    def announce(self):
        if not self.ntfy:
            self.say(f"[10] no ntfy topic (--ntfy or ${NTFY_ENV}): nobody was told")
            return
        self.step(10, "announce on ntfy")
        link = f"https://github.com/{self.a.target}/releases/tag/{self.tag}"
        req = urllib.request.Request(
            self.ntfy, data=f"Nexus {self.version} is out: {link}".encode(), method='POST',
            headers={'Title': f"Nexus {self.version} released", 'Tags': 'package',
                     'Click': link})
        try:
            with urllib.request.urlopen(req, timeout=15):
                pass
        except OSError as e:           # URLError, HTTPError and timeouts
            self.say(f"    ✗ ntfy push failed ({e}); the release itself is done")
            return
        self.say("    ✓ sent")

    def main(self) -> int:
        self.prepare()
        self.clone_target()
        self.check_last_export()
        self.copy_in()
        self.prove_identical()
        self.gate()
        self.suite()
        self.commit()
        self.say("\nWhat publishing needs:")
        self.check_ci()
        self.check_tag()
        self.find_assets()
        self.notes()
        if self.a.publish:
            self.publish()
            return READY
        self.say("\nDry run: nothing was pushed, tagged or released. --publish would run:")
        for c in self.plan():
            self.say("    " + c)
        self.say(f"Release notes: {self.notes_file}")
        if self.blockers:
            self.say("\n--publish would REFUSE:\n    " + '\n    '.join(self.blockers))
            return NOT_READY
        self.say("\n--publish would go ahead.")
        return READY


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Export a commit to nexus-launcher and release it. Dry run by default.",
        epilog=f"Exit codes: {READY} ready/published, {FAILED} a step failed, "
               f"{NOT_READY} dry run fine but --publish would refuse.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', dest='publish', action='store_false', default=False,
                      help="do everything up to the push in a scratch dir (the default)")
    mode.add_argument('--publish', action='store_true',
                      help="push main, tag and create the release")
    p.add_argument('--rev', default='HEAD', help="what to export (default HEAD)")
    p.add_argument('--gate', help=f"the gate's pattern file (else ${GATE_ENV}, else {GATE_DEFAULT})")
    p.add_argument('--last-exported', metavar='REV',
                   help="the commit nexus-launcher main was exported from (default: found by tree hash)")
    p.add_argument('--assets', nargs=2, metavar=('SETUP_EXE', 'PORTABLE_ZIP'),
                   help="local Windows assets instead of the build-windows run's")
    p.add_argument('--build-run', metavar='ID', help="take the assets from this workflow run")
    p.add_argument('--message', '-m', help="the export commit's message (default 'Nexus <version>')")
    p.add_argument('--workdir', help="scratch directory, must be empty (default: a new temp dir)")
    p.add_argument('--skip-suite', action='store_true',
                   help="don't run the unit suite in the clone (dry run only: publishing refuses)")
    p.add_argument('--yes', action='store_true', help="don't ask before publishing")
    p.add_argument('--ntfy', metavar='URL',
                   help=f"announce a published release on this ntfy topic (else ${NTFY_ENV})")
    p.add_argument('--source', default=str(Path(__file__).resolve().parent.parent),
                   help=argparse.SUPPRESS)
    p.add_argument('--source-repo', default=SOURCE_REPO, help=f"for CI and assets (default {SOURCE_REPO})")
    p.add_argument('--target', default=TARGET_REPO, help=f"(default {TARGET_REPO})")
    p.add_argument('--target-url', help=argparse.SUPPRESS)     # tests clone a local repo
    return p.parse_args(argv)


def main(argv=None, run=None, out=print) -> int:
    args = parse_args(argv)
    try:
        return Release(args, run=run, out=out).main()
    except ReleaseError as e:
        out(f"\nrelease: {e}")
        return FAILED


if __name__ == '__main__':
    sys.exit(main())
