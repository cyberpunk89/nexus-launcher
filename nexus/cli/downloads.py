"""Repack feed and index, torrents, repack checks."""

import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from .common import drive_of, existing_ancestor, STEAM_ROOT


def cmd_repack_feed(refresh: bool, as_json: bool) -> None:
    from nexus import repackfeed
    data = repackfeed.refresh(force=refresh)
    found = repackfeed.updates(load_registry()['games'].values(), data)
    if as_json:
        print(json.dumps({'fetched_at': data.get('fetched_at'), 'updates': found}))
        return
    if not found:
        print(f"No newer repacks posted (feed checked {data.get('fetched_at') or 'never'}).")
    for slug, post in sorted(found.items()):
        what = "update" if post['kind'] == 'update' else "new repack"
        print(f"  {slug}: {what} posted {post['date'][:10]} — {post['title']}\n    {post['url']}")


def _cached_wishlist() -> list | None:
    """The Game-db wishlist from its cache (no network), or None when
    Game-db isn't set up."""
    from nexus import gamedb
    if not gamedb.configured():
        return None
    try:
        res = gamedb.summary(cache_only=True)
    except gamedb.GameDbError:
        return None
    return ((res or {}).get('data') or {}).get('wishlist') or []


def cmd_repack_index(refresh: bool, as_json: bool, desktop: bool = False) -> None:
    """Sync FitGirl's catalogue, then say which wishlist games have a
    repack — pushing the new ones to the phone (nexus/alerts.py)."""
    from nexus import alerts as A, net, repackindex as RI

    def progress(page, pages):
        print(f"[{page}/{pages or '?'}] Reading FitGirl's catalogue", flush=True)
    error = None
    try:
        data = RI.sync(force=refresh, progress=progress if not as_json else None)
    except net.NetError as e:
        data, error = RI.load(), str(e)
    wishlist = _cached_wishlist()
    matches = RI.wishlist_matches(wishlist, data) if wishlist else {}
    fresh, first = RI.new_matches(matches, data) if wishlist is not None else ([], False)
    if wishlist is not None and data['posts']:
        RI.save(data)
    titles = {str(g.get('appid')): g.get('title') for g in wishlist or []}
    news = [{'appid': a, 'title': titles.get(a), **matches[a]} for a in fresh]
    if news and not error:
        send_alerts(A.fresh('fitgirl', A.fitgirl_alerts(news), prime=False), desktop)
    if as_json:
        print(json.dumps({'count': len(data['posts']), 'synced_at': data.get('synced_at'),
                          'built_at': data.get('built_at'), 'error': error,
                          'wishlist': wishlist is not None, 'matches': len(matches),
                          'new_matches': news, 'first': first}))
        return
    if error:
        print(f"  [!] FitGirl's site: {error}", file=sys.stderr)
    print(f"FitGirl catalogue: {len(data['posts'])} repacks "
          f"(synced {data.get('synced_at') or 'never'}).")
    if wishlist is None:
        print("Game-db isn't set up, so there's no wishlist to compare.")
        return
    print(f"{len(matches)} of {len(wishlist)} wishlist games have a repack:")
    for a, m in sorted(matches.items(), key=lambda kv: (titles.get(kv[0]) or '').lower()):
        likely = '' if m['confidence'] == 'exact' else '  (likely)'
        hv = f"  [{m['hv'].replace('both', 'HV + non-HV').upper()}]" if m.get('hv') else ''
        print(f"  {titles.get(a) or a}: {m['title']}{likely}{hv}\n    {m['url']}")


def _size_bytes(text: str | None) -> int | None:
    """"from 47.4 GB [Selective Download]" -> bytes (the largest figure)."""
    units = {'kb': 1 << 10, 'mb': 1 << 20, 'gb': 1 << 30, 'tb': 1 << 40}
    found = [float(n.replace(',', '.')) * units[u.lower()]
             for n, u in re.findall(r'(\d+(?:[.,]\d+)?)\s*([KMGT]B)', text or '', re.I)]
    return int(max(found)) if found else None


def _torrent_dest(s: dict) -> str | None:
    from nexus import torrent as T
    if s.get('download_dir'):
        return s['download_dir']
    try:
        return T.client(s).info().get('download_dir')
    except T.TorrentError:
        return None


def _committed(s: dict, base: Path) -> dict | None:
    """{drive id: bytes} that the downloads and installs in flight still
    need on each drive (the pipeline's cards), or None when the torrent
    client can't be asked."""
    from nexus import torrent as T
    if not s['client']:
        return None
    try:
        c = T.client(s)
        cards, _drives = _pipeline(T.all_status(s, c), s, None, base)
    except T.TorrentError:
        return None
    out = {}
    for card in cards:
        for role in ('download', 'install'):
            drive = card['drives'][role]
            if drive is not None and card['need'][role]:
                out[drive] = out.get(drive, 0) + card['need'][role]
    return out


def cmd_repack_page(slug: str, as_json: bool, base: Path | None = None) -> None:
    """What a grab would download: sizes, where it goes and the free space
    there, and on the drive the game installs to (`base`), with what the
    downloads already in flight still need (`committed`). The magnet links
    themselves aren't printed."""
    from nexus import net, repackindex as RI, torrent as T
    try:
        page = RI.fetch_page(slug)
    except net.NetError as e:
        print(json.dumps({'error': str(e)}) if as_json else f"FitGirl's site: {e}")
        sys.exit(1)
    s = T.settings()
    dest = _torrent_dest(s) if s['client'] else None
    free = None
    if dest:
        try:
            free = shutil.disk_usage(Path(dest).expanduser()).free
        except OSError:
            free = None
    out = {'slug': slug, 'title': page['title'], 'url': page['url'],
           'repack_size': page['repack_size'], 'original_size': page['original_size'],
           'size_bytes': _size_bytes(page['repack_size']),
           'original_bytes': _size_bytes(page['original_size']), 'magnets': len(page['magnets']),
           'hv': page.get('hv'),
           'client': s['client'], 'dest': dest, 'free_bytes': free}
    if base is not None:                    # the install side (Grab's pre-flight check)
        root = existing_ancestor(Path(base).expanduser())
        dl, inst = drive_of(dest) if dest else None, drive_of(root)
        try:
            inst_free = shutil.disk_usage(root).free
        except OSError:
            inst_free = None
        out.update({'install_dest': str(base), 'install_free': inst_free,
                    'dest_drive': dl and dl['id'], 'dest_mount': dl and dl['mount'],
                    'install_drive': inst and inst['id'],
                    'install_mount': inst and inst['mount'], 'committed': _committed(s, base)})
    if as_json:
        print(json.dumps(out))
        return
    print(f"{page['title']}\n  {page['url']}\n  repack {page['repack_size'] or '?'}, "
          f"installed {page['original_size'] or '?'}; {len(page['magnets'])} magnet link(s)")
    if page.get('hv'):
        from nexus import hypervisor
        print(f"  {hypervisor.note(page['hv'])}")
    if dest:
        print(f"  downloads to {dest}" + (f" ({free / (1 << 30):.1f} GB free)" if free else ''))
    if out.get('install_free') is not None:
        print(f"  installs to {out['install_dest']} "
              f"({out['install_free'] / (1 << 30):.1f} GB free)")


def cmd_repack_grab(slug: str, appid: str | None, auto_install: bool, as_json: bool) -> None:
    """Read the repack page's magnet link and hand it to the torrent client."""
    from nexus import net, repackindex as RI, torrent as T
    try:
        page = RI.fetch_page(slug)
        if not page['magnets']:
            raise T.TorrentError(f"the repack page has no magnet link: {page['url']}")
        meta = {'source': 'fitgirl', 'slug': slug, 'title': page['title']}
        if appid:
            meta['appid'] = appid
        # Sizes for the downloads pipeline (nexus/pipeline.py): what comes
        # down, and what the install should write ("about": a label).
        for key, text in (('repack_bytes', page.get('repack_size')),
                          ('original_bytes', page.get('original_size'))):
            n = _size_bytes(text)
            if n:
                meta[key] = n
        got = T.add(page['magnets'][0]['uri'], auto_install=auto_install, meta=meta)
    except (net.NetError, T.TorrentError) as e:
        if as_json:
            print(json.dumps({'error': {'kind': 'torrent', 'message': str(e)}}))
        print(f"Couldn't grab it: {e}", file=sys.stderr)
        sys.exit(1)
    got['repack_size'] = page['repack_size']
    if as_json:
        print(json.dumps(got))
        return
    print(f"Downloading {got['name'] or page['title']} ({page['repack_size'] or '?'})"
          + (" — Nexus installs it when it finishes." if auto_install else "."))


def cmd_repack_reject(argv: list[str]) -> None:
    from nexus import repackindex as RI
    appid, slug = argv[0], (argv[1] if len(argv) > 1 else None)
    if not appid.isdigit() or (slug and not RI._SLUG_RE.match(slug)):
        print("usage: --repack-reject APPID [SLUG]  (no SLUG: forget the rejection)",
              file=sys.stderr)
        sys.exit(2)
    RI.reject(appid, slug)
    print(f"Won't match {appid} with {slug}." if slug else f"Matching {appid} again.")


def _owned_store_ids() -> dict[int, str]:
    """{store appid: library slug} for everything you have: installed Steam
    games, Steam games Game-db knows you own, and repacks (by umu GAMEID)."""
    from nexus import steamlib, gamedb, compat
    out = {}
    try:
        for item in gamedb.owned_library():               # cache only
            out[int(item['appid'])] = f"steam-{item['appid']}"
    except Exception:                                     # noqa: BLE001 — optional
        pass
    for g in steamlib.scan(STEAM_ROOT, _read_compat_mapping()):
        out[int(g['appid'])] = g['slug']
    for slug, e in load_registry()['games'].items():
        a = compat.store_appid(e)
        if a:
            out[a] = slug
    return out


def _torrent_mark(h: str, **fields) -> None:
    """Record an install's progress on the download it came from: started
    when the install begins, installed only once it has succeeded."""
    from nexus import torrent as T
    try:
        h = T.check_hash(h)
    except T.TorrentError:
        return
    try:
        with T.edit_tracked() as tracked:
            if h in tracked:
                tracked[h].update(fields)
    except OSError:
        pass


def _pipeline(rows: list, s: dict, info: dict | None, base: Path) -> tuple[list, list]:
    """The downloads pipeline's cards and drives for `rows` (all_status):
    the install progress files folded in (a pid snapshot only when some
    file is recent), and every drive a card still writes to, with its free
    space. Runs on every poll (2 s while the page shows), so a folder is
    looked up only for a download still coming: a finished, gone or other
    torrent's folder (maybe a network share that's down) is never touched.
    Windows: realpath turns a mapped drive (Z:) into its UNC path, so its
    "mount" is the share. See nexus/pipeline.py."""
    from nexus import pipeline as PL, platform, torrent as T
    now = time.time()
    progress = T.load_progress()
    alive = set()
    if any(PL.recent(p, now) for p in progress.values()):
        try:                                       # never os.kill(pid, 0): Windows kills
            alive = {p.pid for p in platform.processes()}
        except OSError:
            pass
    found = {}                                     # id -> mount
    install = drive_of(existing_ancestor(Path(base).expanduser()))
    dest = s.get('download_dir') or (info or {}).get('download_dir')
    download = drive_of(dest) if dest else None
    roles = {}
    for role, d in (('download', download), ('install', install)):
        if d:
            found[d['id']] = d['mount']
            roles.setdefault(d['id'], []).append(role)
    by_dir = {}
    for r in rows:
        folder = r.get('dir')
        if (not r.get('tracked') or not folder or folder in by_dir
                or PL.download_state(r) in ('done', 'gone') or r.get('installed')):
            continue
        d = drive_of(folder)
        by_dir[folder] = d and d['id']
        if d:
            found[d['id']] = d['mount']
            if 'download' not in roles.setdefault(d['id'], []):
                roles[d['id']].append('download')
    drives = []
    for drive, mount in found.items():
        try:
            u = shutil.disk_usage(mount)
        except OSError:
            continue
        drives.append({'id': drive, 'mount': mount, 'free': u.free, 'total': u.total,
                       'roles': roles[drive]})
    cards = PL.cards(rows, progress, now, alive,
                     drive_ids={'install': install and install['id'], 'download': by_dir},
                     stats=T.load_stats())
    return cards, PL.forecast(cards, drives)


def cmd_torrent(args, base: Path | None = None) -> None:
    """--torrent-add / --torrents [--all] / --torrent-pause|resume|remove|queue|while-playing /
    --torrent-test / --torrent-done / --torrent-forget / --torrent-clear-history."""
    from nexus import pipeline, torrent as T
    cards = []
    try:
        if args.torrent_test or args.torrent_detect:
            if args.torrent_detect:
                found = T.detect()
                print(json.dumps({'found': found}) if args.json else
                      (f"{found['client']} answers at {found['url']}" if found
                       else "No torrent client answers on its default port."))
                return
            info = T.client().info()
            print(json.dumps({'ok': True, **info}) if args.json else
                  f"{info['client']} {info['version']} answers"
                  + (f"; it downloads to {info['download_dir']}" if info['download_dir'] else ''))
            return
        for action in ('pause', 'resume', 'remove'):
            hashes = getattr(args, f'torrent_{action}')
            if hashes:
                T.control(action, hashes, delete_data=args.delete_data)
                done = {'pause': 'Paused', 'resume': 'Resumed', 'remove': 'Removed'}[action]
                print(json.dumps({'ok': True}) if args.json else
                      f"{done} {len(hashes)} torrent(s).")
                return
        if args.torrent_while_playing:
            from nexus import slowplay
            from .agent import playing
            when = args.torrent_while_playing
            did = slowplay.sync(playing() if when == 'sync' else when == 'start')
            print(json.dumps({'did': did}) if args.json else f"Slow while playing: {did}.")
            return
        if args.torrent_queue:
            T.queue_move(args.torrent_queue[1], args.torrent_queue[0])
            print(json.dumps({'ok': True}) if args.json else
                  f"Moved the download {args.torrent_queue[0]} in your client's queue.")
            return
        if args.torrent_add:
            got = T.add(args.torrent_add, auto_install=args.auto_install)
            if args.json:
                print(json.dumps(got))
            else:
                print(f"Added {got['name'] or got['hash']} to your torrent client"
                      + (" — Nexus installs it when it finishes." if args.auto_install else "."))
            return
        if args.torrent_clear_history:
            # Done recently's Clear: a mark only (the agent never reads it).
            # The known ones are cleared; unknown ones are reported, and only
            # when none was known does it fail.
            hashes = [h.lower() for h in args.torrent_clear_history]
            stamp = datetime.now().isoformat(timespec='seconds')
            with T.edit_tracked() as tracked:
                cleared = [h for h in hashes if h in tracked]
                for h in cleared:
                    tracked[h]['history_cleared'] = stamp
            unknown = [h for h in hashes if h not in cleared]
            if args.json:
                print(json.dumps({'cleared': cleared, 'unknown': unknown}))
            if unknown:
                print(f"Not a torrent Nexus added: {', '.join(unknown)}", file=sys.stderr)
            if not cleared:
                sys.exit(1)
            return
        if args.torrent_done or args.torrent_forget:
            h = (args.torrent_done or args.torrent_forget).lower()
            with T.edit_tracked() as tracked:
                if h not in tracked:
                    print(f"Not a torrent Nexus added: {h}", file=sys.stderr)
                    sys.exit(1)
                if args.torrent_forget:
                    tracked.pop(h)
                else:
                    tracked[h]['installed'] = True
            return
        if args.all:
            s = T.settings()
            c = T.client(s)
            rows, info = T.all_status(s, c), c.info()     # one client: one session-id handshake
            if info.get('config_dir'):
                info['others'] = T.other_transmissions(info['config_dir'])
            cards, drives = _pipeline(rows, s, info, base) if base else ([], [])
            if args.json:
                print(json.dumps({'torrents': rows, 'client': info, 'pipeline': cards,
                                  'drives': drives}))
                return
            for o in info.get('others') or []:
                print(f"  [!] {o['name']} (pid {o['pid']}, {o['config_dir']}) is another "
                      f"Transmission with its own torrents; Nexus follows the one in "
                      f"{info['config_dir']}.", file=sys.stderr)
        else:
            rows = T.status()
    except T.TorrentError as e:
        if args.json:
            print(json.dumps({'error': {'kind': 'torrent', 'message': str(e)}, 'torrents': [],
                              'configured': T.settings()['client'] is not None}))
        print(f"Torrent client: {e}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps({'torrents': rows}))
        return
    if not rows:
        print("No downloads added from Nexus." if not args.all else "No torrents.")
    by_hash = {c['hash']: c for c in cards} if args.all else {}
    for r in rows:
        if r.get('hash') in by_hash:               # a download on its way to Play
            print(f"  {pipeline.describe(by_hash[r['hash']])}")
            continue
        state = ("gone from the client" if r.get('gone') else "installed" if r.get('installed')
                 else "done" if r.get('done') else f"{r['percent']:.0f}%")
        print(f"  {r['name'] or r['hash']}: {state}")


def send_alerts(new: list[dict], desktop: bool = False) -> None:
    from nexus import alerts
    for err in alerts.deliver(new, desktop=desktop):
        print(f"  [!] alert: {err}", file=sys.stderr)


def cmd_check_repack(path: str, as_json: bool, clamav: bool) -> None:
    from nexus import safety
    d = Path(path).expanduser()
    if not d.is_dir():
        print(f"Not a folder: {d}", file=sys.stderr)
        sys.exit(1)
    r = safety.check(d, clamav=clamav)
    if as_json:
        print(json.dumps(r))
        return
    print({'ok': "Looks like a normal FitGirl repack.",
           'warn': "Some things look unusual — check them before installing.",
           'danger': "This repack looks tampered with. Don't install it."}[r['verdict']])
    for f in r['findings']:
        print(f"  [{f['level']}] {f['file']}: {f['why']}")
    if clamav and not (r['clamav'] or {}).get('available'):
        print(f"  ClamAV isn't installed ({install_hint('clamav', 'clamav')}, then sudo freshclam).")
    sys.exit(2 if r['verdict'] == 'danger' else 0)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .protoncmds import install_hint  # noqa: E402
from .registry import load_registry  # noqa: E402
from .steam import _read_compat_mapping  # noqa: E402
