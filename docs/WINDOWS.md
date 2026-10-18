# Using Windows?

Nexus only runs on **Linux**, and that's on purpose. This page explains why and what to use
instead.

> Nexus is an unofficial hobby project, not affiliated with FitGirl, Valve, Heroic or any
> store. Only install software you are legally allowed to use.

## Why Nexus is Linux-only

Most of what Nexus does is run *Windows* game installers and games on *Linux*, through
Proton, Wine and umu. It sets up a Wine prefix for each game, picks a Proton build and
writes the launchers. It also adds the game to Steam and Heroic on Linux, and tunes things
like gamescope and MangoHud.

On Windows none of that is needed, because the games already run natively. That's why
starting `nexus` or `nexus-cli` on Windows (or macOS) just prints a short message pointing
here.

## What to use on Windows instead

- **Installing a repack:** run the repack's `setup.exe` directly. First, run the repack's
  own *Verify BIN files* script (in its `MD5` folder) to check the download is complete and
  undamaged.
- **A launcher like Nexus:** [Playnite](https://playnite.link) is free and open source. It
  puts Steam, Epic, GOG, other stores, emulators and installed repacks in one library,
  fetches cover art, and has a **Fullscreen mode** with controller support for the TV,
  similar to Nexus's Theatre mode.
- **Epic, GOG and Amazon games:** [Heroic Games Launcher](https://heroicgameslauncher.com)
  also runs on Windows.

## Trying Nexus anyway

If you'd like to try it, you need Linux on the machine itself:

- **Steam Deck:** switch to Desktop Mode and follow the README's
  [Quick start](../README.md#quick-start) (the *Steam Deck* venv step).
- **A gaming-focused Linux distro**, such as [Bazzite](https://bazzite.gg),
  [CachyOS](https://cachyos.org) or [Nobara](https://nobaraproject.org). You can try one
  from a live USB stick, or install it on a spare drive or alongside Windows (dual boot).
  Then follow the README's Quick start.

**WSL** (Windows Subsystem for Linux) isn't a good fit: Nexus would be running Windows
games through Proton inside Linux inside Windows. They'd run slower, not faster.
