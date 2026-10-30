"""LibraryPrefs: the GUI's own settings and per-game notes (library.json)."""

import json
import sys
import uuid

from nexus import net, paths as NX, smart
from nexus.icons import COLLECTION_ICONS

from .common import ATTRACT_MINUTES, MAX_COLLECTION_NAME, MAX_NOTE, MAX_TITLE
from .theme import ACCENTS, normalize_appearance, TILE_W

MAX_PINNED = 3                    # collections pinned to the top bar (w-52)


def clean_collection_name(name) -> str:
    return ' '.join(str(name or '').split())[:MAX_COLLECTION_NAME]


def clean_collection_look(look) -> dict:
    """A collection_look entry with only valid choices: 'icon' a name from
    COLLECTION_ICONS, 'color' an accent key, 'pinned' True. Keys a newer
    Nexus added are kept as they are."""
    if not isinstance(look, dict):
        return {}
    out = {k: v for k, v in look.items() if k not in ('icon', 'color', 'pinned')}
    if look.get('icon') in COLLECTION_ICONS:
        out['icon'] = look['icon']
    if look.get('color') in ACCENTS:
        out['color'] = look['color']
    if look.get('pinned') is True:
        out['pinned'] = True
    return out


class LibraryPrefs:
    """Favourites, hidden games and collections — GUI-only preferences, JSON
    in ~/.config/nexus/library.json."""
    PATH = NX.CONFIG_DIR / 'library.json'

    def __init__(self):
        try:
            data = json.loads(self.PATH.read_text(encoding='utf-8'))
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
        # Each one's icon, colour and top-bar pin (w-52): a key of its own, so
        # an older Nexus still reads `collections` (and keeps this one).
        self.collection_look: dict[str, dict] = {}
        looks = self._data.get('collection_look')
        for name, look in (looks.items() if isinstance(looks, dict) else ()):
            name = self.find_collection(name)
            if name and (look := clean_collection_look(look)):
                self.collection_look[name] = look
        for name in self.pinned_collections()[MAX_PINNED:]:
            self.collection_look[name].pop('pinned')
        self.view = self._data.get('view', 'home')
        try:                                 # a hand-edited bad value mustn't stop start-up
            self.tile = max(140, min(260, int(self._data.get('tile', TILE_W))))
        except (TypeError, ValueError):
            self.tile = TILE_W
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
        # Theatre's Library as it was left (its own: the desktop's is above):
        # {'sort': 'name'|'played'|'most'|'added'|'collection',
        #  'show': 'installed'|'missing'|'all'}
        tl = self._data.get('theatre_library')
        self.theatre_library = {
            'sort': tl.get('sort') if isinstance(tl, dict) and tl.get('sort') in (
                'name', 'played', 'most', 'added', 'collection') else 'name',
            'show': tl.get('show') if isinstance(tl, dict) and tl.get('show') in (
                'installed', 'missing', 'all') else 'installed'}
        # Smart shelves you hid (keys like 'smart:never', 'genre:RPG', 'user:<id>').
        sh = self._data.get('smart_hidden')
        self.smart_hidden = list(dict.fromkeys(
            k for k in sh if isinstance(k, str)
            and (k.startswith(('smart:', 'genre:')) or smart.is_user(k))))[:50] \
            if isinstance(sh, list) else []
        # Shelves you built from rules (w-51, nexus/smart.py); removed ones are kept.
        self.smart_shelves = smart.clean_shelves(self._data.get('smart_shelves'))
        # The Deals page as it was left: section, filter, sort, hide owned.
        ds = self._data.get('deals_state')
        self.deals_state = ds if isinstance(ds, dict) else {}
        # Folders games were moved to (newest first), offered again by Move to….
        mt = self._data.get('move_targets')
        self.move_targets = [str(t) for t in mt if isinstance(t, str)][:5] \
            if isinstance(mt, list) else []
        # First-run setup (gui/setup.py): done once, or skipped.
        self.setup_done = self._data.get('setup_done') is True
        # Windows: the note on the UAC prompt before the first install, shown once.
        self.uac_note_shown = self._data.get('uac_note_shown') is True
        # The newest Nexus version this library has run (its "What's new" was shown).
        lv = self._data.get('last_seen_version')
        self.last_seen_version = lv if isinstance(lv, str) else None
        # The command palette's recent picks (Ctrl+K), newest first.
        pr = self._data.get('palette_recent')
        self.palette_recent = [str(k) for k in pr if isinstance(k, str)][:8] \
            if isinstance(pr, list) else []

    def save(self):
        # Keys this version doesn't know about are kept, not dropped.
        out = dict(self._data)
        out.update({'favorites': sorted(self.favorites), 'hidden': sorted(self.hidden),
                    'view': self.view, 'tile': self.tile,
                    'dashboard': {'version': 1, 'items': self.dashboard},
                    'appearance': self.appearance, 'show_owned': self.show_owned,
                    'owned_view': self.owned_view, 'controls': self.controls,
                    'library_state': self.library_state, 'smart_hidden': self.smart_hidden,
                    'theatre_library': self.theatre_library,
                    'smart_shelves': self.smart_shelves,
                    'collections': self.collections,
                    'deals_state': self.deals_state, 'move_targets': self.move_targets,
                    'palette_recent': self.palette_recent, 'setup_done': self.setup_done,
                    'uac_note_shown': self.uac_note_shown,
                    'last_seen_version': self.last_seen_version,
                    'overrides': self.overrides, 'notes': self.notes})
        if self.seen_repacks is not None:
            out['seen_repacks'] = list(self.seen_repacks)[-300:]
        out['collection_look'] = {n: v for n, v in self.collection_look.items()
                                  if v and n in self.collections}
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
        if old in self.collection_look:            # its icon, colour and pin go with it
            self.collection_look[new] = self.collection_look.pop(old)
        self.save()
        return new

    def delete_collection(self, name: str):
        self.collection_look.pop(name, None)
        if self.collections.pop(name, None) is not None:
            self.save()

    def look_of(self, name: str) -> dict:
        return self.collection_look.get(name) or {}

    def pinned_collections(self) -> list[str]:
        """The pinned ones, in the collections' order."""
        return [n for n in self.collections if self.look_of(n).get('pinned')]

    def set_collection_look(self, name: str, **change) -> bool:
        """Change `icon`, `color` (None: back to the default) or `pinned`.
        False (nothing saved) for an unknown collection, or a pin past
        MAX_PINNED."""
        if name not in self.collections:
            return False
        if change.get('pinned') and not self.look_of(name).get('pinned') \
                and len(self.pinned_collections()) >= MAX_PINNED:
            return False
        look = dict(self.look_of(name))
        for k in ('icon', 'color', 'pinned'):
            if k in change:
                look.pop(k, None)
                if change[k]:
                    look[k] = change[k]
        look = clean_collection_look(look)
        if look:
            self.collection_look[name] = look
        else:
            self.collection_look.pop(name, None)
        self.save()
        return True

    def move_collection(self, name: str, delta: int) -> bool:
        """Move it `delta` places (-1 up / earlier, +1 down); False at an end."""
        names = list(self.collections)
        if name not in names:
            return False
        i = names.index(name)
        j = max(0, min(len(names) - 1, i + delta))
        if i == j:
            return False
        names.insert(j, names.pop(i))
        self.collections = {n: self.collections[n] for n in names}
        self.save()
        return True

    # ---- shelves you build (w-51) ----
    def user_shelves(self) -> list[dict]:
        """Your shelves that weren't removed, in the order they were made."""
        return [s for s in self.smart_shelves if not s['removed']]

    def find_shelf(self, sid: str) -> dict | None:
        return next((s for s in self.smart_shelves if s['id'] == sid), None)

    def new_shelf(self, fields=('playtime',)) -> dict | None:
        """Make a shelf with one starting rule; None when there are already
        MAX_USER_SHELVES (removed ones count: they're kept)."""
        if len(self.smart_shelves) >= smart.MAX_USER_SHELVES:
            return None
        taken = {s['id'] for s in self.smart_shelves}
        sid = uuid.uuid4().hex[:8]
        while sid in taken:
            sid = uuid.uuid4().hex[:8]
        shelf = smart.new_shelf(sid, fields)
        self.smart_shelves.append(shelf)
        self.save()
        return shelf

    def update_shelf(self, shelf: dict, save: bool = True) -> dict | None:
        """Put an edited shelf back in place (cleaned); the window saves a
        burst of edits once."""
        clean = smart.clean_shelf(shelf)
        i = next((i for i, s in enumerate(self.smart_shelves)
                  if clean and s['id'] == clean['id']), None)
        if i is None:
            return None
        self.smart_shelves[i] = clean
        if save:
            self.save()
        return clean

    def set_shelf_removed(self, sid: str, removed: bool):
        """Remove (or bring back) a shelf: it stays in the file either way."""
        s = self.find_shelf(sid)
        if s is not None and s['removed'] != removed:
            s['removed'] = removed
            self.save()


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .dashboard import dash_normalize  # noqa: E402
