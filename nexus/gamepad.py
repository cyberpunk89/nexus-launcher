"""Game-controller input through SDL's GameController API (ctypes, no extra
Python packages). SDL maps Xbox, PlayStation, Switch Pro, Steam Deck/Steam
Controller (via Steam) and most generic pads to one standard layout.

Used by the GUI's poll timer: `Gamepads.poll()` returns abstract events —
('button', name, pressed), ('axis', name, value -1..1), ('added', name, kind,
guid, hotplug), ('removed', name). `hotplug` says a pad really arrived (plugged
in, turned on), not SDL listing one that was already there. It never
blocks, and it's inert (available=False) when no SDL library is found (on Windows: SDL2.dll shipped beside the exe)."""

import ctypes
import ctypes.util
import time
from collections import Counter
from pathlib import Path

from . import platform

POWER_SUPPLY = Path('/sys/class/power_supply')
# Kernel drivers that expose a pad's battery: hid-playstation (DualSense,
# DualShock 4), hid-sony (older), xpadneo, hid-nintendo, hid-steam.
PAD_BATTERY_PREFIXES = ('ps-controller-battery-', 'sony_controller_battery_',
                        'xpadneo-', 'nintendo_switch_controller_battery_',
                        'steam-controller-battery-')

# SDL2 event types (sdl2-compat keeps these on SDL3 systems).
CONTROLLERAXISMOTION = 0x650
CONTROLLERBUTTONDOWN = 0x651
CONTROLLERBUTTONUP = 0x652
CONTROLLERDEVICEADDED = 0x653
CONTROLLERDEVICEREMOVED = 0x654
INIT_GAMECONTROLLER = 0x2000
INIT_EVENTS = 0x4000
# SDL_JoystickPowerLevel -> percent (the top of each band). UNKNOWN (-1) and
# WIRED (4) have no battery to show.
POWER_PERCENT = {0: 5, 1: 20, 2: 70, 3: 100, 5: 100}

BUTTONS = ['a', 'b', 'x', 'y', 'back', 'guide', 'start', 'lstick', 'rstick',
           'lb', 'rb', 'up', 'down', 'left', 'right']
AXES = ['lx', 'ly', 'rx', 'ry', 'lt', 'rt']
# Steam Input's virtual pad (Steam's "Desktop"/game layouts): the same physical
# controller seen a second time. A Steam Deck's own controls are 28de:1205.
STEAM_VIRTUAL = (0x28de, 0x11ff)
# SDL_GameControllerType -> glyph family for on-screen hints.
# After SDL starts (or resume() restarts it) it lists every pad already
# connected as an ADDED: none of that is a hot-plug. The pads open at
# suspend() are remembered for a while longer, so one that comes back late
# (Bluetooth) still isn't.
SETTLE = 2.0
REMEMBER = 10.0
KINDS = {1: 'xbox', 2: 'xbox', 3: 'ps', 4: 'ps', 5: 'nintendo', 6: 'generic', 7: 'ps',
         8: 'generic', 9: 'generic', 10: 'nintendo', 11: 'nintendo', 12: 'nintendo'}


# Sony pads under the kernel's hid-playstation driver (read via evdev, since
# HIDAPI is off). Its evdev codes come out as buttons 0 cross, 1 circle,
# 2 triangle, 3 square, 4/5 L1/R1, 8 create, 9 options, 10 PS, 11/12 sticks and
# axes 0/1 left stick, 2 L2, 3/4 right stick, 5 R2. SDL's database entry for
# these pads still assumes the old hid-sony order, which turned circle into
# "A" (select) and cross into "X" (menu), so we hand SDL the right mapping.
SONY_VENDOR = 0x054c
SONY_PRODUCTS = {0x0ce6: 'PS5 Controller', 0x0df2: 'PS5 Controller',   # DualSense, Edge
                 0x05c4: 'PS4 Controller', 0x09cc: 'PS4 Controller'}   # DualShock 4
PLAYSTATION_MODULE = Path('/sys/module/hid_playstation')
PLAYSTATION_MAP = ('a:b0,b:b1,x:b3,y:b2,back:b8,start:b9,guide:b10,leftshoulder:b4,'
                   'rightshoulder:b5,leftstick:b11,rightstick:b12,leftx:a0,lefty:a1,'
                   'rightx:a3,righty:a4,lefttrigger:a2,righttrigger:a5,'
                   'dpup:h0.1,dpright:h0.2,dpdown:h0.4,dpleft:h0.8,platform:Linux,')


def playstation_mapping(guid: str, product: int, vendor: int = SONY_VENDOR,
                        module: Path = PLAYSTATION_MODULE) -> str | None:
    """An SDL mapping line for a Sony pad driven by hid-playstation, else None."""
    if vendor != SONY_VENDOR or product not in SONY_PRODUCTS or not module.exists():
        return None
    return f"{guid},{SONY_PRODUCTS[product]},{PLAYSTATION_MAP}"


class _GUID(ctypes.Structure):
    _fields_ = [('data', ctypes.c_uint8 * 16)]


class _Event(ctypes.Structure):
    _fields_ = [('type', ctypes.c_uint32), ('timestamp', ctypes.c_uint32),
                ('which', ctypes.c_int32), ('b0', ctypes.c_uint8), ('b1', ctypes.c_uint8),
                ('b2', ctypes.c_uint8), ('b3', ctypes.c_uint8), ('value', ctypes.c_int16),
                ('_pad', ctypes.c_uint8 * 38)]            # SDL_Event is a 56-byte union


# The Gamepads that's open, so battery() can ask SDL where sysfs has nothing.
_live = None


def _load():
    for name in (*platform.sdl_library_paths(), ctypes.util.find_library('SDL2') or ''):
        if not name:
            continue
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


class Gamepads:
    """Open while the launcher window is focused, released (`suspend`) while
    it isn't — so a game you started never shares its controller with us."""

    def __init__(self, load: bool = True):
        self.lib = _load() if load else None
        self.available = False
        self.active = False
        self.pads: dict[int, tuple] = {}              # instance id -> (handle, name, kind)
        self.virtual: set[int] = set()                # ids of Steam's virtual pads
        self.guids: dict[int, str] = {}               # instance id -> SDL GUID ('' unknown)
        self._expect: Counter = Counter()             # GUIDs open at the last suspend()
        self._settle_until = 0.0
        self._expect_until = 0.0
        if not self.lib:
            return
        L = self.lib
        L.SDL_SetHint.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        # The Qt window, not SDL, has keyboard focus, so SDL must deliver
        # "background" events; the GUI only acts on them while it's focused.
        L.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
        # Never take over SIGINT/SIGTERM from the application.
        L.SDL_SetHint(b"SDL_NO_SIGNAL_HANDLERS", b"1")
        # Linux: evdev only, HIDAPI off. Windows: HIDAPI on (no evdev there),
        # a DualSense kept in simple mode. See platform.sdl_hints.
        for hint, value in platform.sdl_hints().items():
            L.SDL_SetHint(hint, value)
        L.SDL_Init.argtypes = [ctypes.c_uint32]
        L.SDL_InitSubSystem.argtypes = [ctypes.c_uint32]
        L.SDL_QuitSubSystem.argtypes = [ctypes.c_uint32]
        if L.SDL_Init(INIT_EVENTS) != 0:
            return
        L.SDL_PollEvent.argtypes = [ctypes.POINTER(_Event)]
        L.SDL_GameControllerOpen.restype = ctypes.c_void_p
        L.SDL_GameControllerOpen.argtypes = [ctypes.c_int]
        L.SDL_GameControllerName.restype = ctypes.c_char_p
        L.SDL_GameControllerName.argtypes = [ctypes.c_void_p]
        L.SDL_GameControllerGetJoystick.restype = ctypes.c_void_p
        L.SDL_GameControllerGetJoystick.argtypes = [ctypes.c_void_p]
        L.SDL_JoystickInstanceID.restype = ctypes.c_int32
        L.SDL_JoystickInstanceID.argtypes = [ctypes.c_void_p]
        L.SDL_GameControllerClose.argtypes = [ctypes.c_void_p]
        self._ids = []
        for fn in ('SDL_GameControllerGetVendor', 'SDL_GameControllerGetProduct'):
            f = getattr(L, fn, None)
            if f:
                f.restype = ctypes.c_uint16
                f.argtypes = [ctypes.c_void_p]
                self._ids.append(f)
        self._fix = False
        try:
            L.SDL_JoystickGetDeviceGUID.restype = _GUID
            L.SDL_JoystickGetDeviceGUID.argtypes = [ctypes.c_int]
            L.SDL_JoystickGetGUIDString.argtypes = [_GUID, ctypes.c_char_p, ctypes.c_int]
            L.SDL_JoystickGetDeviceVendor.restype = ctypes.c_uint16
            L.SDL_JoystickGetDeviceVendor.argtypes = [ctypes.c_int]
            L.SDL_JoystickGetDeviceProduct.restype = ctypes.c_uint16
            L.SDL_JoystickGetDeviceProduct.argtypes = [ctypes.c_int]
            L.SDL_GameControllerAddMapping.argtypes = [ctypes.c_char_p]
            self._fix = True
        except AttributeError:
            self._fix = False
        self._type = getattr(L, 'SDL_GameControllerGetType', None)
        if self._type:
            self._type.restype = ctypes.c_int
            self._type.argtypes = [ctypes.c_void_p]
        self._power = getattr(L, 'SDL_JoystickCurrentPowerLevel', None)
        if self._power:
            self._power.restype = ctypes.c_int
            self._power.argtypes = [ctypes.c_void_p]
        self.available = True
        global _live
        _live = self
        self.resume()

    def resume(self):
        if self.available and not self.active:
            self.active = self.lib.SDL_InitSubSystem(INIT_GAMECONTROLLER) == 0
            if self.active:                     # what SDL lists now was already there
                now = time.monotonic()
                self._settle_until = now + SETTLE
                self._expect_until = now + REMEMBER

    def suspend(self):
        """Close every controller and stop the controller subsystem."""
        if not (self.available and self.active):
            return
        for h, _n, _k in self.pads.values():
            self.lib.SDL_GameControllerClose(h)
        self._expect = Counter(g for g in self.guids.values() if g)
        self.pads.clear()
        self.virtual.clear()
        self.guids.clear()
        self.lib.SDL_QuitSubSystem(INIT_GAMECONTROLLER)
        self.active = False

    def shutdown(self):
        """Release everything; call once when the application quits."""
        if self.lib:
            self.suspend()
            self.lib.SDL_Quit()
            self.available = False
        global _live
        if _live is self:
            _live = None

    def poll(self) -> list[tuple]:
        if not (self.available and self.active):
            return []
        out = []
        ev = _Event()
        L = self.lib
        while L.SDL_PollEvent(ctypes.byref(ev)):
            t = ev.type
            if t == CONTROLLERDEVICEADDED:
                guid = self._guid(ev.which)
                self._remap(ev.which, guid)
                h = L.SDL_GameControllerOpen(ev.which)
                if not h:
                    continue
                iid = L.SDL_JoystickInstanceID(L.SDL_GameControllerGetJoystick(h))
                if iid in self.pads:
                    continue
                name = (L.SDL_GameControllerName(h) or b'Controller').decode('utf-8', 'replace')
                kind = KINDS.get(self._type(h), 'xbox') if self._type else 'xbox'
                self.pads[iid] = (h, name, kind)
                self.guids[iid] = guid
                if len(self._ids) == 2 and tuple(f(h) for f in self._ids) == STEAM_VIRTUAL:
                    self.virtual.add(iid)
                out.append(('added', name, kind, guid, self._hotplug(iid, guid)))
            elif t == CONTROLLERDEVICEREMOVED:
                pad = self.pads.pop(ev.which, None)
                self.virtual.discard(ev.which)
                self.guids.pop(ev.which, None)
                if pad:
                    L.SDL_GameControllerClose(pad[0])
                    out.append(('removed', pad[1]))
            elif self.shadowed(ev.which):
                continue
            elif t in (CONTROLLERBUTTONDOWN, CONTROLLERBUTTONUP):
                if ev.b0 < len(BUTTONS):
                    out.append(('button', BUTTONS[ev.b0], t == CONTROLLERBUTTONDOWN))
            elif t == CONTROLLERAXISMOTION:
                if ev.b0 < len(AXES):
                    out.append(('axis', AXES[ev.b0], max(-1.0, ev.value / 32767.0)))
        return out

    def _guid(self, index: int) -> str:
        """The pad's SDL GUID (its bus, vendor, product and version, so two
        pads of one model share it); '' when this SDL can't say."""
        if not self._fix:
            return ''
        L = self.lib
        buf = ctypes.create_string_buffer(64)
        L.SDL_JoystickGetGUIDString(L.SDL_JoystickGetDeviceGUID(index), buf, 64)
        return buf.value.decode('ascii', 'replace')

    def _remap(self, index: int, guid: str):
        """Correct SDL's mapping for a hid-playstation pad before opening it."""
        if not self._fix:
            return
        L = self.lib
        line = playstation_mapping(guid, L.SDL_JoystickGetDeviceProduct(index),
                                   L.SDL_JoystickGetDeviceVendor(index))
        if line:
            L.SDL_GameControllerAddMapping(line.encode())

    def _hotplug(self, iid: int, guid: str) -> bool:
        """A pad that just arrived for real: not Steam's virtual copy, not
        SDL listing what was there when it (re)started, and not a pad that was
        open at suspend() coming back (each remembered GUID matches once)."""
        now = time.monotonic()
        if now > self._expect_until:
            self._expect.clear()
        remembered = bool(guid) and self._expect[guid] > 0
        if remembered:
            self._expect[guid] -= 1
        return not (iid in self.virtual or now < self._settle_until or remembered)

    def shadowed(self, iid: int) -> bool:
        """Input from Steam's virtual pad while the physical one is open too:
        the same press twice, so drop the virtual copy."""
        return iid in self.virtual and any(i not in self.virtual for i in self.pads)

    @property
    def steam_input(self) -> bool:
        """Steam Input has the pad (its virtual pad is open): Steam may be
        remapping it, and a game sees Steam's copy."""
        return bool(self.virtual)

    def battery(self) -> list[dict]:
        """The open pads' batteries as SDL reports them: [{percent,
        charging}], lowest first. SDL can't tell charging, so never True."""
        if not (self._power and self.active):
            return []
        out = []
        for iid, (h, _n, _k) in self.pads.items():
            if iid in self.virtual:
                continue
            level = self._power(self.lib.SDL_GameControllerGetJoystick(h))
            if level in POWER_PERCENT:
                out.append({'percent': POWER_PERCENT[level], 'charging': False})
        return sorted(out, key=lambda b: b['percent'])

    @property
    def kind(self) -> str | None:
        return next(iter(self.pads.values()))[2] if self.pads else None

    @property
    def names(self) -> list[str]:
        return [p[1] for p in self.pads.values()]

    @property
    def physical(self) -> list[str]:
        """The open pads' names, Steam's virtual pads left out."""
        return [p[1] for iid, p in self.pads.items() if iid not in self.virtual]


def battery(root: Path = POWER_SUPPLY) -> list[dict]:
    """Connected controllers' batteries from sysfs: [{percent, charging}],
    lowest first. Empty when no pad reports one (wired-only, or none).
    Where sysfs has none (Windows), the open Gamepads asks SDL."""
    out = []
    try:
        entries = list(root.iterdir())
    except OSError:
        entries = []
    if not entries:
        return _live.battery() if _live is not None and root == POWER_SUPPLY else []
    for d in entries:
        if not d.name.startswith(PAD_BATTERY_PREFIXES):
            continue
        try:
            pct = int((d / 'capacity').read_text(encoding='utf-8').strip())
        except (OSError, ValueError):
            continue
        try:
            status = (d / 'status').read_text(encoding='utf-8').strip()
        except OSError:
            status = ''
        out.append({'percent': max(0, min(100, pct)),
                    'charging': status in ('Charging', 'Full')})
    return sorted(out, key=lambda b: b['percent'])
