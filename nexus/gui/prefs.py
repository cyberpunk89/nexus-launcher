"""LibraryPrefs: the GUI's own settings and per-game notes (library.json)."""

import json
import sys
import uuid
from datetime import datetime

from nexus import net, paths as NX, smart
from nexus.icons import COLLECTION_ICONS

from .moments import (clean_kinds_off, is_snoozed, quiet_until_next, SNOOZE_MAX, snooze_key,
    snooze_until)
from .common import (ATTRACT_MINUTES, MAX_COLLECTION_NAME, MAX_NOTE, MAX_TITLE, THEATRE_STARTS,
    VOLUME_STEPS)
from .theme import ACCENTS, normalize_appearance, TILE_W

MAX_PINNED = 3                    # collections pinned to the top bar (w-52)
MAX_KINDS_OFF = 20                # For you now kinds switched off (w-88)


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


def _snoozes(raw, now: datetime) -> dict:
    """For you now's Not now snoozes {'kind:key': 'YYYY-MM-DD'}: the ones
    still running, the newest SNOOZE_MAX (a bad entry is dropped)."""
    if not isinstance(raw, dict):
        return {}
    live = [(k, v) for k, v in raw.items()
            if isinstance(k, str) and ':' in k and is_snoozed(raw, k, now)]
    return dict(live[-SNOOZE_MAX:])


def _quiet_until(raw, now: datetime) -> datetime | None:
    """When Quiet until tomorrow ends (a local ISO time), or None once past."""
    try:
        t = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if t is not None and t.tzinfo:
        t = t.astimezone().replace(tzinfo=None)
    return t if t is not None and t > now else None


def _volume(v) -> int:
    """controls.volume: 0-100 in steps of 10; anything else is 50."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return 50
    return min(VOLUME_STEPS, key=lambda s: abs(s - max(0, min(100, v))))


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
        raw = dash.get('items') if isinstance(dash, dict) else None
        ver = dash.get('version') if isinstance(dash, dict) else None
        self.dashboard_version = ver if isinstance(ver, int) and not isinstance(ver, bool) \
            and ver >= 1 else 1
        self.dashboard = dash_normalize(raw, self.dashboard_version)
        # For you now's dismiss state (w-88), next to the layout.
        opts = dash if isinstance(dash, dict) else {}
        now = datetime.now()
        self.snoozes = _snoozes(opts.get('snoozes'), now)
        self.quiet_until = _quiet_until(opts.get('quiet_until'), now)
        self.kinds_off = clean_kinds_off(opts.get('kinds_off'))[:MAX_KINDS_OFF]
        # Customise → Density (w-90): Home's spacing.
        dens = opts.get('density')
        self.density = dens if isinstance(dens, str) and dens in DASH_DENSITY else 'comfortable'
        # The layout as saved by an older Nexus, kept once before the first save
        # writes the new format (w-86); Customise can offer it back later.
        self._dashboard_v1 = raw if isinstance(raw, list) and raw \
            and self.dashboard_version < DASH_VERSION \
            and 'dashboard_v1' not in self._data else None
        # Saved layouts (w-92): named snapshots of the items and density, the
        # one Home is on now (`active`, edits write through to it), the one
        # last chosen by hand (`base`, where the clock goes back to), and
        # whether "My old Home" was taken from dashboard_v1.
        self.layouts = clean_layouts(opts.get('layouts'))
        self.active = self._layout_name(opts.get('active'))
        self.base = self._layout_name(opts.get('base'))
        self.v1_taken = opts.get('v1_taken') is True
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
            # Theatre (w-63): start Nexus in it, and switch to it when a pad
            # connects (stored here; w-64 acts on them).
            'theatre_start': ctl.get('theatre_start') if ctl.get('theatre_start') in
            THEATRE_STARTS else 'never',
            'theatre_on_pad': ctl.get('theatre_on_pad') is True,
            # Menu sounds in Theatre and their volume (w-65 plays them).
            'sounds': ctl.get('sounds') is True,
            'volume': _volume(ctl.get('volume')),
            # The "Your week" card on Theatre's Home.
            'week_home': ctl.get('week_home') is not False,
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
        #  'show': 'installed'|'missing'|'all'|'friends' (w-82: Friends own)
        #          or a Play together key 'friends:<ids>[:multi]' (w-83)}
        tl = self._data.get('theatre_library')
        show = tl.get('show') if isinstance(tl, dict) else None
        self.theatre_library = {
            'sort': tl.get('sort') if isinstance(tl, dict) and tl.get('sort') in (
                'name', 'played', 'most', 'added', 'collection') else 'name',
            'show': show if show in ('installed', 'missing', 'all', 'friends')
            or smart.parse_friends_key(show) is not None else 'installed'}
        # Theatre's Discover: the Game-db setup card, hidden with X.
        self.theatre_hide_gamedb = bool(self._data.get('theatre_hide_gamedb', False))
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
        self.write_through()
        # Keys this version doesn't know about are kept, not dropped.
        out = dict(self._data)
        if self._dashboard_v1 is not None:
            out['dashboard_v1'] = self._dashboard_v1
        out.update({'favorites': sorted(self.favorites), 'hidden': sorted(self.hidden),
                    'view': self.view, 'tile': self.tile,
                    # Merged, so keys a newer Nexus keeps here (layouts, active) stay.
                    'dashboard': {**(self._data.get('dashboard')
                                     if isinstance(self._data.get('dashboard'), dict) else {}),
                                  'version': max(self.dashboard_version, DASH_VERSION),
                                  'items': self.dashboard, 'snoozes': self.snoozes,
                                  'quiet_until': self.quiet_until.isoformat()
                                  if self.quiet_until else None,
                                  'kinds_off': self.kinds_off, 'density': self.density,
                                  'layouts': self.layouts, 'active': self.active,
                                  'base': self.base, 'v1_taken': self.v1_taken},
                    'appearance': self.appearance, 'show_owned': self.show_owned,
                    'owned_view': self.owned_view,
                    # A newer version's controls keys are kept too.
                    'controls': {**(self._data.get('controls')
                                    if isinstance(self._data.get('controls'), dict) else {}),
                                 **self.controls},
                    'library_state': self.library_state, 'smart_hidden': self.smart_hidden,
                    'theatre_library': self.theatre_library,
                    'theatre_hide_gamedb': self.theatre_hide_gamedb,
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

    # ---- saved layouts (w-92) ----
    def _layout_name(self, name) -> str | None:
        lt = find_layout(self.layouts, name)
        return lt['name'] if lt else None

    def layout(self, name) -> dict | None:
        return find_layout(self.layouts, name)

    def write_through(self):
        """Home is on a saved layout: it follows what Home shows now."""
        lt = self.layout(self.active)
        if lt is not None:
            lt['items'] = [dict(i) for i in self.dashboard]
            lt['density'] = self.density

    def old_home(self) -> list | None:
        """The layout saved by an older Nexus (top-level dashboard_v1, or the
        one this run will write there), when it can still be offered as
        "My old Home": not taken before and no layout has that name."""
        raw = self._data.get('dashboard_v1')
        raw = raw if isinstance(raw, list) else self._dashboard_v1
        if not (isinstance(raw, list) and raw) or self.v1_taken \
                or self.layout(OLD_HOME) is not None:
            return None
        return raw

    def _make_room(self) -> bool:
        """A free slot for one more layout: at the cap the oldest one saved
        for you (auto) goes. False when every layout is your own."""
        if len(self.layouts) < MAX_LAYOUTS:
            return True
        old = next((lt for lt in self.layouts if lt.get('auto')), None)
        if old is None:
            return False
        self.delete_layout(old['name'], save=False)
        return True

    def add_layout(self, name: str, items, density: str | None = None, auto: bool = False,
                   save: bool = True) -> dict | None:
        """A new layout (its name made unique) with these items; None when
        12 are saved and none of them was saved for you."""
        name = clean_name(name)
        if name is None or not self._make_room():
            return None
        lt = {'name': unique_name(self.layouts, name),
              'items': dash_normalize([dict(i) for i in items])}
        if density in DASH_DENSITY:
            lt['density'] = density
        if auto:
            lt['auto'] = True
        self.layouts.append(lt)
        if save:
            self.save()
        return lt

    def keep_current(self, label: str) -> str:
        """Before a preset (or My old Home) replaces Home: save what it shows.
        'kept' when it is already a saved layout (active, or the same items
        and density as one), 'saved' as `label` (auto), 'full' when there is
        no room."""
        self.write_through()
        if self.layout(self.active) is not None or any(
                same_layout(lt, self.dashboard, self.density) for lt in self.layouts):
            return 'kept'
        lt = self.add_layout(label, self.dashboard, self.density, auto=True, save=False)
        return 'saved' if lt else 'full'

    def save_as(self, name: str) -> dict | None:
        """Save as…: a copy of Home now, which Home is then on."""
        self.write_through()
        lt = self.add_layout(name, self.dashboard, self.density, save=False)
        if lt is not None:
            self.active = self.base = lt['name']
            self.save()
        return lt

    def rename_layout(self, old: str, new: str) -> str | None:
        lt, new = self.layout(old), clean_name(new)
        if lt is None or new is None:
            return None
        others = [o for o in self.layouts if o is not lt]
        name = unique_name(others, new)
        for attr in ('active', 'base'):
            if getattr(self, attr) == lt['name']:
                setattr(self, attr, name)
        lt['name'] = name
        lt.pop('auto', None)                     # named by you now
        self.save()
        return name

    def delete_layout(self, name: str, save: bool = True):
        lt = self.layout(name)
        if lt is None:
            return
        self.layouts.remove(lt)
        if self.active == lt['name']:
            self.active = None
        if self.base == lt['name']:
            self.base = None
        if save:
            self.save()

    def set_rule(self, name: str, rule: dict | None):
        lt = self.layout(name)
        if lt is None:
            return
        rule = clean_rule(rule)
        if rule is None:
            lt.pop('rule', None)
        else:
            lt['rule'] = rule
        self.save()

    # ---- For you now (w-88) ----
    def snooze(self, kind: str, key: str, now: datetime):
        """Not now: hide `kind` (a moment's kind or alert kind) for the game
        `key` for SNOOZE_DAYS, and save."""
        k = snooze_key(kind, key)
        self.snoozes.pop(k, None)                 # snoozed again: the newest once more
        self.snoozes[k] = snooze_until(now)
        self.snoozes = _snoozes(self.snoozes, now)
        self.save()

    def quiet_tomorrow(self, now: datetime):
        """Quiet until tomorrow: until the next 04:00 local time."""
        self.quiet_until = quiet_until_next(now)
        self.save()

    def clear_quiet(self):
        if self.quiet_until is not None:
            self.quiet_until = None
            self.save()

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

    def add_shelf(self, draft: dict) -> dict | None:
        """Save a shelf made in the New shelf modal (w-67) under a fresh id,
        cleaned, in one write; None when there are already MAX_USER_SHELVES
        (removed ones count: they're kept)."""
        if len(self.smart_shelves) >= smart.MAX_USER_SHELVES:
            return None
        taken = {s['id'] for s in self.smart_shelves}
        sid = uuid.uuid4().hex[:8]
        while sid in taken:
            sid = uuid.uuid4().hex[:8]
        shelf = smart.clean_shelf({**draft, 'id': sid, 'removed': False})
        self.smart_shelves.append(shelf)
        self.save()
        return shelf

    def update_shelf(self, shelf: dict) -> dict | None:
        """Put an edited shelf back in place (cleaned) and save."""
        clean = smart.clean_shelf(shelf)
        i = next((i for i, s in enumerate(self.smart_shelves)
                  if clean and s['id'] == clean['id']), None)
        if i is None:
            return None
        self.smart_shelves[i] = clean
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
from .dashboard import DASH_DENSITY, DASH_VERSION, dash_normalize  # noqa: E402
from .layouts import (clean_layouts, clean_name, clean_rule, find_layout,  # noqa: E402
    MAX_LAYOUTS, OLD_HOME, same_layout, unique_name)
