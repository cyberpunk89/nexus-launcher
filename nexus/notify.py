"""Pushes to your phone when something long finishes: an ntfy topic
(ntfy.sh or self-hosted, optional access token) and/or every reachable,
paired KDE Connect device. Configured in config.toml:

    [notify]
    ntfy = "https://ntfy.sh/<your-topic>"   # public topics are readable by anyone
    ntfy_token = "tk_..."                    # optional
    kdeconnect = true
    min_minutes = 3                          # installs shorter than this don't push

Everything is best-effort: a push that fails is reported, never fatal.
KDE Connect counts on Windows only when kdeconnect-cli is installed there."""

import shutil
import subprocess
import tomllib

from . import net
from .features import has
from .paths import CONFIG_PATH


def settings() -> dict:
    try:
        data = tomllib.loads(CONFIG_PATH.read_text(encoding='utf-8-sig')).get('notify') or {}
    except (OSError, ValueError):
        data = {}
    url = str(data.get('ntfy') or '').strip()
    try:
        minutes = float(data.get('min_minutes', 3))
    except (TypeError, ValueError):
        minutes = 3.0
    return {'ntfy': url if url.startswith('https://') else None,
            'ntfy_token': str(data.get('ntfy_token') or '') or None,
            'kdeconnect': data.get('kdeconnect') is True
            and (has('kdeconnect') or shutil.which('kdeconnect-cli') is not None),
            'min_minutes': minutes}


def configured(s: dict | None = None) -> bool:
    s = s or settings()
    return bool(s['ntfy'] or s['kdeconnect'])


def _kdeconnect_devices() -> list[str]:
    try:
        out = subprocess.run(['kdeconnect-cli', '-a', '--id-only'], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def push(title: str, body: str, ok: bool = True, s: dict | None = None) -> list[str]:
    """Send to every configured target; returns the errors (empty = sent)."""
    s = s or settings()
    errs = []
    if s['ntfy']:
        headers = {'Title': title.encode('ascii', 'replace').decode(),
                   'Tags': 'white_check_mark' if ok else 'warning',
                   'Priority': 'default' if ok else 'high'}
        if s['ntfy_token']:
            headers['Authorization'] = f"Bearer {s['ntfy_token']}"
        try:
            net.post(s['ntfy'], body.encode(), headers)
        except net.NetError as e:
            errs.append(f"ntfy: {e}")
    if s['kdeconnect']:
        if not shutil.which('kdeconnect-cli'):
            errs.append("kdeconnect-cli isn't installed")
        else:
            devices = _kdeconnect_devices()
            if not devices:
                errs.append("KDE Connect: no paired device is reachable")
            for dev in devices:
                try:
                    subprocess.run(['kdeconnect-cli', '-d', dev, '--ping-msg',
                                    f"{title}: {body}"], capture_output=True, timeout=10)
                except (OSError, subprocess.TimeoutExpired) as e:
                    errs.append(f"KDE Connect {dev}: {e}")
    return errs
