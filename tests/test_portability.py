"""Keeps the shared modules portable. Both apps use these, and nothing stops a
Linux change from quietly breaking the Windows build except this: OS calls
belong in nexus/platform/, Linux-only behaviour behind nexus.features. Plain
text scans, no imports, so it runs the same on either OS."""

import ast
import re
import unittest
from pathlib import Path

NEXUS = Path(__file__).resolve().parent.parent / 'nexus'

SHARED = ('net', 'gamedb', 'repackindex', 'repackfeed', 'torrent', 'hltb', 'storeinfo',
          'news', 'inno', 'safety', 'history', 'achievements', 'icons', 'watch', 'alerts',
          'cleanup', 'steamlib', 'instance', 'cli/registry', 'cli/sgdb', 'cli/config',
          'sources/__init__', 'sources/epic')

FORBIDDEN = {
    r'\bimport fcntl\b': "file locks: platform.file_lock",
    r'\bimport (msvcrt|winreg)\b': "Windows APIs belong in platform/windows.py",
    r"""['"]/proc\b""": "the process list: platform.processes / platform.meminfo",
    r'\bxdg-open\b': "platform.open_url",
    r'\bstart_new_session\b': "platform.new_group / spawn_detached",
    r'\bos\.killpg\b': "platform.kill_tree",
    r'\bsymlink_to\b': "symlinks need admin rights on Windows",
    r'\bos\.getuid\b': "no uids on Windows",
    r'\.local/share\b': "paths.DATA_DIR / platform.data_dir",
    r'\bsys\.platform\b': "nexus.features.has(), or the platform layer",
}


class Portability(unittest.TestCase):
    def test_shared_modules_make_no_os_calls_of_their_own(self):
        problems = []
        for mod in SHARED:
            path = NEXUS / f'{mod}.py'
            self.assertTrue(path.is_file(), path)
            for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                for pat, use in FORBIDDEN.items():
                    if re.search(pat, line):
                        problems.append(f"nexus/{mod}.py:{n}: {line.strip()}  ->  {use}")
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_text_files_are_read_and_written_as_utf8(self):
        # Without encoding= Windows uses the ANSI code page (cp1252).
        problems = []
        for path in sorted(NEXUS.rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in ('read_text', 'write_text') \
                        and 'encoding' not in {k.arg for k in node.keywords}:
                    problems.append(f"{path.relative_to(NEXUS.parent)}:{node.lineno}")
        self.assertEqual(problems, [], "\nadd encoding='utf-8':\n" + "\n".join(problems))


if __name__ == '__main__':
    unittest.main()
