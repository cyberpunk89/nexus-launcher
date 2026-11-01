"""The newer Nexus release, from the moment a check finds it until it is
installed (w-72). The start-up toast can go; Health's row and Settings'
About row show this one state and act through Updater.act().

On the installed Windows edition (updates.in_app) Update downloads the
Setup exe in a CLI job (`--update-fetch`, checked against the release's
SHA256SUMS, shown in Activity too), then "Restart to update" runs it with
no window (updates.install_argv) and quits; the installer starts Nexus
again (nexus.iss /RELAUNCH). A portable zip copy, a source checkout and
Linux keep the link the toast always had."""

from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QObject, QUrl
from PyQt6.QtGui import QDesktopServices

from nexus import platform, updates

from .common import CLI


class Updater(QObject):
    changed = pyqtSignal()

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.rel: dict | None = None    # updates.available(): version, url, page, installer, sums
        self.state = ''                 # '', available, downloading, ready, failed
        self.detail = ''
        self.path = ''                  # the checked Setup exe, once ready
        self.sha = ''
        self.job = None
        self.in_app = updates.in_app()

    def offer(self, rel: dict | None):
        """A check's answer. The same version keeps whatever is under way."""
        if not rel:
            return
        if self.rel and self.rel.get('version') == rel.get('version') and self.state:
            return
        self.rel, self.state, self.detail, self.path = dict(rel), 'available', '', ''
        self.changed.emit()

    def view(self) -> dict | None:
        """What the rows show: {version, title, detail, button, enabled,
        progress ((n, total) or None)}; None when there's nothing newer."""
        if not self.rel:
            return None
        v = self.rel['version']
        if not self.in_app:
            setup = self.rel['url'].endswith('.exe')
            return {'version': v, 'title': f"Nexus {v} is available",
                    'detail': "Download the installer and run it over this copy." if setup else
                              "See the release page for what's new and how to get it.",
                    'button': "Installer" if setup else "Release page", 'enabled': True,
                    'progress': None}
        job = self.job if self.state == 'downloading' else None
        detail, button = {
            'available': ("Nexus downloads it in the background, checks it and asks to "
                          "restart.", "Update"),
            'downloading': ((job.detail if job and job.detail else "Starting the download…"),
                            "Downloading…"),
            'ready': ("Downloaded and checked. Nexus closes, updates and opens again.",
                      "Restart to update"),
            'failed': (self.detail or "The download failed.", "Try again"),
        }[self.state]
        return {'version': v, 'title': f"Nexus {v} is available", 'detail': detail,
                'button': button, 'enabled': self.state != 'downloading',
                'progress': job.progress if job else None}

    def act(self):
        """The button (Health, Settings, the toast)."""
        if not self.rel:
            return
        if not self.in_app:
            QDesktopServices.openUrl(QUrl(self.rel['url']))
        elif self.state == 'ready':
            self.restart()
        elif self.state != 'downloading':
            self.download()

    def download(self):
        from .jobs import Job
        self.state, self.detail = 'downloading', ''
        self.job = Job([*CLI, '--update-fetch'], f"Downloading Nexus {self.rel['version']}",
                       None, 'tool')
        self.job.changed.connect(self.changed)
        self.job.done.connect(self._fetched)
        self.win.jobs.submit(self.job)
        self.changed.emit()

    def _fetched(self, _code):
        job = self.job
        ready = job.events.get('update-ready') or {}
        v = self.rel['version']
        if job.state == 'done' and ready.get('path'):
            self.state, self.path, self.sha = 'ready', ready['path'], ready.get('sha256', '')
            self.win.toast(f"Nexus {v} is ready to install.", 'success',
                           ("Restart to update", self.act), 12000)
        elif job.state == 'cancelled':
            self.state = 'available'
        else:
            self.state = 'failed'
            self.detail = (job.detail or '').removeprefix('[!] ') or "The download failed."
            self.win.toast(f"Couldn't download Nexus {v}: {self.detail}", 'error',
                           ("Try again", self.act), 12000)
        self.changed.emit()

    def restart(self):
        """Check the download once more, then run it and quit; with work
        still going (jobs, games waiting for Steam) ask first."""
        reasons = self.win._quit_reasons()
        if reasons:
            self.win.confirm("Restart to update?", "\n\n".join(reasons), "Update anyway",
                             self._verify)
        else:
            self._verify()

    def _verify(self):
        from .jobs import in_thread
        path, want = self.path, self.sha
        in_thread(lambda: Path(path).is_file() and (not want or updates.sha256(path) == want),
                  self._install)

    def _install(self, ok, err):
        if not ok:
            self.state, self.path = 'failed', ''
            self.detail = "The downloaded installer changed or went missing. Download it again."
            self.changed.emit()
            self.win.toast(self.detail, 'error', ("Download again", self.act), 12000)
            return
        try:
            platform.spawn_detached(updates.install_argv(self.path))
        except OSError as e:
            self.state, self.detail = 'failed', f"Couldn't start the installer ({e})."
            self.changed.emit()
            self.win.toast(self.detail, 'error')
            return
        self.win.quit_after_jobs()
