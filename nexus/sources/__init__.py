"""Extra stores the library reads besides Steam and Heroic (steamlib keeps
those). Each source is a module with a `Source`: it says whether its store is
on this machine, lists the installed games as the same dicts steamlib.scan
emits, and reports its own health for the Health page. A source that fails
is logged and reported, never allowed to break the library."""

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class Source(Protocol):
    name: str

    def available(self) -> bool: ...

    def installed(self) -> list[dict]: ...

    def health(self) -> dict: ...


def _all() -> list[Source]:
    from . import epic
    return [epic.Epic()]


def sources(candidates: list[Source] | None = None) -> list[Source]:
    """The sources whose store is on this machine."""
    out = []
    for s in _all() if candidates is None else candidates:
        try:
            if s.available():
                out.append(s)
        except Exception:
            log.exception("source %s: availability check failed", s.name)
    return out


def installed_games(candidates: list[Source] | None = None) -> list[dict]:
    """Every available source's installed games, in source order."""
    games = []
    for s in sources(candidates):
        try:
            games += s.installed()
        except Exception:
            log.exception("source %s: reading installed games failed", s.name)
    return games
