"""NEXUS_PROFILE: a dev checkout next to the installed release, with its own
folders, socket, app-id and agent unit (nexus/platform/profile.py). Each case
runs in a child process because the profile is read at import time."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tests.sandbox  # noqa: F401  (HOME is a temp dir from here on)

REPO = Path(__file__).resolve().parent.parent

PROBE = """
import json
from nexus import paths as NX, platform, instance, winfocus
from nexus.cli import agent
print(json.dumps({
    'profile': platform.PROFILE, 'app_id': NX.APP_ID,
    'data': str(NX.DATA_DIR), 'config': str(NX.CONFIG_DIR), 'cache': str(NX.CACHE_DIR),
    'sock': instance.INSTANCE, 'unit': agent.UNIT_NAME, 'unit_text': agent.unit_text(),
    'own': winfocus.OWN_APP_ID, 'migrated': NX.migrate(quiet=True),
}))
"""


def probe(profile: str | None, argv0: str | None = None) -> dict:
    home = tempfile.mkdtemp()
    env = {k: v for k, v in os.environ.items()
           if k not in ('NEXUS_PROFILE', 'NEXUS_INSTANCE') and not k.startswith('XDG_')}
    env.update(HOME=home, XDG_RUNTIME_DIR=home, PYTHONPATH=str(REPO))
    if profile is not None:
        env['NEXUS_PROFILE'] = profile
    (Path(home) / '.local/share/fitgirl-install').mkdir(parents=True)   # a legacy dir
    out = subprocess.run([sys.executable, '-c', PROBE], env=env, cwd=home,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) | {'home': home}


@unittest.skipUnless(sys.platform.startswith('linux'), "Linux folder names")
class Profile(unittest.TestCase):
    def test_default_is_unchanged(self):
        p = probe(None)
        self.assertEqual((p['profile'], p['app_id'], p['own']), ('', 'nexus', 'nexus'))
        self.assertEqual(p['data'], f"{p['home']}/.local/share/nexus")
        self.assertEqual(p['sock'], f"{p['home']}/nexus.sock")
        self.assertEqual(p['unit'], 'nexus-agent.service')
        self.assertNotIn('NEXUS_PROFILE', p['unit_text'])
        self.assertTrue(p['migrated'])                # the main copy still migrates

    def test_dev_profile_gets_its_own_everything(self):
        p = probe('dev')
        self.assertEqual((p['profile'], p['app_id'], p['own']), ('dev', 'nexus-dev', 'nexus-dev'))
        self.assertEqual(p['data'], f"{p['home']}/.local/share/nexus-dev")
        self.assertEqual(p['config'], f"{p['home']}/.config/nexus-dev")
        self.assertEqual(p['cache'], f"{p['home']}/.cache/nexus-dev")
        self.assertEqual(p['sock'], f"{p['home']}/nexus-dev.sock")
        self.assertEqual(p['unit'], 'nexus-dev-agent.service')
        self.assertIn('Environment=NEXUS_PROFILE=dev', p['unit_text'])
        self.assertEqual(p['migrated'], [])           # legacy dirs are the main copy's

    def test_bad_profile_names_are_ignored(self):
        for bad in ('../x', 'Dev Build', 'a' * 17, ''):
            self.assertEqual(probe(bad)['app_id'], 'nexus', bad)

    def test_stub_name_picks_the_profile(self):
        for stub, link in (('nexus-cli', 'nexus-cli-dev'), ('nexus', 'nexus-dev')):
            home = Path(tempfile.mkdtemp())
            (home / link).symlink_to(REPO / 'bin' / stub)
            env = {k: v for k, v in os.environ.items() if k != 'NEXUS_PROFILE'}
            code = ("import runpy, sys, os; sys.argv=[sys.argv[1]]; "
                    "import builtins; src=open(sys.argv[0]).read()"
                    ".replace('main()', 'print(os.environ.get(\"NEXUS_PROFILE\"))'); "
                    "exec(compile(src, sys.argv[0], 'exec'), {'__file__': sys.argv[0]})")
            out = subprocess.run([sys.executable, '-c', code, str(home / link)], env=env,
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(out.stdout.strip().splitlines()[-1:], ['dev'], out.stderr)


if __name__ == '__main__':
    unittest.main()
