"""Bug reports (phase 1a, no server): a crash leaves a file on disk
(crash.py), a report is collected from this machine (sysinfo.py), every
string in it goes through the scrubber (scrub.py), and render.py turns a
draft plus the user's choices into the exact issue text and a prefilled
GitHub issue URL. Qt-free and stdlib only: the CLI does the work
(nexus-cli --report-*, nexus/cli/report.py) and the window only shows it."""
