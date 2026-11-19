"""Keeps the shared modules portable. Both apps use these, and nothing stops a
Linux change from quietly breaking the Windows build except this: OS calls
belong in nexus/platform/, Linux-only behaviour behind nexus.features. Plain
text scans, no imports, so it runs the same on either OS."""

import ast
import builtins
import re
import unittest
from pathlib import Path

NEXUS = Path(__file__).resolve().parent.parent / 'nexus'

SHARED = ('net', 'gamedb', 'repackindex', 'repackfeed', 'torrent', 'hltb', 'storeinfo',
          'news', 'inno', 'safety', 'history', 'achievements', 'icons', 'watch', 'alerts',
          'cleanup', 'steamlib', 'instance', 'cli/registry', 'cli/sgdb', 'cli/config',
          'sources/__init__', 'sources/epic', 'report/__init__', 'report/scrub',
          'report/sysinfo', 'report/crash', 'report/render', 'stats', 'social')

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

    def test_annotations_only_name_what_is_already_bound(self):
        # Python 3.14 evaluates annotations lazily, but CI's 3.11 and the
        # Windows build's 3.12 evaluate a def's annotations when the def runs:
        # a name imported further down the module (or the class's own name in
        # its body) is a NameError there and breaks every import of the GUI.
        # Quote it ('Popover') instead. Found by the Windows VM on 2026-11-19.
        problems = []
        for path in sorted(NEXUS.rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            if any(isinstance(n, ast.ImportFrom) and n.module == '__future__'
                   and any(a.name == 'annotations' for a in n.names) for n in tree.body):
                continue
            bound: dict[str, int] = {}

            def bind(name, line):
                bound.setdefault(name, line)
            for n in tree.body:
                for x in ast.walk(n) if isinstance(n, (ast.If, ast.Try)) else [n]:
                    if isinstance(x, (ast.Import, ast.ImportFrom)):
                        for a in x.names:
                            bind((a.asname or a.name).split('.')[0], n.lineno)
                    elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        bind(x.name, n.lineno)
                    elif isinstance(x, (ast.Assign, ast.AnnAssign)):
                        targets = x.targets if isinstance(x, ast.Assign) else [x.target]
                        for t in targets:
                            for y in ast.walk(t):
                                if isinstance(y, ast.Name):
                                    bind(y.id, n.lineno)
            funcs = []
            for n in tree.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs.append(n)
                elif isinstance(n, ast.ClassDef):
                    funcs += [m for m in n.body
                              if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for f in funcs:
                a = f.args
                anns = [f.returns, a.vararg and a.vararg.annotation, a.kwarg and a.kwarg.annotation]
                anns += [x.annotation for x in a.args + a.kwonlyargs + a.posonlyargs]
                for ann in anns:
                    if ann is None or isinstance(ann, ast.Constant):
                        continue
                    for nm in {x.id for x in ast.walk(ann) if isinstance(x, ast.Name)}:
                        if hasattr(builtins, nm):
                            continue
                        line = bound.get(nm)
                        if line is None or line >= f.lineno:
                            problems.append(f"{path.relative_to(NEXUS.parent)}:{f.lineno} {nm}")
        self.assertEqual(problems, [], "\nquote these annotations:\n" + "\n".join(problems))


if __name__ == '__main__':
    unittest.main()
