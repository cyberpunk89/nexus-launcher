"""Microsoft Store (UWP) games. Some repacks are a packaged app: an
AppxManifest.xml beside the exe, registered with Windows by the installer.
Such a game only starts as its package (the Store runtime DLLs are loaded for
it then); run as a plain exe it dies at once with "DLL not found". It is
started through its AppUserModelID: shell:AppsFolder\\<family name>!<app id>."""

import hashlib
import re
from pathlib import Path

_CROCKFORD = '0123456789abcdefghjkmnpqrstvwxyz'
_IDENTITY = re.compile(r'<Identity\b[^>]*>', re.S)
_APPLICATION = re.compile(r'<Application\b[^>]*>', re.S)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', tag)
    return m.group(1) if m else None


def publisher_id(publisher: str) -> str:
    """The 13-character publisher hash in a package family name: the first
    8 bytes of SHA-256 over the UTF-16LE publisher, in 5-bit groups."""
    bits = int.from_bytes(hashlib.sha256(publisher.encode('utf-16-le')).digest()[:8], 'big') << 1
    return ''.join(_CROCKFORD[(bits >> (5 * (12 - i))) & 31] for i in range(13))


def app_id(exe: Path) -> str | None:
    """'<family name>!<app id>' for an exe that is a packaged app's entry
    point (the manifest beside it, or in a folder above, names it), else
    None."""
    exe = Path(exe)
    for root in [exe.parent, *exe.parents][:4]:
        manifest = root / 'AppxManifest.xml'
        try:
            text = manifest.read_text(encoding='utf-8-sig', errors='replace')
        except OSError:
            continue
        ident = _IDENTITY.search(text)
        name, pub = (_attr(ident.group(0), 'Name'), _attr(ident.group(0), 'Publisher')) \
            if ident else (None, None)
        if not (name and pub):
            return None
        rel = exe.relative_to(root).as_posix().lower()
        for m in _APPLICATION.finditer(text):
            target = (_attr(m.group(0), 'Executable') or '').replace('\\', '/').lower()
            if target == rel and _attr(m.group(0), 'Id'):
                return f"{name}_{publisher_id(pub)}!{_attr(m.group(0), 'Id')}"
        return None
    return None


def family(aumid: str) -> str:
    return aumid.split('!', 1)[0]


def shell_uri(aumid: str) -> str:
    return f'shell:AppsFolder\\{aumid}'
