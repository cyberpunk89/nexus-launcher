"""A safety look at a repack folder before it's installed. FitGirl repacks
have a fixed shape — setup.exe, fg-NN.bin parts, "Verify BIN files before
installation.bat" and an MD5 folder holding QuickSFV — and fakes or
tampered copies usually break it: an extra executable or script, a
disguised file name, a setup.exe that isn't an Inno installer, or an
installer told to fetch and run something from the internet.

check() returns {verdict: 'ok'|'warn'|'danger', findings: [{level ('info',
'warn' or 'danger'), file, why}], clamav, hypervisor}. A hypervisor-bypass
repack (nexus/hypervisor.py) is a finding of its own: 'warn' on Linux, where
it can't run, 'info' on Windows; it's marked 'hv' so it's always shown. With ClamAV installed it can also scan the small files
(never the multi-GB .bin parts, which are compressed archives anyway).
Nothing here is proof either way: it catches the common tricks."""

import re
import shutil
import subprocess
from pathlib import Path

from . import hypervisor
from .features import has

EXEC_EXT = {'.exe', '.scr', '.com', '.pif', '.bat', '.cmd', '.vbs', '.vbe', '.js', '.jse',
            '.wsf', '.wsh', '.ps1', '.psm1', '.lnk', '.msi', '.msp', '.dll', '.hta', '.jar',
            '.reg', '.url', '.cpl', '.appref-ms'}
DOC_EXT = {'.pdf', '.txt', '.doc', '.docx', '.jpg', '.jpeg', '.png', '.mp4', '.mkv', '.zip',
           '.rar', '.7z', '.iso'}
ALLOWED = {'setup.exe', 'verify bin files before installation.bat',
           'md5/quicksfv.exe', 'md5/quicksfv.ini'}
BIN_RE = re.compile(r'^fg-(?:\d+|optional-[\w.-]+|selective-[\w.-]+)\.bin$', re.I)
# What a legitimate FitGirl setup header links to (redistributables, credits).
URL_OK = re.compile(r'^https?://(?:[\w-]+\.)*(?:microsoft\.com|nvidia\.com|riotpixels\.com|'
                    r'fitgirl-repacks\.site|dk-soft\.org|bit\.ly/fitgirl-repacks-site|'
                    r'visualstudio\.com|aka\.ms|openal\.org|xinput\.com|directx\.com)(?:/|$)',
                    re.I)
DANGER_TOKENS = ('powershell', 'mshta', 'bitsadmin', 'certutil', 'wscript', 'cscript',
                 'schtasks', '-encodedcommand', 'frombase64string', 'invoke-webrequest',
                 'downloadstring', 'start-process')
BAT_SAFE = re.compile(r'^\s*(@echo off|cd /d "%~dp0"|cd md5|start quicksfv\.exe [\w.-]+\.md5|'
                      r'rem\b.*|)\s*$', re.I)
RTLO = '‮'


def _level(findings) -> str:
    levels = {f['level'] for f in findings}
    return 'danger' if 'danger' in levels else 'warn' if 'warn' in levels else 'ok'


def check(repack_dir: Path, header_reader=None, clamav: bool = False) -> dict:
    repack_dir = Path(repack_dir)
    findings = []

    def add(level, path, why):
        rel = path.relative_to(repack_dir).as_posix() if isinstance(path, Path) else path
        findings.append({'level': level, 'file': rel, 'why': why})

    files = []
    for p in sorted(repack_dir.rglob('*')):
        try:
            if p.is_symlink():
                add('warn', p, "a symbolic link — repacks don't contain any")
                continue
            if p.is_file():
                files.append(p)
        except OSError:
            continue
    names = {p.relative_to(repack_dir).as_posix().lower() for p in files}
    hv = hypervisor.in_folder(repack_dir)
    allowed = ALLOWED | ({'vbs.cmd'} if hv else set())   # an HV repack's own VBS script
    if 'setup.exe' not in names:
        add('warn', 'setup.exe', "missing — this doesn't look like a FitGirl repack")
    if not any(BIN_RE.match(p.name) for p in files):
        add('warn', 'fg-01.bin', "no fg-NN.bin parts — not the usual repack layout")
    for p in files:
        rel = p.relative_to(repack_dir).as_posix().lower()     # ALLOWED uses /
        name = p.name
        if RTLO in name or any(ord(c) < 32 for c in name):
            add('danger', p, "the name hides its real extension (right-to-left override)")
            continue
        parts = name.lower().split('.')
        if len(parts) > 2 and '.' + parts[-1] in EXEC_EXT and '.' + parts[-2] in DOC_EXT:
            add('danger', p, f"a program dressed up as a .{parts[-2]} file")
            continue
        if rel in allowed or BIN_RE.match(name) or rel.startswith('md5/') and rel.endswith('.md5'):
            continue
        ext = p.suffix.lower()
        if ext in EXEC_EXT:
            add('warn', p, "an extra program or script FitGirl repacks don't include")
    bat = repack_dir / 'Verify BIN files before installation.bat'
    try:
        lines = (bat.read_text(encoding='utf-8', errors='replace').splitlines()
                 if bat.is_file() else [])
    except OSError:
        lines = []
    if any(not BAT_SAFE.match(line) for line in lines):
        add('danger' if any(t in ' '.join(lines).lower() for t in DANGER_TOKENS) else 'warn',
            bat, "does more than run QuickSFV on the MD5 file")
    setup = repack_dir / 'setup.exe'
    if setup.is_file():
        if header_reader is None:
            from nexus.inno import inno_header as header_reader
        header = header_reader(setup)
        if header is None:
            add('warn', setup, "not a readable Inno Setup installer (FitGirl's always is)")
        else:
            text = (header.decode('latin-1') + '\n' + header.decode('utf-16-le', 'replace')).lower()
            for tok in DANGER_TOKENS:
                if tok in text:
                    add('danger', setup, f"the installer mentions {tok} — it may download "
                                         f"and run something")
            bad = sorted({u.rstrip('./') for u in
                          re.findall(r'https?://[\w.-]+(?:/[\w./%-]*)?', text)
                          if not URL_OK.match(u)})
            if bad:                   # credits and project pages: worth a look, no more
                add('info', setup, "the installer links to: " + ', '.join(bad[:4]))
    if hv:
        findings.append({'level': 'warn' if has('proton') else 'info', 'file': repack_dir.name,
                         'why': hypervisor.note(), 'hv': True})
    result = {'verdict': _level(findings), 'findings': findings, 'clamav': None,
              'hypervisor': hv,
              'clamav_available': bool(shutil.which('clamdscan') or shutil.which('clamscan'))}
    if clamav:
        result['clamav'] = clamscan([p for p in files if not BIN_RE.match(p.name)])
        if result['clamav'].get('infected'):
            for f in result['clamav']['infected']:
                add('danger', Path(f) if Path(f).is_absolute() else f, "ClamAV flagged it")
            result['verdict'] = _level(findings)
    return result


def clamscan(paths: list[Path]) -> dict:
    exe = shutil.which('clamdscan') or shutil.which('clamscan')
    if not exe:
        return {'available': False, 'infected': []}
    if not paths:
        return {'available': True, 'infected': []}
    try:
        r = subprocess.run([exe, '--no-summary', '--infected', '--',
                            *(str(Path(x).resolve()) for x in paths)],
                           capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'available': True, 'error': str(e), 'infected': []}
    infected = [line.split(':', 1)[0] for line in r.stdout.splitlines()
                if line.rstrip().endswith('FOUND')]
    return {'available': True, 'infected': infected,
            'error': r.stderr.strip()[:300] if r.returncode == 2 else None}
