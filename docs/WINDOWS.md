# Nexus on Windows

Nexus has a Windows edition, called **nexus-launcher** for now (the name may change). Games
on Windows already run natively, so it runs a game's own `setup.exe`, adds the game to your
library and keeps it tidy. It doesn't use Proton or Wine.

> Nexus is an unofficial hobby project, not affiliated with Valve, Epic or any store. It
> never downloads games by itself. Only install software you are allowed to use.

> **Draft.** No Windows release has been published yet; details may change once the
> first one is tested.

## Install

Download from the release page (Windows 10 or 11, 64-bit). Pick one:

- **Installer:** `nexus-launcher-Setup-<version>.exe`. It installs just for you (no admin
  prompt) into `%LOCALAPPDATA%\Programs\nexus-launcher` and adds a Start Menu entry. It can
  also start Nexus in the tray when you sign in to Windows (a tick box, off by default). To
  update, run the newer installer over the old one; close Nexus first, or let the installer
  close it.
- **Portable zip:** `nexus-launcher-<version>-portable.zip`. Unzip it anywhere (say
  `C:\Tools`) and run `nexus-launcher.exe` in the `nexus-launcher` folder. Nothing is
  installed. `nexus-cli.exe` beside it is the command line.

Either way, Nexus keeps its settings under `%APPDATA%\nexus-launcher` and its data and cache
under `%LOCALAPPDATA%\nexus-launcher`. That holds for the portable zip too, so the zip and
an installed copy share the same settings and library.

Once a day Nexus asks GitHub whether a newer release is out and, if so, shows a notice with
a link to it. Nothing is downloaded. To turn that off, put `check = false` under `[updates]`
in `%APPDATA%\nexus-launcher\config.toml`.

Uninstalling (Settings → Apps) asks whether to delete that data too. The answer defaults to
**No**, so a reinstall picks up where you left off. Installed games are never touched.

## First run and SmartScreen

The build isn't code-signed, so Windows SmartScreen may show *"Windows protected your PC"*.
Click **More info**, then **Run anyway**. You'll only see this the first time. There are no
plans for a signed build.

To check a download, compare its SHA-256 with the one in the release notes or the release's
`SHA256SUMS` file:

```
certutil -hashfile nexus-launcher-Setup-<version>.exe SHA256
```

A setup wizard then checks what this machine has and offers to pick a torrent client. By
default games are installed under `<drive>:\Games`, on the local drive with the most free
space other than the Windows one (else `C:\Games`). Never Program Files: some older games
write their saves next to the exe, which fails there. You can change the folder in Settings.

## Torrent client

Nexus can show and control your downloads through a torrent client you already run. It
supports two:

- **Transmission:** turn on remote access (*Edit → Preferences → Remote → Allow remote
  control*; port 9091 by default).
- **qBittorrent:** turn on the Web UI (*Tools → Options → Web UI → Web User Interface*; port
  8080 by default).

Nexus finds the client from its settings file, or by trying those two default local ports.
If it finds none, the wizard says so; you can set one up later in **Settings → Torrent
client**. Transmission only writes its settings file when it quits, so restart it after
changing them.

## Antivirus

Windows Defender (and other antivirus) sometimes flags files inside game installers, such
as the unpacking helpers `unarc.dll` and `ISDone.dll` or a game's modified DLLs, as
threats. Many of these are false alarms, but whether to trust a download is **your call**.

What Nexus does:

- It never changes Defender's settings or exclusions, and never asks you to turn it off.
- If an install fails, it reads the setup log and Defender's recent detections (read-only)
  and tells you when Defender removed a file, which one, and why that probably broke the
  install.

What you can do: restore the file from **Windows Security → Protection history**, or allow
it or exclude your games folder there, then install again. If you'd rather not, don't.

## The admin prompt

Most game installers (`setup.exe`) ask for administrator rights, so Windows shows a UAC
prompt each time Nexus starts an install. Nexus itself doesn't run as administrator. If you click **No**, the
install is cancelled and the empty folder is cleaned up.

For a silent install the prompt names Nexus's command-line tool (`nexus-cli`, or Python when
run from source): it runs the installer as administrator and keeps it quiet. The installer's
music is muted, console windows it opens for its own steps are hidden, and web links open
nothing until it finishes, so it can't open a website at the end. Your default browser and
terminal settings are put back as soon as the installer exits.

## Which stores are read

Steam and Epic only. Steam games come from your Steam library folders. Epic games come from
the Epic Games Launcher's install list, so only games that are **installed** show up. Other
stores aren't read yet. Games installed by Nexus appear with their own Start Menu shortcut.

## Saves backup

Games on Windows keep saves in your real `AppData` and Documents folders. To know which ones
belong to a game, Nexus uses the community
[Ludusavi manifest](https://github.com/mtkennerly/ludusavi-manifest) (built from
PCGamingWiki), downloaded once and cached. Backing up and restoring saves around an uninstall
uses it.

Backups go to `%LOCALAPPDATA%\nexus-launcher\saves`. A game the manifest doesn't list
gets no backup: Nexus says it doesn't know where that game keeps its saves rather than
guessing.

## Not available on Windows

These are Linux-only, or not ported yet, and are hidden or skipped:

- **Proton, umu and Wine:** not needed. This includes the Proton page, ProtonDB, per-game
  runtime choice, launch profiles and the Wine prefix tools.
- **Performance presets:** lsfg-vk, gamescope and MangoHud.
- **protongen.**
- **Heroic:** its games and shortcuts are skipped.
- **Sunshine:** not ported yet (planned).
- **Linux system extras:** system tuning tips, pausing a running game, KDE Connect, the
  systemd background service and `.desktop` launchers (Windows uses Start Menu shortcuts).

There is no background service on Windows. Instead, **Settings → Start with Windows** (or
the installer's tick box) puts a `Nexus` shortcut in your Startup folder that starts Nexus
in the tray when you sign in, and the tray does that work while the window is closed.
Turn it off in Settings or in Task Manager → Startup apps.
