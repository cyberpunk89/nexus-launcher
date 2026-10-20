"""LibraryPrefs: the GUI's own settings and per-game notes (library.json)."""

import json
import sys

from nexus import net, paths as NX

from .common import ATTRACT_MINUTES, MAX_COLLECTION_NAME, MAX_NOTE, MAX_TITLE
from .theme import normalize_appearance, TILE_W


def clean_collection_name(name) -> str:
    return ' '.join(str(name or '').split())[:MAX_COLLECTION_NAME]


class LibraryPrefs:
    """Favourites, hidden games and collections — GUI-only preferences, JSON
    in ~/.config/nexus/library.json."""
    PATH = NX.CONFIG_DIR / 'library.json'

    def __init__(self):
        try:
            data = json.loads(self.PATH.read_text())
        except (OSError, ValueError):
            data = {}
        self._data = data if isinstance(data, dict) else {}
        self.favorites = set(self._data.get('favorites', []))
        self.hidden = set(self._data.get('hidden', []))
        # Named groups of games, in the order they were made: name -> slugs.
        self.collections: dict[str, list[str]] = {}
        cols = self._data.get('collections')
        for name, slugs in (cols.items() if isinstance(cols, dict) else ()):
            name = clean_collection_name(name)
            if name and isinstance(slugs, list) and not self.find_collection(name):
                self.collections[name] = list(dict.fromkeys(
                    s for s in slugs if isinstance(s, str)))
        self.view = self._data.get('view', 'home')
        self.tile = int(self._data.get('tile', TILE_W))
        dash = self._data.get('dashboard') or {}
        self.dashboard = dash_normalize(dash.get('items') if isinstance(dash, dict) else None)
        self.appearance = normalize_appearance(self._data.get('appearance'))
        # Games you own but haven't installed: shown at all (Settings), and
        # which of installed / not installed / all the Library lists.
        self.show_owned = bool(self._data.get('show_owned', True))
        own = self._data.get('owned_view', 'installed')
        self.owned_view = own if own in ('installed', 'owned', 'all') else 'installed'
        # Repack downloads already announced (path|fingerprint); None until the
        # first scan, which only records what's there without announcing it.
        seen = self._data.get('seen_repacks')
        # A list in first-seen order, so trimming drops the oldest entries.
        self.seen_repacks = list(dict.fromkeys(seen)) if isinstance(seen, list) else None
        # How a tile is activated: a double click and A/Enter each play the
        # game or open its page (Settings → Controls).
        ctl = self._data.get('controls') if isinstance(self._data.get('controls'), dict) else {}
        self.controls = {
            'double_click': ctl.get('double_click') if ctl.get('double_click') in
            ('play', 'details') else 'play',
            'pad_a': ctl.get('pad_a') if ctl.get('pad_a') in ('play', 'details') else 'details',
            # One tile per game when it's in several stores (Settings → Controls).
            'dedupe': ctl.get('dedupe') is not False,
            # Theatre's screensaver after this many idle minutes (0 = never).
            'attract': ctl.get('attract') if ctl.get('attract') in ATTRACT_MINUTES else 5,
            # Closing the window hides it to the tray (Settings → Background).
            'close_to_tray': ctl.get('close_to_tray') is True,
        }
        # Your notes on a game (the details panel's Notes section).
        notes = self._data.get('notes')
        self.notes = {str(k): str(v)[:MAX_NOTE] for k, v in
                      (notes.items() if isinstance(notes, dict) else ()) if str(v).strip()}
        # Your own name for a game, any source: {slug: {'name': str}}.
        ov = self._data.get('overrides')
        self.overrides = {k: {'name': str(v['name'])[:MAX_TITLE]}
                          for k, v in (ov.items() if isinstance(ov, dict) else ())
                          if isinstance(v, dict) and str(v.get('name') or '').strip()}
        # The Library toolbar as it was left: sort, status filter, source chip,
        # and how wide the details panel was dragged.
        lib = self._data.get('library_state')
        self.library_state = lib if isinstance(lib, dict) else {}
        # The Deals page as it was left: section, filter, sort, hide owned.
        ds = self._data.get('deals_state')
        self.deals_state = ds if isinstance(ds, dict) else {}

    def save(self):
        # Keys this version doesn't know about are kept, not dropped.
        out = dict(self._data)
        out.update({'favorites': sorted(self.favorites), 'hidden': sorted(self.hidden),
                    'view': self.view, 'tile': self.tile,
                    'dashboard': {'version': 1, 'items': self.dashboard},
                    'appearance': self.appearance, 'show_owned': self.show_owned,
                    'owned_view': self.owned_view, 'controls': self.controls,
                    'library_state': self.library_state, 'collections': self.collections,
                    'deals_state': self.deals_state,
                    'overrides': self.overrides, 'notes': self.notes})
        if self.seen_repacks is not None:
            out['seen_repacks'] = list(self.seen_repacks)[-300:]
        try:
            net.write_atomic(self.PATH, json.dumps(out, indent=2))
        except OSError as e:                 # a full disk: keep running, say so
            sys.stderr.write(f"nexus: couldn't save {self.PATH}: {e}\n")

    def toggle(self, which: str, slug: str) -> bool:
        s = self.favorites if which == 'favorite' else self.hidden
        s.symmetric_difference_update({slug})
        self.save()
        return slug in s

    # ---- collections ----
    def find_collection(self, name: str) -> str | None:
        """The existing collection with this name, ignoring case and spacing."""
        key = clean_collection_name(name).casefold()
        return next((n for n in self.collections if n.casefold() == key), None)

    def collections_of(self, slug: str) -> list[str]:
        return [n for n, slugs in self.collections.items() if slug in slugs]

    def add_collection(self, name: str) -> str | None:
        """Make a collection (or find the one already called that)."""
        name = clean_collection_name(name)
        if not name:
            return None
        hit = self.find_collection(name)
        if hit is None:
            self.collections[name] = []
            self.save()
        return hit or name

    def toggle_member(self, name: str, slug: str) -> bool:
        slugs = self.collections.get(name)
        if slugs is None:
            return False
        on = slug not in slugs
        if on:
            slugs.append(slug)
        else:
            slugs.remove(slug)
        self.save()
        return on

    def rename_collection(self, old: str, new: str) -> str | None:
        """None if `new` is empty or another collection already has it."""
        new = clean_collection_name(new)
        hit = self.find_collection(new) if new else None
        if not new or old not in self.collections or (hit is not None and hit != old):
            return None
        self.collections = {new if n == old else n: v for n, v in self.collections.items()}
        self.save()
        return new

    def delete_collection(self, name: str):
        if self.collections.pop(name, None) is not None:
            self.save()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .dashboard import dash_normalize  # noqa: E402
