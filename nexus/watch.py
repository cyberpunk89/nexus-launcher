"""Which finished repack downloads have been announced already, shared by
the GUI's RepackWatcher and the background agent (nexus/cli/agent.py), so a
download is offered once whichever of them sees it first.

State: DATA_DIR/watch-state.json {"seen": ["<path>|<fingerprint>", ...]},
edited under a lock. No file yet means "nothing recorded": the first scan
then only records what's there instead of announcing every old folder."""

import contextlib
import json

from . import net, platform
from .paths import DATA_DIR

STATE = DATA_DIR / 'watch-state.json'
SEEN_MAX = 300


def key(c: dict) -> str:
    return f"{c['path']}|{c.get('fingerprint') or ''}"


def load_seen() -> list[str] | None:
    """The announced keys, or None when nothing was ever recorded."""
    try:
        data = json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    seen = data.get('seen') if isinstance(data, dict) else None
    return [str(k) for k in seen] if isinstance(seen, list) else None


@contextlib.contextmanager
def edit_seen():
    """Yield {'seen': the announced keys, or None when never recorded} for
    the caller to replace under the lock; saved if it changed."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with platform.file_lock(STATE.with_name(STATE.name + '.lock')):
        before = load_seen()
        box = {'seen': None if before is None else list(before)}
        yield box
        if box['seen'] is not None and box['seen'] != before:
            net.save_json(STATE, {'seen': box['seen'][-SEEN_MAX:]})


def migrate(seen_from_prefs: list | None) -> None:
    """Carry the GUI's old LibraryPrefs.seen_repacks over, once."""
    if seen_from_prefs is None or STATE.exists():
        return
    with edit_seen() as box:
        if box['seen'] is None:
            box['seen'] = [str(k) for k in seen_from_prefs]


def triage(cands: list[dict], seen: list[str] | None, sizes: dict, skip: set[str]):
    """Decide which scanned folders to announce now. A folder is ready once
    it's complete and its size held still between two scans (`sizes`
    remembers the last one per path; updated in place); folders in `skip`
    (being installed already) and installed ones that aren't updates are
    recorded without a word. On the first scan ever (`seen` None) every
    folder is only recorded.

    Returns (ready, new_seen, waiting): the candidates to announce, the
    keys to store, and whether something is still arriving. Run it inside
    edit_seen() so the GUI and the agent can't both announce one folder:

        with edit_seen() as box:
            ready, box['seen'], waiting = triage(cands, box['seen'], sizes, skip)"""
    first = seen is None
    out = {} if first else dict.fromkeys(seen)          # an ordered set
    ready, waiting = [], False
    for c in cands:
        k = key(c)
        if k in out:
            continue
        if str(c['path']) in skip or first or (c.get('installed') and not c.get('update_for')):
            out[k] = None
            continue
        size, prev = c.get('size_bytes'), sizes.get(c['path'])
        sizes[c['path']] = size
        if not c.get('complete', True) or prev != size:
            waiting = True
            continue
        out[k] = None
        ready.append(c)
    return ready, list(out), waiting

