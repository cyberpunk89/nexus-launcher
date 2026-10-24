"""Repacks with a hypervisor (HV) Denuvo bypass.

Some FitGirl repacks crack Denuvo with a hypervisor: a kernel driver that
needs VT-x/AMD-V, Windows' virtualization-based security turned off
(the repack's VBS.cmd) and a reboot with driver signature checks off
(F7). Under Wine or Proton there is no Windows kernel to load it into, so
those games don't run on Linux at all.

FitGirl files these posts under the "Hypervisor Bypass" category; some
ship both builds ("v1.7.0 HV / v1.2.0 Non-HV"). A downloaded HV repack's
folder is named "... [FitGirl HV Repack]" and carries VBS.cmd."""

import re
from pathlib import Path

from .features import has

CATEGORY = 319                   # "Hypervisor Bypass" on fitgirl-repacks.site
CATEGORY_NAME = 'Hypervisor Bypass'
GUIDE = 'https://fitgirl-repacks.site/hypervisor-guide/'
_BOTH_RE = re.compile(r'non[\s_-]?hv|denuvoless', re.I)
_FOLDER_RE = re.compile(r'\bHV\s+Repack\b', re.I)


def kind(title: str) -> str:
    """'both' when the post also offers a non-HV build, else 'hv'."""
    return 'both' if _BOTH_RE.search(title or '') else 'hv'


def badge(k: str | None) -> str | None:
    return {'hv': "HV", 'both': "HV + non-HV"}.get(k or '')


def in_folder(repack_dir: Path) -> bool:
    """Whether a downloaded repack is an HV one: its folder name, or the
    VBS.cmd that only HV repacks carry."""
    d = Path(repack_dir)
    if _FOLDER_RE.search(d.name):
        return True
    try:
        return any(p.name.lower() == 'vbs.cmd' for p in d.iterdir())
    except OSError:
        return False


def note(k: str | None = 'hv') -> str:
    """One line on what an HV repack means on this system."""
    if has('proton'):
        s = ("Hypervisor bypass: it won't run under Proton or Wine (it needs a "
             "Windows kernel driver).")
        return s + (" Pick the non-HV build if you download it." if k == 'both' else "")
    s = ("Hypervisor bypass: it needs VT-x/AMD-V, the repack's VBS.cmd and a reboot "
         "with driver signature checks off; Defender may flag its files.")
    return s + (" A non-HV build is included too." if k == 'both' else "")
