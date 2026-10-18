"""Proton builds: what's installed, what's released upstream, and installing
or removing GE-Proton the way ProtonPlus / protonup do — into Steam's
compatibilitytools.d, where Steam, Heroic, umu and protongen all find it.

Downloads are verified against the release's published sha512 checksum and
extracted with tarfile's 'data' filter (no absolute paths, no escaping the
target, no device files) before being moved into place."""

import hashlib
import platform
import re
import shutil
import tarfile
from pathlib import Path

from . import net
from .paths import CACHE_DIR, STEAM_ROOT

USER_TOOLS = STEAM_ROOT / 'compatibilitytools.d'

FAMILIES = {
    'ge': {
        'label': 'GE-Proton',
        'repo': 'GloriousEggroll/proton-ge-custom',
        'match': re.compile(r'^GE-Proton\d', re.IGNORECASE),
        'installable': True,
    },
    'cachyos': {
        'label': 'proton-cachyos',
        'repo': 'CachyOS/proton-cachyos',
        'match': re.compile(r'^proton-cachyos', re.IGNORECASE),
        'installable': False,          # a pacman package on CachyOS
    },
}
RELEASES_MAX_AGE = 6 * 3600


def family_of(name: str) -> str | None:
    for key, fam in FAMILIES.items():
        if fam['match'].match(name or ''):
            return key
    return None


def version_key(name: str) -> tuple:
    """'GE-Proton11-7' -> (11, 7); cachyos build dates compare numerically too."""
    return tuple(int(n) for n in re.findall(r'\d+', name or ''))


_ARCH = {'x86_64': 'x86_64', 'amd64': 'x86_64', 'aarch64': 'aarch64',
         'arm64': 'aarch64'}.get(platform.machine().lower(), platform.machine().lower())


def pick_asset(assets: list[dict], suffixes: tuple, arch: str = _ARCH) -> dict | None:
    """The archive for this CPU: one naming the host arch, else one naming no
    arch at all (older GE releases). Never another arch's build."""
    cands = [a for a in assets if a.get('name', '').endswith(suffixes)]
    for a in cands:
        if f"-{arch}." in a['name'] or f"_{arch}." in a['name']:
            return a
    others = {'x86_64', 'aarch64', 'arm64', 'armhf', 'i386'} - {arch}
    for a in cands:
        if not any(o in a['name'] for o in others):
            return a
    return None


def releases(family: str = 'ge', refresh: bool = False, count: int = 15) -> dict:
    """Upstream releases (newest first), cached 6 h. Only assets hosted in the
    family's own repo are accepted."""
    fam = FAMILIES[family]
    url = f"https://api.github.com/repos/{fam['repo']}/releases?per_page={count}"
    data, at, stale = net.cached(url, f"releases-{family}.json", RELEASES_MAX_AGE, refresh)
    allowed = f"https://github.com/{fam['repo']}/releases/download/"
    out = []
    for rel in data if isinstance(data, list) else []:
        if rel.get('draft') or rel.get('prerelease'):
            continue
        assets = [a for a in rel.get('assets', [])
                  if a.get('browser_download_url', '').startswith(allowed)]
        tar = pick_asset(assets, ('.tar.gz',))
        sha = None
        if tar:
            stem = tar['name'][:-len('.tar.gz')]
            sha = next((a for a in assets
                        if a.get('name') in (f"{stem}.sha512sum", f"{stem}.sha512")), None)
        out.append({
            'tag': rel.get('tag_name'),
            'name': rel.get('name') or rel.get('tag_name'),
            'published_at': rel.get('published_at'),
            'body': (rel.get('body') or '')[:6000],
            'html_url': rel.get('html_url') if str(rel.get('html_url', '')).startswith(
                f"https://github.com/{fam['repo']}/") else None,
            'tar_url': tar and tar['browser_download_url'],
            'tar_name': tar and tar['name'],
            'tar_size': tar and tar.get('size'),
            'sha_url': sha and sha['browser_download_url'],
        })
    return {'family': family, 'releases': out, 'fetched_at': at, 'stale': stale}


class InstallError(Exception):
    pass


def _expected_sha512(text: str, tar_name: str) -> str:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip('*').endswith(tar_name):
            return parts[0].lower()
        if len(parts) == 1 and re.fullmatch(r'[0-9a-fA-F]{128}', parts[0]):
            return parts[0].lower()
    raise InstallError("the checksum file doesn't list this archive")


def _sha512(path: Path) -> str:
    h = hashlib.sha512()
    with open(path, 'rb') as f:
        while chunk := f.read(4 << 20):
            h.update(chunk)
    return h.hexdigest()


def extract(archive: Path, dest_dir: Path, replace=None) -> Path:
    """Extract a Proton tarball whose members all live under one top folder,
    into dest_dir/<top>. Refuses anything else. Returns the new folder.
    With `replace` (a callable), an existing dest_dir/<top> is handed to it
    (to move it to the Trash) once the new copy is extracted and checked —
    so a failed reinstall leaves the old build where it was."""
    with tarfile.open(archive, 'r:gz') as tf:
        members = tf.getmembers()
        tops = {m.name.split('/', 1)[0] for m in members if m.name not in ('', '.')}
        if len(tops) != 1:
            raise InstallError(f"unexpected archive layout ({len(tops)} top-level entries)")
        top = tops.pop()
        if top in ('.', '..') or '/' in top or top.startswith('.'):
            raise InstallError(f"unsafe top-level folder {top!r}")
        final = dest_dir / top
        if final.exists() and replace is None:
            raise InstallError(f"{final} already exists")
        staging = dest_dir / f".nexus-extract-{top}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            tf.extractall(staging, filter='data')
        except (tarfile.TarError, OSError) as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise InstallError(f"extraction failed: {e}") from None
    # Checked before it lands in compatibilitytools.d: Steam lists whatever is
    # there, and a half-right folder would block the next try ("already exists").
    if not (staging / top / 'proton').is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallError(f"{top} has no 'proton' launcher — not a Proton build?")
    if final.exists():
        try:
            replace(final)
        except OSError as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise InstallError(f"couldn't move the old {top} aside: {e}") from None
        if final.exists():
            shutil.rmtree(staging, ignore_errors=True)
            raise InstallError(f"the old {top} is still there")
    (staging / top).rename(final)
    shutil.rmtree(staging, ignore_errors=True)
    return final


def install(release: dict, say=print, dest_dir: Path = USER_TOOLS, replace=None) -> Path:
    """Download, verify and install one GE-Proton release. `say` receives
    progress lines in the "[k/N] step" form the GUI's progress bar reads.
    `replace` reinstalls over an existing copy (see extract)."""
    if not (release.get('tar_url') and release.get('sha_url')):
        raise InstallError(f"{release.get('tag')} has no downloadable archive + checksum")
    dl_dir = CACHE_DIR / 'downloads'
    archive = dl_dir / release['tar_name']
    say(f"[1/3] Downloading {release['tar_name']}")
    last = [-1]

    def progress(done, total):
        if total:
            pct = int(done * 100 / total)
            if pct != last[0] and pct % 2 == 0:
                last[0] = pct
                say(f"  [{pct}/100] {done >> 20} / {total >> 20} MB")
    try:
        net.download(release['tar_url'], archive, progress)
        say("[2/3] Verifying checksum")
        expected = _expected_sha512(net.get(release['sha_url']).decode('utf-8', 'replace'),
                                    release['tar_name'])
        if _sha512(archive) != expected:
            raise InstallError("checksum mismatch — the download is corrupt or was tampered "
                               "with; nothing was installed")
        say("[3/3] Extracting")
        final = extract(archive, dest_dir, replace)
    except net.NetError as e:
        raise InstallError(str(e)) from None
    finally:
        archive.unlink(missing_ok=True)
    return final


def is_user_build(path: Path) -> bool:
    """Only builds in the user's compatibilitytools.d may be removed here —
    system packages (/usr) belong to the package manager."""
    try:
        return path.resolve().parent == USER_TOOLS.resolve()
    except OSError:
        return False


# ── a build whose files were changed after it was installed ───────────────
# Proton prefixes symlink their builtin DLLs into the build's own files/.
# A tool that writes "into the prefix" through those links (instead of
# replacing them) rewrites the *build*: seen on this machine when Steam's
# start-up prefix setup left GE-Proton's Windows DLLs as proton-cachyos's.
# The Linux half no longer matches, and every program aborts at once with
# "unimplemented function win32u.…".
CORE_DLLS = ('win32u.dll', 'ntdll.dll', 'user32.dll', 'kernel32.dll')
_SLACK = 3600            # seconds: the files of one install land together


def _pe_dir(build: Path) -> Path | None:
    for rel in ('files/lib/wine/x86_64-windows', 'files/lib64/wine/x86_64-windows',
                'dist/lib/wine/x86_64-windows'):
        if (build / rel).is_dir():
            return build / rel
    return None


def _digest(path: Path) -> str | None:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:
        return None


def damage(build: Path, others=()) -> str | None:
    """Why this Proton build is broken, or None. Its core Windows DLLs are
    newer than the build itself (changed after it was installed) and — the
    tell — byte-identical to another installed build's. `others` are the
    other builds' folders."""
    pe = _pe_dir(build)
    if pe is None:
        return None
    try:
        born = max((build / n).stat().st_mtime for n in ('proton', 'version')
                   if (build / n).exists())
    except ValueError:
        return None
    changed = []
    for name in CORE_DLLS:
        try:
            if (pe / name).stat().st_mtime > born + _SLACK:
                changed.append(name)
        except OSError:
            continue
    if not changed:
        return None
    mine = {n: _digest(pe / n) for n in changed}
    for other in others:
        if Path(other) == build:
            continue
        ope = _pe_dir(Path(other))
        if ope is None:
            continue
        same = [n for n in changed if mine[n] and mine[n] == _digest(ope / n)]
        if same:
            from datetime import datetime
            when = datetime.fromtimestamp((pe / same[0]).stat().st_mtime)
            return (f"its Windows DLLs were overwritten on {when:%Y-%m-%d %H:%M} and now "
                    f"match {Path(other).name}'s ({', '.join(same)})")
    return None


def tag_of(name: str) -> str | None:
    """The GE-Proton release tag a build folder came from
    ("GE-Proton11-9-x86_64" -> "GE-Proton11-9"), or None."""
    m = re.match(r'^(GE-Proton\d+-\d+)', name)
    return m.group(1) if m else None
