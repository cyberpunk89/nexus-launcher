# Downloads and repack installs, start to Play

Status: exploration, nothing built. Mockup:
[downloads-pipeline-mockup.html](downloads-pipeline-mockup.html) (live simulation; Pause works).
Read from the code on 2026-11-06.

## The problem

A repack goes through four things, and Nexus shows them in three unrelated places:

| Stage | Where it shows today | How good the progress is |
|-------|----------------------|--------------------------|
| Torrent download | Downloads page (`nexus/gui/downloads.py`), a thin 4 px bar per torrent | Good: percent, speed and ETA come from Transmission or qBittorrent (`nexus/torrent.py`). |
| Verify (MD5) | Install job, step text | Per-file markers (`[12/40] data.bin: OK`) become a bar in `nexus/gui/jobs.py`. |
| Install (the Inno `setup.exe`) | Install job | Weak. The CLI reports only `written` bytes of the game folder every 3 s (`InstallProgress` in `nexus/cli/repack.py`). With no total, the GUI shows "12.4 GB written" and a speed, never a percentage or ETA. |
| Finish (find exe, register, art) | Install job, step `[3/4]` | Step text only. |

Consequences:

- When a download ends, its row changes owner. A torrent row stops, an install job row starts,
  and the percentage resets to 0.
- The longest and least predictable part, unpacking, has no percentage and no ETA.
- Theatre's Downloads tab builds its rows from the same two sources (`theatre` rows in
  `downloads.py`), so it inherits both gaps.
- The preflight disk check only requires room for the repack size
  (`preflight_hard_disk_check` in `nexus/cli/repack.py`). The installed game is usually
  2 to 3 times bigger.

## The idea: one card per game, one track from magnet to Play

Each game gets one card with a four-stage track: **Download, Verify, Install, Finish**. The
card keeps its place while the game moves along the track, so the percentage never resets.

What the mockup shows:

- **Overall percent and ETA** across all four stages, not just the current one.
- **Stage widths by typical duration**, not equal quarters, so the bar moves at a believable
  pace. The weights can be learned from the user's own past installs (see below).
- **Honest labels:** download and verify are tagged *exact*; install is tagged *estimate*
  and capped at 99% until the installer actually exits.
- **Speed sparkline** for the stages that have a speed (download, install).
- **Queue state:** "Waits for a free download slot" (the existing queue already runs one
  install at a time).
- **Disk forecast:** a bar of what's in use, what these games still have to write, and what
  will be free afterwards. Turns "not enough room" from a surprise at 90% into a warning at the
  start.
- **Ready to play:** the card stays in the list with Play and Show.

## Where real install progress comes from

Install progress is a ratio, written bytes over expected bytes. Nexus already has the
numerator; the denominator is what's missing. Candidates, best first:

1. **Original Size from the repack page.** `nexus/repackindex.py` already scrapes
   "Original Size" and "Repack Size", and `nexus/cli/downloads.py` returns both as text. The
   repack size is already parsed to bytes (`_size_bytes`); doing the same for the original size
   and saving it with the tracked torrent in `torrents.json` gives a denominator at no extra
   cost. It is the *uncompressed original*, so the installed size is close to it but not
   identical. That is why the mockup tags it "estimate".
2. **The installer's own header.** `nexus/inno.py` already reads the Inno setup header. The
   required-disk-space figure Inno shows in its wizard is derived from data in the setup file,
   so it may be readable without running the installer. Needs checking against a real FitGirl
   `setup.exe`.
3. **Your own history.** After each install, store `installed_bytes / repack_bytes` for that
   repack. Future ones from the same repacker use the median ratio when the page figure is
   missing.
4. **Fallback:** no denominator, so keep today's "N GB written, X MB/s" and an indeterminate
   shimmer. Never show a made-up percentage.

Quality rules so the estimate doesn't lie:

- Cap the install stage at 99% until the process exits, so an under-estimate can't show 100%
  and sit there.
- If written bytes pass the estimate, widen it (for example to written plus 10%) and show
  "almost done" instead of going backwards.
- Smooth the ETA with a rolling average of the last 30 to 60 seconds. Unpacking speed jumps
  when it moves from big archives to many small files, and raw speed makes the ETA flicker.

## Stage weights

Start with fixed fractions (the mockup uses roughly Download 52%, Verify 10%, Install 33%,
Finish 5%). Then record each stage's duration after every finished repack in
`DATA_DIR` (a small JSON file, like `torrents.json`) and use the median share from the last
10 installs. People on a fast line and a slow disk get a different bar from people on a slow
line and an NVMe, and both are right.

Verify is optional today (`--verify`). If skipped, its segment is dropped from the track.

## Data model

One record per game, merged from three existing sources in the GUI (the merge already exists
in spirit in `downloads.py`, which builds rows from torrents and install jobs):

```
{ slug, name, art, source,
  stage: queued | download | verify | install | finish | ready | failed,
  stage_fraction, overall_fraction, eta_seconds, speed_bytes_per_s,
  expected_install_bytes, written_bytes, exact: bool,
  history: [recent speeds] }
```

The join key is the torrent hash that `torrents.json` already stores with the install
intent, then the game slug once an install job starts. A pure function (new
`nexus/pipeline.py`, no Qt, like `nexus/smart.py`) maps the raw pieces to this record. Both
the desktop page and Theatre consume it, and it is testable with plain dicts.

## UI plan

- **Desktop Downloads page:** replace `DownloadRow` with a `PipelineCard`: art tile, title,
  phase line, four-segment track, percent, ETA, sparkline, Pause/Resume. Keep the existing
  "⋮" menu (open folder, remove, delete files) for torrents.
- **Disk forecast strip** at the top, based on the install drive.
- **Home Wire** (see the Home redesign): one chip, "2 downloads, 63%", fed by the same record.
- **Theatre:** big card per game, controller focus on Pause/Play, no sparkline.
- **Update in place:** change numbers in the existing widgets instead of rebuilding rows,
  so the bar animates and the controller focus doesn't jump (the mockup patches in place for
  this reason).
- **Failure:** a failed stage turns its segment red and the card offers Retry from that stage.

## Rollout

1. `nexus/pipeline.py` and tests: merge torrents, jobs and installs into one record. No UI.
2. Save `original_size` in `torrents.json`; use it as the install denominator and show
   percent and ETA on the existing install row. This alone fixes the biggest gap.
3. `PipelineCard` on the Downloads page, behind the existing page code.
4. Disk forecast and the stricter preflight (original size, not repack size).
5. Learned stage weights and ratios.
6. Theatre card and the Home Wire chip.

## Risks and open questions

- The page's "Original Size" can be wrong or missing, and repacks with optional components
  install less. Mitigation: the "estimate" tag, the 99% cap, the history-based fallback.
- Some repackers aren't FitGirl and the Inno trick won't apply. The card still works with
  downloads only, or with the indeterminate install fallback.
- On Linux the installer runs under Wine/Proton, and the folder-size measure still works
  there, but the target is the prefix's folder. Check it reports correctly through
  `InstallProgress`.
- The installer runs elevated on Windows and can't be paused or cancelled from Nexus. The
  Pause button must only appear for stages that can pause (download).
- Should a finished game disappear from Downloads after a day, or stay until dismissed? The
  current code hides finished rows after `READY_HOURS`.
- Do you want a notification or sound when a stage completes, or only at Ready?
