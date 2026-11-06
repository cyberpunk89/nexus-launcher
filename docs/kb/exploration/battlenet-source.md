# Battle.net games in Nexus

Status: exploration, nothing built. Read from the code on 2026-11-06. The Battle.net details
under "What Battle.net gives us" come from general knowledge of the launcher, not from a
machine in this session, so they need checking on a real install before any code is written.

## Short answer

Nexus already has a plug-in point for extra stores, and Epic on Windows was built through it.
A Battle.net source can follow the same pattern: read the installed games from the launcher's
own files, launch them through a `battlenet://` link, and show them in the library like any
other game. It would list **installed games only**, because Battle.net's owned list needs a
login. Windows first. Linux needs a different route.

## What Nexus already has

- `nexus/sources/__init__.py` defines a `Source` protocol: `available()`, `installed()` and
  `health()`. A source that fails is logged and reported on the Health page, and never breaks
  the library.
- `nexus/sources/epic.py` (about 100 lines) is the template. It reads Epic's manifest files
  under `%ProgramData%`, builds the same game dicts as `steamlib.scan`, sets
  `source: 'epic'`, and gives each game a `launcher` URL.
- Windows launches any such game with `--open-url <launcher>` (`nexus/gui/win_actions.py`).
- The same `launcher` key already feeds Sunshine, the doctor, and uninstall.
- `tests/test_sources.py` tests Epic against synthetic fixture files, with no network and no
  real home folder.
- Library filter chips are a short list in `nexus/gui/win_library.py` (`STORE_CHIPS`).
- Linux gets Epic, GOG and Amazon through Heroic (`nexus/cli/heroic.py`). Heroic does not
  support Battle.net.

## What Battle.net gives us

To verify on a real machine:

| Need | Likely source |
|------|---------------|
| Which games are installed (Windows) | `%ProgramData%\Battle.net\Agent\product.db`, a binary protobuf file with one record per product: product code and install path. |
| Install folder and defaults | `%AppData%\Battle.net\Battle.net.config`, plain JSON. |
| Launch | `battlenet://<ProductCode>` (for example `WoW`), or `Battle.net.exe --exec="launch <Code>"`. |
| Name and art | Product codes are short and not self-explanatory (`WoW`, `Pro` for Overwatch, `Fen` for Diablo IV, `WTCG` for Hearthstone). Needs a small built-in code-to-name table, and art from SteamGridDB, which Nexus already uses for stores with no art of their own. |
| Owned but not installed | Needs a Blizzard login. Not available. Same limit as Epic today. |

`product.db` is the risky piece. It is undocumented and can change with any launcher update.
Epic's source already handles this kind of risk: an unreadable file is skipped, logged, and
reported on Health with a plain message. Battle.net should do the same, and degrade to
"couldn't read Battle.net's install list" instead of an empty library.

A minimal reader is enough. It only needs to pull the product code and install path from each
record, so it can avoid a full protobuf dependency. Nexus's CLI is stdlib only.

## Proposed design

**Windows (MVP)**

1. `nexus/sources/battlenet.py` with a `Battlenet` class, same shape as `Epic`.
2. Game dict: `slug: 'battlenet-<Code>'`, `source: 'battlenet'`, `display_name` from a code
   table (fall back to the install folder name), `install_dir`, `exe` if it can be found,
   `launcher: 'battlenet://<Code>'`, `installed: True`, `disk_bytes` from the folder.
3. Register it in `_all()` in `nexus/sources/__init__.py`.
4. Add a `battlenet` chip in `win_library.py` and a colour in the source-colour table in
   `theme.py`.
5. Tests: fixture `product.db` and `Battle.net.config` files under `tests/fixtures/sources/
   battlenet/` (normal, empty, corrupt), mirroring the Epic tests.

**Play time and last played:** the launcher starts the game as its own child, so Nexus's
launch tracker may not see it. Start by recording sessions the way Steam games are recorded
today (the GUI watches the process while it is open), keyed on the game's exe name. Without
this, play time stays empty, as it is for Epic now.

**Linux options, none as clean as Windows:**

- **Steam non-Steam shortcut:** users who run Battle.net through Steam or Proton already show
  up as one library entry through Nexus's shortcut reader, but as "Battle.net", not per game.
- **Lutris or Bottles prefixes:** read the Wine prefix's `product.db` the same way as on
  Windows, after the user points Nexus at the prefix in Settings. More setup, per-game entries,
  launch through `wine`/`umu` into that prefix.
- **Do nothing on Linux for now**, and say so in the README's Linux vs Windows table, as
  Heroic games are marked "skipped" on Windows.

Recommendation: Windows first, Linux through a configurable prefix later.

## Risks

- `product.db` changes format. Mitigated by the Health message and by tests with a corrupt
  fixture, but a launcher update can still silently empty the list until the reader is fixed.
- Launch behaviour: `battlenet://` opens the launcher, which may show an update or login
  screen before the game starts. Nexus can't tell the difference between "launching" and
  "stuck on Battle.net's update".
- Anti-cheat and online-only games (Call of Duty and similar) may need the launcher already
  running and signed in.
- Name table maintenance: new Blizzard titles need a new entry. A fallback to the folder name
  keeps unknown ones usable.
- Scope: this adds one more store to keep working. The Epic source is the cost reference:
  about 100 lines of code plus fixtures.

## Phases

1. Confirm file locations and formats on a real Windows install with a few Blizzard games.
2. Source, tests, library chip. No play time yet.
3. Play-time tracking by process name.
4. Linux prefix support, if wanted.

## Open questions

- Which platform do you run Battle.net on, and which Blizzard games do you own? That decides
  Windows-only or Linux-first, and which product codes go in the name table.
- Is installed-only acceptable, or do you want the owned list? Owned needs a Blizzard login,
  so it would have to come from a source like Game-db.
- Should other launchers (EA app, Ubisoft Connect, GOG Galaxy) be done as the same kind of
  source? The pattern above would cover them.
