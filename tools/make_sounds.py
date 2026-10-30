"""Theatre's menu sounds, synthesised from scratch: original work, MIT like
the rest of Nexus (no samples, no third-party packs, so no licence trail).
Pure Python, standard library only: writes 48 kHz 16-bit mono WAVs that
Qt's QSoundEffect plays as they are (nexus/gui/sounds.py).

    python3 tools/make_sounds.py [out_dir]      (default: assets/sounds)

Five sounds, about 125 KB together:
    move    a tiny high tick, the quietest (it plays on every D-pad step)
    select  two notes up (E6, A6)
    back    the same two notes down
    error   a low, soft double bump (never harsh)
    launch  a rising arpeggio that settles (C6 E6 G6 C7)

The output is deterministic (no randomness), so running it again gives the
same files byte for byte.
"""
import math
import os
import struct
import sys
import wave

RATE = 48000
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, 'assets', 'sounds')
NAMES = ('move', 'select', 'back', 'error', 'launch')


def tone(freq, ms, gain=0.5, decay=30.0, attack_ms=2.0, harm=0.18, glide=0.0):
    """A soft bell-like note: a sine plus a little 2nd harmonic, a quick
    attack and an exponential decay. `glide` bends the pitch by that ratio
    over the note."""
    n = int(RATE * ms / 1000)
    out = []
    ph = 0.0
    for i in range(n):
        t = i / RATE
        f = freq * (1 + glide * i / n)
        ph += 2 * math.pi * f / RATE
        env = min(1.0, t * 1000 / attack_ms) * math.exp(-decay * t)
        out.append(gain * env * (math.sin(ph) + harm * math.sin(2 * ph)))
    return out


def mix(*parts):
    """(offset_ms, samples) pairs summed into one buffer."""
    n = max(int(RATE * off / 1000) + len(s) for off, s in parts)
    buf = [0.0] * n
    for off, s in parts:
        o = int(RATE * off / 1000)
        for i, v in enumerate(s):
            buf[o + i] += v
    return buf


def write(path, buf, peak=0.5):
    """Normalise to `peak` of full scale, fade the last 4 ms (no click)."""
    top = max(abs(v) for v in buf) or 1.0
    fade = int(RATE * 0.004)
    for i in range(fade):
        buf[-1 - i] *= i / fade
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b''.join(struct.pack('<h', int(32767 * peak * v / top)) for v in buf))


def sounds():
    """name -> (samples, peak)."""
    return {
        'move': (tone(1760, 40, decay=110, harm=0.05), 0.22),
        'select': (mix((0, tone(1318.5, 90, decay=45)), (45, tone(1760, 140, decay=35))), 0.4),
        'back': (mix((0, tone(1760, 90, decay=45)), (45, tone(1318.5, 140, decay=35))), 0.35),
        'error': (mix((0, tone(220, 110, decay=28, harm=0.35)),
                      (120, tone(207.65, 160, decay=24, harm=0.35))), 0.45),
        'launch': (mix((0, tone(1046.5, 420, decay=9, glide=0.01)),
                       (70, tone(1318.5, 380, decay=10, glide=0.01)),
                       (140, tone(1568.0, 360, decay=11, glide=0.01)),
                       (210, tone(2093.0, 420, decay=10, glide=0.01, gain=0.35))), 0.45),
    }


def main(argv):
    out = argv[1] if len(argv) > 1 else DEFAULT_OUT
    os.makedirs(out, exist_ok=True)
    total = 0
    for name, (buf, peak) in sounds().items():
        path = os.path.join(out, name + '.wav')
        write(path, buf, peak)
        total += os.path.getsize(path)
    print(f"wrote {', '.join(NAMES)} to {out} ({total // 1024} KB)")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
