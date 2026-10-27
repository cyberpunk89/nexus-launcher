"""Integrity check: an MD5 baseline of a game's installed files, to catch
silent corruption in a folder without btrfs checksums."""

import json
import sys
from datetime import datetime
from pathlib import Path

from nexus import paths as NX

from .common import atomic_write


INTEGRITY_DIR = NX.INTEGRITY_DIR

# Everything under pfx/drive_c EXCEPT these two top-level dirs is treated as
# "game content" worth snapshotting: `users` holds saves/config (_SAVE_DIRS)
# and other per-user state that's supposed to change, and `windows` is
# Wine/Proton's own runtime, churned by every Proton update — neither is
# useful (or stable enough) to check for corruption.
_INTEGRITY_SKIP_TOPLEVEL = {'users', 'windows'}


def _integrity_files(game_dir: Path) -> list[Path]:
    """Installed game files worth an integrity snapshot (see
    _INTEGRITY_SKIP_TOPLEVEL for what's excluded and why)."""
    drive_c = game_dir / 'pfx' / 'drive_c'
    if not drive_c.is_dir():
        return []
    files = []
    for top in drive_c.iterdir():
        if top.name.lower() in _INTEGRITY_SKIP_TOPLEVEL:
            continue
        if top.is_file():
            files.append(top)
        elif top.is_dir():
            files.extend(f for f in top.rglob('*') if f.is_file())
    return files


def integrity_manifest_path(slug: str) -> Path:
    return INTEGRITY_DIR / f"{slug}.json"


def load_integrity_manifest(slug: str) -> dict | None:
    try:
        return json.loads(integrity_manifest_path(slug).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def save_integrity_manifest(slug: str, game_dir: Path, files: list[Path]) -> None:
    hashes = dict(zip(files, _parallel_md5(files)))
    manifest = {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'files': {
            str(f.relative_to(game_dir)): {'size': f.stat().st_size, 'md5': h}
            for f, h in hashes.items()
        },
    }
    atomic_write(integrity_manifest_path(slug), json.dumps(manifest, indent=2))


def cmd_check_integrity(slug: str, rebaseline: bool = False) -> None:
    """Compare installed game files against a saved MD5 manifest (or create
    one if this is the first run) to catch silent corruption — relevant here
    because the game dir has btrfs checksums disabled (see disable_cow)."""
    entry = registry_get(slug)
    if not entry or not entry.get('wine_prefix'):
        print(f"'{slug}' is not a managed game with a prefix", file=sys.stderr)
        sys.exit(1)
    game_dir = Path(entry['wine_prefix']).parent
    if not game_dir.is_dir():
        print(f"Install directory for '{slug}' is missing: {game_dir}", file=sys.stderr)
        sys.exit(1)

    files = _integrity_files(game_dir)
    if not files:
        print("No installed game files found under pfx/drive_c to check "
              "(everything is under users/ or windows/, which are skipped).")
        return

    existing = None if rebaseline else load_integrity_manifest(slug)
    if existing is None:
        total_mb = sum(f.stat().st_size for f in files) / 1024 / 1024
        print(f"{'Rebaselining' if rebaseline else 'No baseline yet — creating one'}: "
              f"hashing {len(files)} file(s), {total_mb:.0f} MB...")
        save_integrity_manifest(slug, game_dir, files)
        print(f"Baseline saved ({len(files)} files). Run this again later to "
              f"check for changes.")
        return

    current_rel = {str(f.relative_to(game_dir)) for f in files}
    known_rel = set(existing.get('files', {}))
    missing = sorted(known_rel - current_rel)
    added = sorted(current_rel - known_rel)
    common = [f for f in files if str(f.relative_to(game_dir)) in known_rel]

    total_mb = sum(f.stat().st_size for f in common) / 1024 / 1024
    print(f"Checking against baseline from {existing.get('created_at', 'unknown')}: "
          f"hashing {len(common)} known file(s), {total_mb:.0f} MB...")
    changed = []
    for f, actual in zip(common, _parallel_md5(common)):
        rel = str(f.relative_to(game_dir))
        if actual != existing['files'][rel]['md5']:
            changed.append(rel)
    changed.sort()

    def _list(label, items):
        print(f"  [!] {len(items)} file(s) {label}:")
        for rel in items[:20]:
            print(f"        {rel}")
        if len(items) > 20:
            print(f"        ... and {len(items) - 20} more")

    print()
    if not missing and not changed:
        print(f"  OK — all {len(common)} file(s) match the baseline.")
    else:
        if changed:
            _list("CHANGED since the baseline (possible corruption)", changed)
        if missing:
            _list("MISSING since the baseline", missing)
    if added:
        print(f"  [i] {len(added)} file(s) not in the baseline (new — not flagged; "
              f"likely a game update or a config/settings write).")

    if changed or missing:
        print(f"\n  If this is expected (e.g. you updated or modded the game), accept")
        print(f"  the current files as the new baseline with:")
        print(f"    nexus-cli --check-integrity {slug} --rebaseline")
        sys.exit(1)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .registry import registry_get  # noqa: E402
from .repack import _parallel_md5  # noqa: E402
