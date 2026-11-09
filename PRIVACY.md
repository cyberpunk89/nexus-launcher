# Privacy

Nexus sends nothing about you on its own. Two things can leave your computer, and only
when you say so:

- **The install count**, if you answer "Count me" (in setup, on Home or in
  Settings → System → Privacy). It is off until you answer.
- **A bug report**, each time you press "Send anonymously" on Report a problem. You see
  the whole report before it goes.

To do its work Nexus also downloads things: update checks and Proton releases from GitHub,
and artwork, game facts and news from the sites you set up or turn on in Settings. Those
are ordinary downloads with no id; like any download, the site sees your IP address.

## The install count

At most once a day, Nexus sends one small message to the Nexus relay
(`https://nexus-reports-relay.vercel.app/v1/count`, source at
[cyberpunk89/nexus-reports](https://github.com/cyberpunk89/nexus-reports)). It has no id of
any kind. It only says that one install was used today, this week or this month, and these
facts about it:

| Field | What it holds | Example |
|---|---|---|
| `schema` | The message format | `1` |
| `client` | The Nexus version (one newer than the latest release counts as `dev`) | `0.5.5` |
| `periods` | Whether this is the install's first message of the day (`d`), the week (`w`), the month (`m`) | `["d", "w"]` |
| `edition` | `linux` or `windows` | `linux` |
| `install` | How Nexus was installed: `git`, `source`, `win-installer`, `win-portable` | `git` |
| `os` | The distribution, from a fixed list (`windows` on Windows) | `fedora` |
| `os_ver` | Its major version; none for rolling releases | `43` |
| `desktop` | The desktop, from a fixed list (`windows` on Windows) | `kde` |
| `session` | `wayland`, `x11` or `tty`; none on Windows | `wayland` |
| `lang` | The first two letters of your language setting | `de` |
| `py` | The Python version | `3.14` |
| `age` | How long ago this install was first counted: `<2w`, `2-5w`, `5-25w`, `25w+` | `<2w` |

Anything not on a list is sent as `other`. The date of the first count, from which `age`
is worked out, stays on your computer.

**Never sent:** an install id or machine id, your user or computer name, your IP address
as data, your games, play time, folders, settings, keys, accounts, hardware, time zone or
errors.

**What the relay keeps.** For each day it adds one to a counter for each field and value,
for example "Monday, `desktop` = `kde`: 41". A message is never stored as a row, so the
fields can't be joined back into one install; the only pair counted together is edition
with version. After 13 months, day counts are summed into monthly totals. The totals are
not published.

**Your IP address.** The relay runs on Vercel, so Vercel's network sees the address the
message comes from, as with any website. The relay doesn't store it. To stop floods it
keeps a daily counter keyed by a hash of the address with a random salt that is deleted
every day, so the hash can't be traced back. Vercel keeps its own short-lived request logs.

**When it is sent.** When the Nexus window opens, then hourly while the window or the
background agent runs. A message goes out only if the day, week or month hasn't been
counted yet, so most checks send nothing. If sending fails, nothing is marked and it tries
again after three hours.

### See what is sent

- Settings → System → Privacy → **Show what is sent**: the exact message, even while the
  count is off. **Sent so far**: the last 100 messages and the relay's answer.
- From a terminal: `nexus-cli --stats-preview` and `nexus-cli --stats-sent`.
- On disk: `logs/stats-sent.jsonl` and `stats.json` in Nexus's data folder
  (`~/.local/share/nexus` on Linux, `%LOCALAPPDATA%\nexus-launcher` on Windows).

### Turn it off

Any of these stops it:

- Settings → System → Privacy → **Count this install** off, or `count = false` under
  `[stats]` in `config.toml`.
- The environment variable `DO_NOT_TRACK=1` or `NEXUS_NO_STATS=1`.
- `relay = false` under `[report]` in `config.toml` (this also turns off sending bug reports
  anonymously).

## Bug reports

Report a problem shows the full report before anything is sent. Your home folder, user
name, computer name, folders, game names, emails, addresses and keys are taken out, and you
can leave out whole parts or single lines. "Open on GitHub" opens a prefilled issue in your
browser; "Send anonymously" sends the report through the same relay, which files it in a
private inbox. The relay keeps the report for 90 days and stores no IP address. The details
are in [docs/LINUX.md](docs/LINUX.md#command-line-reference) and the relay's
[README](https://github.com/cyberpunk89/nexus-reports#readme).
