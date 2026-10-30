"""Theatre's menu sounds (w-65): move, select, back, error, launch.

Off by default (`controls.sounds`), Theatre only, and only while Nexus is the
active app. The WAVs are original, made by tools/make_sounds.py, and live in
assets/sounds/.

`QSoundEffect` objects are created lazily (the first time a sound is wanted
in Theatre with sounds on) and released as soon as Nexus goes inactive, leaves
Theatre or sounds are turned off, so a game started from Theatre gets the
audio device to itself. Silent without QtMultimedia, without the files, and
under `NEXUS_NO_SOUND=1` (tests/sandbox.py sets it: tests never open audio).

Where the sounds come from:
    move    an arrow key (D-pad or keyboard) that moved focus (SpatialNav's
            `moved`); never a programmatic focus change, never the stick's
            scrolling. At most every 60 ms.
    select  Return, Enter or Space on a control (A on a pad; this class
            watches key presses while sounds are on).
    back    B / Escape that closed something (win_nav._escape).
    launch  Play (TheatreView.play): it replaces a select still sounding.
    error   a refused action (a warn or error toast in Theatre).
A move right after another sound is the result of it (a page opening
focuses its Play button), so it stays quiet.
"""

import os
import time

from PyQt6.QtCore import QEvent, QObject, Qt, QUrl
from PyQt6.QtWidgets import QApplication, QLineEdit

from .common import _REPO

SOUND_DIR = _REPO / 'assets' / 'sounds'
NAMES = ('move', 'select', 'back', 'error', 'launch')
CEILING = 0.7                 # full volume is 70 % of the system's
RANGE_DB = 30.0               # volume 10 is 27 dB under volume 100

ACCEPT = (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)


def gain(volume) -> float:
    """controls.volume (0-100) as QSoundEffect's linear volume: a log curve
    (each step sounds like the same change) with a 0.7 ceiling. 0 is silent."""
    try:
        v = max(0, min(100, int(volume)))
    except (TypeError, ValueError):
        return 0.0
    if v == 0:
        return 0.0
    return CEILING * 10 ** (-(1.0 - v / 100.0) * RANGE_DB / 20.0)


def _sound_effect_class():
    """QSoundEffect, or None when QtMultimedia can't be imported (a distro
    PyQt6 without it, a trimmed build)."""
    try:
        from PyQt6.QtMultimedia import QSoundEffect
    except Exception:                                      # ImportError, a missing plugin lib
        return None
    return QSoundEffect


class UiSounds(QObject):
    """The window's sounds. `in_theatre` is a callable (the window's
    `_theatre`); `configure(on, volume)` follows prefs.controls."""

    MOVE_GAP = 0.06           # s between two moves (a held D-pad repeats every 85 ms)
    QUIET_AFTER = 0.15        # s: a move this soon after another sound is its echo
    SAME_GAP = 0.05           # s: the same sound twice (Steam's Desktop layout doubling a key)

    def __init__(self, parent, in_theatre):
        super().__init__(parent)
        self._in_theatre = in_theatre
        self.disabled = os.environ.get('NEXUS_NO_SOUND') == '1'
        self.factory = _sound_effect_class        # tests hand in a fake
        self.on = False
        self.volume = 50
        self.effects: dict = {}                    # name -> QSoundEffect, while held
        self._last: dict[str, float] = {}          # name -> monotonic time it played
        self._last_any = ('', 0.0)
        self._watching = False
        app = QApplication.instance()
        self.app_active = app.applicationState() == Qt.ApplicationState.ApplicationActive
        app.applicationStateChanged.connect(self._app_state)

    # ---- settings ----
    def configure(self, on: bool, volume: int):
        self.on, self.volume = bool(on), volume
        for e in self.effects.values():
            e.setVolume(gain(volume))
        if not self.on:
            self.release()
        self._watch(self.on and not self.disabled)

    def active(self) -> bool:
        """Whether a sound would play now."""
        return self.on and not self.disabled and self.app_active and bool(self._in_theatre()) \
            and gain(self.volume) > 0 and self.factory() is not None

    # ---- playing ----
    def play(self, name: str) -> bool:
        """Play one of NAMES if sounds are on, in Theatre, in an active app.
        True if it was played."""
        if name not in NAMES or not self.active():
            return False
        now = time.monotonic()
        last_name, last_t = self._last_any
        if name == 'move':
            if now - self._last.get('move', -1.0) < self.MOVE_GAP:
                return False
            if last_name != 'move' and now - last_t < self.QUIET_AFTER:
                return False
        elif name == last_name and now - last_t < self.SAME_GAP:
            return False
        if name == 'launch':
            sel = self.effects.get('select')
            if sel is not None:
                sel.stop()                         # Play replaces the select it came with
        e = self._effect(name)
        if e is None:
            return False
        e.setVolume(gain(self.volume))
        e.play()
        self._last[name] = now
        self._last_any = (name, now)
        return True

    def sample(self):
        """The volume row changed: the select sound at the new level (it
        doesn't wait out the same-sound gap)."""
        self._last_any = ('', 0.0)
        self.play('select')

    def _effect(self, name: str):
        e = self.effects.get(name)
        if e is not None:
            return e
        path = SOUND_DIR / f'{name}.wav'
        cls = self.factory()
        if cls is None or not path.exists():
            return None
        e = cls(self)
        e.setSource(QUrl.fromLocalFile(str(path)))
        e.setVolume(gain(self.volume))
        self.effects[name] = e
        return e

    def preload(self):
        """Load every sound before it's first wanted (entering Theatre with
        sounds on), so the first move isn't late. Nothing when inactive."""
        if self.active():
            for name in NAMES:
                self._effect(name)

    def release(self):
        """Let go of every QSoundEffect (and with them the audio device)."""
        effects, self.effects = self.effects, {}
        for e in effects.values():
            e.stop()
            e.deleteLater()

    def _app_state(self, state):
        self.app_active = state == Qt.ApplicationState.ApplicationActive
        if not self.app_active:
            self.release()                         # a game may want the device

    # ---- select, from the keys ----
    def _watch(self, on: bool):
        if on == self._watching:
            return
        self._watching = on
        app = QApplication.instance()
        if on:
            app.installEventFilter(self)
        else:
            app.removeEventFilter(self)

    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.Type.KeyPress or ev.key() not in ACCEPT or ev.isAutoRepeat():
            return False
        if ev.modifiers() & ~Qt.KeyboardModifier.KeypadModifier or not self.active():
            return False
        focus = QApplication.focusWidget()
        # The filter sees a key once per receiver as it climbs to the parents.
        if focus is not None and obj is focus and focus.isEnabled() \
                and not isinstance(focus, QLineEdit):
            self.play('select')
        return False
