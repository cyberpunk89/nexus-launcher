"""What moves Home between your own clicks (w-138): the day changing, the
hour, a play session ending, the library changing and the insights answer.
Widgets list the events they want in their spec's `feeds`; HomeFeed only
pokes the hub (dash_update, coalesced) and Dashboard.refresh routes through
FED_BY. The timers run only while Home shows (set_live) and stop on any
other page, in Theatre, and while minimised. A day change that happened
while they were off (suspend, another page) fires when Home shows again."""

from datetime import date, datetime, time as dtime, timedelta

from PyQt6.QtCore import QObject, QTimer

from .jobs import cli_cache_forget, cli_json

INSIGHTS_ARGS = ['--insights', '--json']
INSIGHTS_TTL = 300                      # seconds a payload is reused
HOUR_MS = 3600 * 1000
FORCE_EVENTS = {'midnight', 'sessions', 'library'}   # these make the next ask a fresh one


def today() -> date:
    return date.today()


def now() -> datetime:
    return datetime.now()


class HomeFeed(QObject):
    """Child of the Dashboard. `.data` is the last insights payload."""

    def __init__(self, dash):
        super().__init__(dash)
        self.dash = dash
        self.data: dict | None = None
        self.live = False
        self._day = today()
        self._filling = False           # an ask is inside Dashboard.refresh
        self._force = False
        self._hourly = QTimer(self)
        self._hourly.setInterval(HOUR_MS)
        self._hourly.timeout.connect(self._on_hour)
        self._midnight = QTimer(self)
        self._midnight.setSingleShot(True)
        self._midnight.timeout.connect(self._on_midnight)

    # ---- the clock ----
    def set_live(self, on: bool):
        """Home is showing (or not). On: start the timers and look at the
        day, which covers a suspend or a stay on another page."""
        self.live = bool(on)
        if not on:
            self._hourly.stop()
            self._midnight.stop()
            return
        if not self._hourly.isActive():
            self._hourly.start()
        self._arm_midnight()
        self._check_day()

    def _arm_midnight(self):
        t = now()
        nxt = datetime.combine(t.date() + timedelta(days=1), dtime.min)
        ms = int((nxt - t).total_seconds() * 1000) + 1000
        self._midnight.start(max(ms, 1000))

    def _check_day(self):
        d = today()
        if d != self._day:
            self._day = d
            self.poke('midnight')

    def _on_midnight(self):
        self._check_day()
        if self.live:
            self._arm_midnight()

    def _on_hour(self):
        self._check_day()
        self.poke('hour')

    # ---- events ----
    def poke(self, *events):
        """Something happened: refresh whoever listens (coalesced by the hub)."""
        if set(events) & FORCE_EVENTS:
            self._force = True
        update = getattr(self.dash.hub, 'dash_update', None)
        if update is not None:
            update(*events)

    # ---- insights ----
    def ask(self, kinds):
        """Called by Dashboard.refresh before it fills: ask for the insights
        payload when a shown widget reads it and is among `kinds`. Ask
        first: with a fresh memo the callback runs before cli_json returns,
        so the answer is stored for the fill that follows and nothing is
        queued (re-entering fill would loop)."""
        consumers = self.dash.insights_consumers()
        if not consumers or self._filling:
            return
        if kinds is not None and not set(kinds) & (consumers | {'insights'}):
            return
        force, self._force = self._force, False
        if force:
            cli_cache_forget(INSIGHTS_ARGS)
        self._filling = True
        try:
            cli_json(INSIGHTS_ARGS, self._arrived, 30000, ttl=INSIGHTS_TTL)
        finally:
            self._filling = False

    def _arrived(self, data, err=''):
        if data is None or data == self.data:
            return                      # keep what we had
        self.data = data
        if not self._filling:
            self.dash.hub.dash_update('insights')
