"""A side-by-side copy of Nexus: NEXUS_PROFILE=dev gives it its own data,
config and cache folders, instance socket, app-id, desktop entry and agent
unit (nexus-dev), so a dev checkout can run next to the installed release
without touching its library. The bin/ stubs also set it from their own name:
a `nexus-dev` / `nexus-cli-dev` symlink runs the `dev` profile. Unset (the
default) changes nothing. Only lowercase letters and digits are accepted."""

import os
import re

_raw = (os.environ.get('NEXUS_PROFILE') or '').strip().lower()
PROFILE = _raw if re.fullmatch(r'[a-z0-9]{1,16}', _raw) else ''
SUFFIX = f'-{PROFILE}' if PROFILE else ''
