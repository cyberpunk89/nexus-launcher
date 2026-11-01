"""Theatre's menu sounds (w-65): move, select, back, error, launch.

Off by default (`controls.sounds`), Theatre only, and only while Nexus is the
active app. The WAVs are original, made by tools/make_sounds.py, and live in
assets/sounds/.

`WavEffect`s (a QAudioSink each; why not QSoundEffect is in its docstring)
are created lazily (the first time a sound is wanted in Theatre with sounds
on) and released as soon as Nexus goes inactive, leaves
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

When sounds can't play (no QtMultimedia, no audio output, an effect in
Status.Error) `problem()` says why, and each reason goes to stderr once a
session (w-73). Settings shows it under the switch and has a Test sound.
"""

import enum
import os
import sys
import time
import wave

from PyQt6.QtCore import (QBuffer, QByteArray, QEvent, QIODevice, QObject, Qt, QTimer, QUrl,
                          pyqtSignal)
from PyQt6.QtWidgets import QApplication, QLineEdit

from .common import _REPO

SOUND_DIR = _REPO / 'assets' / 'sounds'
NAMES = ('move', 'select', 'back', 'error', 'launch')
CEILING = 0.7                 # full volume is 70 % of the system's
RANGE_DB = 30.0               # volume 10 is 27 dB under volume 100

ACCEPT = (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)

NO_MULTIMEDIA = "Sounds can't play: this PyQt6 has no QtMultimedia."
NO_OUTPUT = "Sounds can't play: no audio output found. Check your sound settings."
FAILED = "Sounds can't play: the audio system refused them. Check your sound settings."
_logged: set = set()          # reasons already written to stderr this session


def gain(volume) -> float:
    """controls.volume (0-100) as the effects' linear volume: a log curve
    (each step sounds like the same change) with a 0.7 ceiling. 0 is silent."""
    try:
        v = max(0, min(100, int(volume)))
    except (TypeError, ValueError):
        return 0.0
    if v == 0:
        return 0.0
    return CEILING * 10 ** (-(1.0 - v / 100.0) * RANGE_DB / 20.0)


class WavEffect(QObject):
    """The part of QSoundEffect UiSounds uses, played through a QAudioSink.

    QSoundEffect tags its stream media.role=event, and PipeWire/PulseAudio
    mute that role whenever "System Sounds" is muted (pavucontrol), so the
    sounds were silent there with no error. A QAudioSink stream is an ordinary
    one, under Nexus's own volume. The WAV is read once by `wave`; each play
    starts the sink on a fresh buffer over the same bytes."""

    class Status(enum.Enum):
        Null, Loading, Ready, Error = range(4)

    statusChanged = pyqtSignal()
    TAIL_MS = 400             # after Idle, before the sink is stopped

    def __init__(self, parent=None):
        super().__init__(parent)
        self._status = self.Status.Null
        self._volume = 1.0
        self._data = None                        # QByteArray of the frames
        self._format = None                      # QAudioFormat
        self._sink = None
        self._buf = None
        # Child timer: dies with the effect, so it never fires after release().
        self._tail = QTimer(self, singleShot=True, interval=self.TAIL_MS)
        self._tail.timeout.connect(self._idle_stop)

    def status(self):
        return self._status

    def _set_status(self, st):
        if st != self._status:
            self._status = st
            self.statusChanged.emit()

    def setSource(self, url):
        from PyQt6.QtMultimedia import QAudioFormat
        fmt = {1: QAudioFormat.SampleFormat.UInt8, 2: QAudioFormat.SampleFormat.Int16,
               4: QAudioFormat.SampleFormat.Int32}
        try:
            with wave.open(url.toLocalFile(), 'rb') as w:
                width, rate, channels = w.getsampwidth(), w.getframerate(), w.getnchannels()
                frames = w.readframes(w.getnframes())
        except (OSError, EOFError, wave.Error):
            self._set_status(self.Status.Error)
            return
        if width not in fmt:
            self._set_status(self.Status.Error)
            return
        f = QAudioFormat()
        f.setSampleRate(rate)
        f.setChannelCount(channels)
        f.setSampleFormat(fmt[width])
        self._format, self._data = f, QByteArray(frames)
        self._set_status(self.Status.Ready)

    def volume(self) -> float:
        return self._volume

    def setVolume(self, v: float):
        self._volume = float(v)
        if self._sink is not None:
            self._sink.setVolume(self._volume)

    def isPlaying(self) -> bool:
        from PyQt6.QtMultimedia import QAudio
        return self._sink is not None and self._sink.state() == QAudio.State.ActiveState

    def play(self):
        if self._status != self.Status.Ready:
            return
        from PyQt6.QtMultimedia import QAudioSink, QMediaDevices
        self.stop()
        if self._sink is None:
            # The output at the first play: a sink can't follow a device change,
            # and UiSounds lets go of its effects often enough (inactive, Test).
            self._sink = QAudioSink(QMediaDevices.defaultAudioOutput(), self._format, self)
            self._sink.stateChanged.connect(self._state)
        self._sink.setVolume(self._volume)
        self._tail.stop()
        self._buf = QBuffer(self._data, self)
        self._buf.open(QIODevice.OpenModeFlag.ReadOnly)
        self._sink.start(self._buf)

    def _state(self, state):
        from PyQt6.QtMultimedia import QAudio
        if state == QAudio.State.IdleState:
            # The buffer is read, not yet heard (a short sound reports Idle
            # before its end; Bluetooth lags ~200 ms): stop() now would cut it.
            self._tail.start()
        elif state == QAudio.State.StoppedState \
                and self._sink.error() in (QAudio.Error.OpenError, QAudio.Error.FatalError):
            self._set_status(self.Status.Error)

    def _idle_stop(self):
        from PyQt6.QtMultimedia import QAudio
        if self._sink is not None and self._sink.state() == QAudio.State.IdleState:
            self.stop()

    def stop(self):
        if self._sink is not None:
            self._sink.stop()
        if self._buf is not None:
            self._buf.close()
            self._buf.deleteLater()
            self._buf = None


def _sound_effect_class():
    """WavEffect, or None when QtMultimedia can't be imported (a distro
    PyQt6 without it, a trimmed build)."""
    try:
        from PyQt6.QtMultimedia import QAudioSink  # noqa: F401
    except Exception:                                      # ImportError, a missing plugin lib
        return None
    return WavEffect


def _audio_outputs():
    """The audio outputs Qt sees, or None without QtMultimedia."""
    try:
        from PyQt6.QtMultimedia import QMediaDevices
    except Exception:
        return None
    return QMediaDevices.audioOutputs()


class UiSounds(QObject):
    """The window's sounds. `in_theatre` is a callable (the window's
    `_theatre`); `configure(on, volume)` follows prefs.controls."""

    problem_found = pyqtSignal(str)          # a new reason sounds can't play ('' when fixed)

    MOVE_GAP = 0.06           # s between two moves (a held D-pad repeats every 85 ms)
    QUIET_AFTER = 0.15        # s: a move this soon after another sound is its echo
    SAME_GAP = 0.05           # s: the same sound twice (Steam's Desktop layout doubling a key)

    def __init__(self, parent, in_theatre):
        super().__init__(parent)
        self._in_theatre = in_theatre
        self.disabled = os.environ.get('NEXUS_NO_SOUND') == '1'
        self.factory = _sound_effect_class        # tests hand in a fake
        self.outputs = _audio_outputs             # and a fake device list
        self.reason = ''                          # the last problem() found
        self._failed = False                      # an effect reached Status.Error
        self.on = False
        self.volume = 50
        self.effects: dict = {}                    # name -> WavEffect, while held
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

    def problem(self) -> str:
        """Why sounds can't play here, or ''. Looks at QtMultimedia and the
        audio outputs (never under NEXUS_NO_SOUND); logs a new reason once."""
        if self.disabled:
            return ''
        if self.factory() is None:
            reason = NO_MULTIMEDIA
        elif not self.outputs():
            reason = NO_OUTPUT
        else:
            reason = FAILED if self._failed else ''
        self._set_reason(reason)
        return reason

    def _set_reason(self, reason: str):
        if reason and reason not in _logged:
            _logged.add(reason)
            sys.stderr.write(f"nexus: menu sounds: {reason}\n")
        if reason != self.reason:
            self.reason = reason
            self.problem_found.emit(reason)

    def _status(self, e):
        """An effect's status changed: Error is a device or decode failure."""
        if e.status() == type(e).Status.Error and not self._failed:
            self._failed = True
            self.problem()

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
        if not self._play(name):
            return False
        self._last[name] = now
        self._last_any = (name, now)
        return True

    def _play(self, name: str) -> bool:
        e = self._effect(name)
        if e is None:
            return False
        e.setVolume(gain(self.volume))
        e.play()
        return True

    def test(self) -> bool:
        """Settings' Test sound: select at the chosen volume, outside Theatre
        and with sounds off too (not under NEXUS_NO_SOUND, not at volume 0).
        Outside Theatre the effects are let go of again after 3 s."""
        if self.disabled or gain(self.volume) <= 0:
            return False
        self.release()                             # a fresh try: new effects, new status
        if self.problem():
            return False
        played = self._play('select')
        QTimer.singleShot(3000, self._after_test)
        return played

    def _after_test(self):
        if not self.active():
            self.release()

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
        if not self.effects:
            self.problem()                         # the first one: log why it won't play
        e = cls(self)
        if hasattr(e, 'statusChanged'):
            e.statusChanged.connect(lambda e=e: self._status(e))
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
        """Let go of every effect (and with them the audio device)."""
        effects, self.effects = self.effects, {}
        self._failed = False                       # the next effects get another chance
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
