"""Per-game information: history, store pages, tips, news, Game-db."""

import json
import re
import sys
from datetime import datetime

from nexus import features

from .common import _cached_read, _load_toml, CONFIG_PATH, STEAM_ROOT


def cmd_history(as_json: bool) -> None:
    from nexus import history
    sm = history.summary()
    if as_json:
        print(json.dumps(sm))
        return
    y = sm['year']
    if not y['sessions']:
        print("No play sessions recorded yet (they're recorded from now on).")
        return
    print(f"{y['year']}: {y['minutes'] / 60:.1f} h over {y['sessions']} sessions, "
          f"{y['games']} games, {y['days']} days (best streak {y['best_streak']} days)")
    if y['longest']:
        print(f"  Longest session: {y['longest']['name']}, {y['longest']['minutes'] / 60:.1f} h "
              f"on {y['longest']['date']}")
    for t in y['top']:
        print(f"  {t['minutes'] / 60:6.1f} h  {t['name']}")


def cmd_store_info(ident: str, as_json: bool, refresh: bool = False) -> None:
    """Store page facts for any game: trailers, reviews, metacritic, genres
    and similar games (owned ones marked)."""
    from nexus import storeinfo, compat
    g = _game_by_id(ident, light=True)
    if not g:
        print(f"Unknown game {ident!r}", file=sys.stderr)
        sys.exit(1)
    info = storeinfo.info(compat.store_appid(g), refresh)
    if info['similar']:
        owned = _owned_store_ids()
        for s_ in info['similar']:
            s_['owned_slug'] = owned.get(s_['appid'])
    if as_json:
        print(json.dumps(info))
        return
    st, rv = info['store'] or {}, info['reviews'] or {}
    if not st and not rv:
        print("The Steam store has nothing on this game.")
        return
    print(f"{st.get('name') or ident}  {st.get('url') or ''}")
    if st.get('blurb'):
        print(f"  {st['blurb']}")
    if rv:
        print(f"  Reviews: {rv['desc']} ({rv.get('percent')}% of {rv['total']})")
    if st.get('metacritic'):
        print(f"  Metacritic: {st['metacritic']['score']}")
    if st.get('genres'):
        print(f"  {', '.join(st['genres'])} · {st.get('release') or ''}")
    for t in st.get('trailers') or []:
        print(f"  Trailer: {t['name']}  {t['url']}")
    if info['similar']:
        print("  More like this: " + ", ".join(
            s_['name'] + (' (yours)' if s_.get('owned_slug') else '') for s_ in info['similar'][:8]))


def _facts_library() -> list[dict]:
    """[{slug, appid, name}] for every library game with a Steam store id:
    repacks by their umu GAMEID, installed Steam games, then owned Steam
    games Game-db knows (cache only; none when it's off)."""
    from nexus import compat, gamedb, steamlib
    out, seen = [], set()

    def add(slug, appid, name):
        if appid and slug not in seen:
            seen.add(slug)
            out.append({'slug': slug, 'appid': appid, 'name': name})
    for slug, e in load_registry()['games'].items():
        add(slug, compat.store_appid(e), e.get('display_name') or slug)
    for g in steamlib.scan(STEAM_ROOT):
        add(g['slug'], compat.store_appid(g), g.get('display_name'))
    try:
        owned = gamedb.owned_library()                    # cache only
    except Exception:                                     # noqa: BLE001 — optional
        owned = []
    for item in owned:
        add(f"steam-{item['appid']}", compat.store_appid(
            {'source': 'steam', 'appid': item['appid']}), item.get('title'))
    return out


def cmd_game_facts(refresh: bool, as_json: bool, fill_n: int | None = None) -> None:
    """Rebuild the facts index (~/.cache/nexus/facts.json) from cached store
    and HowLongToBeat data. --refresh, or the app's --fill N, first fetches
    what's missing within the hourly budget (nexus/facts.py)."""
    from nexus import facts
    lib = _facts_library()
    res = None
    if refresh or fill_n is not None:
        n = facts.BUDGET if fill_n is None else max(0, min(fill_n, facts.BUDGET))
        res = facts.fill(lib, n)
    idx = facts.build(lib)
    facts.write_index(idx)
    if as_json:
        print(json.dumps({'built': idx['built'], 'games': idx['games'],
                          'missing': idx['missing'], 'fill': res}))
        return
    for slug, f in sorted(idx['games'].items(), key=lambda kv: (kv[1]['name'] or '').lower()):
        bits = [', '.join((f['developers'] or [])[:2]), str(f['year'] or ''),
                ', '.join(f['genres']), ', '.join(f['modes'] or []),
                (f['status'] or '').replace('_', ' '),
                f"main {f['length']['main']:g} h" if (f['length'] or {}).get('main') else '']
        print(f"{f['name'] or slug}  " + ' · '.join(b for b in bits if b))
    m = idx['missing']
    print(f"{len(idx['games'])} of {len(lib)} games with a store id have facts"
          + (f"; still to fetch: {m['steam']} store pages, {m['hltb']} play lengths"
             if m['steam'] or m['hltb'] else '')
          + ('' if refresh else ' (--refresh fetches them)' if m['steam'] or m['hltb'] else ''))
    if res and res['stopped']:
        print(f"  Stopped for now ({', '.join(res['stopped'])}): the hourly limit or a "
              "rate limit was reached; Nexus carries on later.", file=sys.stderr)


def compat_lines(info: dict) -> list[str]:
    """Plain-text anti-cheat / Steam Deck / PCGamingWiki lines for --game-tips."""
    out = []
    ac = info.get('anticheat')
    if ac:
        out.append(f"Anti-cheat: {ac['status']} ({', '.join(ac['anticheats']) or '?'})"
                   f"  {ac['url']}")
    dk = info.get('deck')
    if dk:
        out.append("Steam Deck: " + (dk.get('deck') or 'unknown')
                   + (f", SteamOS {dk['steamos']}" if dk.get('steamos') else ''))
    pw = info.get('pcgw')
    if pw:
        sup = ', '.join(f"{k} {v}" for k, v in pw['support'].items())
        out.append(f"PCGamingWiki: {sup or 'no support table'}  {pw['url']}")
        for f in pw['fixes']:
            out.append(f"  fix: {f}")
    return out


def cmd_game_tips(ident: str, as_json: bool) -> None:
    """Everything the details panel shows beyond the basics, for any game:
    PCGamingWiki, Game-db and HowLongToBeat facts everywhere; where Proton is
    (Linux), also protongen's saved tuning, ProtonDB tier (Steam games),
    Steam Deck and anti-cheat status, and the protongen recipes that fit this
    machine. Without Proton those keys are there but empty."""
    from nexus import tips, protondb
    g = _game_by_id(ident, light=True)
    if not g:
        print(f"Unknown game {ident!r}", file=sys.stderr)
        sys.exit(1)
    from nexus import compat
    proton = features.has('proton')
    hw = tips.hardware() if proton else {'gpus': []}
    tier = None
    pdb = None
    store = compat.store_appid(g)            # a repack's comes from its umu GAMEID
    if store and proton:
        pdb = protondb.tiers([store]).get(store)
        tier = (pdb or {}).get('tier')
    pg = None
    if not proton:
        hit = None
    elif g.get('source') == 'nexus':
        hit = protongen_memory_for(g)
    else:
        pid = _protongen_id(g)
        hit = None
        try:
            mem = _cached_read(PROTONGEN_STATE, _load_toml).get('game_memory', {})
            cfg = mem.get(str(pid)) if pid else None
            hit = ('protongen', cfg) if isinstance(cfg, dict) else None
        except (OSError, ValueError):
            pass
    if hit:
        prof = profile_from_protongen_config(hit[1])
        pg = {'source': hit[0], 'launch_options': format_launch_options(prof),
              'proton': prof.get('proton')}
    cur = format_launch_options(g.get('launch') or {}) if g.get('source') == 'nexus' else None
    recipes = tips.recipes_for(hw, tier) if proton else []
    # Deck and Are We Anti-Cheat Yet are about running under Proton; PCGamingWiki isn't.
    facts = compat.facts(store) if proton else {
        'anticheat': None, 'deck': None,
        'pcgw': compat.pcgw([store]).get(store) if store else None}
    info = {'id': ident, 'protondb': pdb, 'protongen': pg,
            'protongen_id': _protongen_id(g) if proton else None,
            'recipes': recipes, 'current': cur,
            'gpu': next((f"{x['name']} ({x['gen'] or x['vendor']})" for x in hw['gpus']
                         if not x['integrated']), None),
            'llm': _llm_config() is not None, 'gamedb': None,
            'store_appid': store, **facts}
    try:
        from nexus import gamedb
        appid = g['appid'] if g.get('source') == 'steam' else g.get('steam_appid')
        if appid and str(appid).isdigit():
            info['gamedb'] = gamedb.lookup([int(appid)]).get(int(appid))   # cache only
    except Exception:
        pass
    if not ((info.get('gamedb') or {}).get('hltb') or {}).get('main'):
        from nexus import hltb
        name = g.get('display_name') or g.get('name')
        if g.get('source') == 'steam' and not name:
            full = _game_by_id(ident)
            name = (full or {}).get('display_name') or (full or {}).get('name')
        info['hltb'] = hltb.lookup(name) if name else None
    if as_json:
        print(json.dumps(info))
        return
    if pdb:
        print(f"ProtonDB: {pdb.get('tier') or 'no reports'}  {pdb['url']}")
    if info.get('hltb'):
        h = info['hltb']
        print(f"HowLongToBeat: main {h['main'] or '?'} h · +extras {h['plus'] or '?'} h · "
              f"100% {h['complete'] or '?'} h")
    for line in compat_lines(info):
        print(line)
    if pg:
        print(f"protongen ({pg['source']}): {pg['launch_options']}")
    if recipes:
        print(f"Suggestions for {info['gpu']}:")
    for r in recipes:
        print(f"  - {r['name']}: {r['launch_options']}")


def cmd_apply_recipe(slug: str, name: str, config: dict) -> None:
    """Merge a protongen recipe into a Nexus game's launch options (its env
    vars override same-named ones; its wrappers are added if missing)."""
    from nexus import tips
    entry = _require_entry(slug)
    rec = next((r for r in tips.recipes_for(tips.hardware(), None, ('profile', 'fix'))
                if r['name'] == name), None) or next(
        (r for r in tips.load_recipes()[0] if r.get('name') == name), None)
    if not rec:
        print(f"No recipe named {name!r}", file=sys.stderr)
        sys.exit(1)
    add = parse_launch_options(tips.recipe_launch_string(rec))
    prof = {**empty_profile(), **(entry.get('launch') or {})}
    env = {k: v for k, v in prof.get('env', [])}
    env.update({k: v for k, v in add['env']})
    prof['env'] = [[k, v] for k, v in env.items()]
    wr = list(prof.get('wrappers') or [])
    for seg in split_wrappers(add['wrappers']):
        if seg[0] not in wr:
            wr = seg + wr if seg[0] == 'gamescope' else wr + seg
    prof['wrappers'] = wr
    prof = _save_profile(slug, prof, f"recipe '{name}'")
    print(f"Applied '{name}' to {slug}: {format_launch_options(prof)}")
    cmd_apply(slug, config)


def _llm_config() -> dict | None:
    """protongen's local-LLM settings, only when enabled and pointing at this
    machine (log text never leaves it)."""
    try:
        st = __import__('tomllib').loads(PROTONGEN_STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    ep = st.get('llm_endpoint') or 'http://127.0.0.1:1234/v1'
    if not st.get('llm_enabled'):
        return None
    if not re.match(r'^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)', ep):
        return None
    return {'endpoint': ep.rstrip('/'), 'model': st.get('llm_model') or 'gpt-oss-20b'}


def cmd_explain_log(ident: str) -> None:
    """Ask the local LLM protongen is configured with to explain a game's
    latest launch/install log (error lines + tail). Loopback endpoints only."""
    import urllib.request
    cfg = _llm_config()
    if not cfg:
        print("Local AI is off. Enable it in protongen (Settings → AI log coach) with a "
              "local endpoint such as LM Studio.", file=sys.stderr)
        sys.exit(1)
    path = launch_log_path(ident)
    if not path.exists():
        path = latest_install_log(ident) or path
    try:
        text = path.read_text(encoding='utf-8', errors='replace')[-64_000:]
    except OSError:
        print(f"No log for {ident!r} yet", file=sys.stderr)
        sys.exit(1)
    errors = [l for l in text.splitlines()
              if re.search(r'\b(err|error|fail|fatal|exception|crash|unhandled)\b', l, re.I)][-80:]
    prompt = ("You help Linux gamers fix games running under Proton/Wine. Explain in plain "
              "words what went wrong and suggest concrete fixes (Proton env vars, a different "
              "Proton build, missing dependencies). Be brief.\n\nError lines:\n"
              + "\n".join(errors) + "\n\nLog tail:\n" + text[-12_000:])
    body = json.dumps({'model': cfg['model'], 'messages': [{'role': 'user', 'content': prompt}],
                       'temperature': 0.2}).encode()
    req = urllib.request.Request(cfg['endpoint'] + '/chat/completions', data=body,
                                 headers={'Content-Type': 'application/json'})
    print(f"Asking {cfg['model']} about {path.name}...", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            answer = json.loads(r.read())['choices'][0]['message']['content']
    except (OSError, ValueError, KeyError, IndexError) as e:
        print(f"The local model didn't answer: {e}", file=sys.stderr)
        sys.exit(1)
    print()
    print(answer.strip())


def cmd_news(refresh: bool, as_json: bool) -> None:
    from nexus import news, steamlib, protons as PR, net
    opts = news.options()
    games = [{'appid': g['appid'], 'name': g['display_name']}
             for g in steamlib.scan(STEAM_ROOT)] if opts['steam'] else []
    rel = []
    for fam in (('ge', 'cachyos') if opts['proton'] else ()):
        try:
            for r in PR.releases(fam)['releases'][:3]:
                rel.append({**r, 'family_label': PR.FAMILIES[fam]['label']})
        except net.NetError:
            pass
    data = news.collect(games, rel, refresh)
    data['feeds'] = news.all_feeds()
    data['options'] = opts
    if as_json:
        print(json.dumps(data))
        return
    for i in data['items'][:40]:
        print(f"{(i.get('date') or '')[:10]}  [{i['source']}] {i['title']}")
        if i.get('link'):
            print(f"            {i['link']}")
    bad = [s for s in data['sources'] if s['error']]
    for b in bad:
        print(f"  [!] {b['name']}: {b['error']}", file=sys.stderr)


def cmd_news_feed(action: str, url: str, name: str | None) -> None:
    from nexus import news
    # Feeds are fetched over https only (nexus/net.py); removal accepts any
    # URL so an old http:// entry can still be taken out.
    if action == 'add' and not re.match(r'^https://', url):
        print("Feed URL must start with https://", file=sys.stderr)
        sys.exit(2)
    user = news.user_feeds()
    if action == 'add':
        if any(f['url'] == url for f in user):
            print("Already added.")
            return
        user.append({'name': name or re.sub(r'^https?://(www\.)?', '', url).split('/')[0],
                     'url': url})
    else:
        user = [f for f in user if f['url'] != url]
    news.save_user_feeds(user)
    print(f"{'Added' if action == 'add' else 'Removed'} feed {url}")


def cmd_news_source(pairs: list[list[str]]) -> None:
    """Switch news sources (a feed id, or `steam` / `proton`) on or off."""
    from nexus import news
    for name, state in pairs:
        if state not in ('on', 'off'):
            print("Say 'on' or 'off'.", file=sys.stderr)
            sys.exit(2)
        on = state == 'on'
        ok = news.set_option(name, on) if name in news.OPTIONS else news.set_source(name, on)
        if not ok:
            ids = ', '.join([*news.OPTIONS, *(f['id'] for f in news.all_feeds())])
            print(f"No news source '{name}'. Choose from: {ids}", file=sys.stderr)
            sys.exit(2)
        print(f"News source {name}: {state}")


def _ago(ts: float) -> str:
    mins = int((datetime.now().timestamp() - ts) // 60)
    if mins < 1:
        return 'just now'
    if mins < 60:
        return f'{mins} min ago'
    if mins < 48 * 60:
        return f'{mins // 60} h ago'
    return f'{mins // 1440} days ago'


def cmd_gamedb(section: str, refresh: bool, as_json: bool, time_budget: str = '1to3') -> None:
    """Wishlist deals, upcoming releases and backlog picks from Game-db
    (cached 45 min; --refresh to bypass). section: all|deals|upcoming|backlog."""
    from nexus import gamedb, steamlib
    s = gamedb.settings()
    if not gamedb.configured(s):
        why = s['url_error'] or (
            "Game-db isn't set up — create a token on " f"{s['url']}/account and add it in "
            "Settings ([gamedb] token / NEXUS_GAMEDB_TOKEN, [gamedb] enabled = true)"
            if s['url'] else
            "Game-db isn't set up — add your Game-db server's address and a desktop app token "
            "in Settings ([gamedb] url, token, enabled = true)")
        if as_json:                   # the GUI renders the error itself
            print(json.dumps({'configured': False, 'error': {'kind': 'config', 'message': why},
                              'url': s['url']}))
            return
        print(why, file=sys.stderr)
        sys.exit(2)
    try:
        res = gamedb.summary(refresh=refresh)
    except gamedb.GameDbError as e:
        if as_json:
            print(json.dumps({'configured': True, 'url': s['url'],
                              'error': {'kind': e.kind, 'message': str(e)}}))
            return
        print(f"Game-db: {e}", file=sys.stderr)
        sys.exit(1)
    data = res['data']
    if as_json:
        installed = sorted({int(g['appid']) for g in steamlib.scan(STEAM_ROOT)
                            if str(g.get('appid', '')).isdigit()})
        out = {'configured': True, 'url': s['url'], 'fetched_at': res['fetched_at'],
               'stale': res['stale'], 'error': res['error'], 'installed_appids': installed}
        try:
            out['owned_appids'] = sorted(_owned_store_ids())
        except Exception:                                 # noqa: BLE001 — optional
            out['owned_appids'] = []
        if section in ('deals', 'all'):
            from nexus import repackindex
            out['repacks'] = repackindex.wishlist_matches(data.get('wishlist') or [])
        if section == 'deals':
            out['wishlist'] = data.get('wishlist', [])
            out['radar'] = data.get('radar')
        elif section == 'upcoming':
            out['upcoming'] = data.get('upcoming', [])
        elif section == 'backlog':
            out['tonight'] = (data.get('tonight') or {}).get(time_budget, [])
            out['backlog'] = data.get('backlog')
        else:
            out['summary'] = data
        print(json.dumps(out))
        return
    if res['error']:
        print(f"  [!] {res['error']['message']} — showing data from {_ago(res['fetched_at'])}",
              file=sys.stderr)
    cur = data.get('currency') or ''
    radar = data.get('radar') or {}
    if section in ('all', 'deals'):
        sale = [g for g in data.get('wishlist', []) if (g.get('price') or {}).get('onSale')]
        print(f"Wishlist deals — {len(sale)} on sale"
              + (f", {radar['totalNow']:.2f} {cur} for all of them" if radar.get('totalNow') else '')
              + f" (region {data.get('region')}, synced {_ago(res['fetched_at'])})")
        for g in sale:
            p = g['price']
            best = p.get('best') or {}
            shop = best.get('shop') or ('keyshop' if best.get('category') == 'keyshop' else '')
            cut = f"-{p['cutPct']}%" if p.get('cutPct') else ''
            atl = 'ATL' if p.get('atHistoricalLow') else ''
            print(f"  {p['currentStr']:>10} {cut:>5} {atl:3}  {g['title'][:44]:44}  "
                  f"low {p.get('historicalLowStr') or '—':>9}  {shop}")
        if not sale:
            print("  nothing on sale right now")
    if section in ('all', 'upcoming'):
        up = data.get('upcoming', [])
        print(f"\nUpcoming — {len(up)} games")
        for g in up:
            print(f"  {gamedb.release_label(g):>14}  {g['title']}"
                  + ('  (owned)' if g.get('owned') else ''))
    if section in ('all', 'backlog'):
        b = data.get('backlog') or {}
        print(f"\nBacklog — {b.get('beaten', 0)} beaten, {b.get('inProgress', 0)} in progress, "
              f"{b.get('notStarted', 0)} not started, ~{b.get('backlogHours', 0)} h left")
        labels = {'lt1h': 'under an hour', '1to3': '1–3 hours', '3plus': '3+ hours',
                  'long': 'a long session'}
        print(f"Tonight ({labels.get(time_budget, time_budget)}):")
        for pick in (data.get('tonight') or {}).get(time_budget, []):
            print(f"  {pick['title']}  — {'; '.join(pick.get('reasons') or [])}")


# config.toml keys that hold credentials: (section, key, what it is)
CONFIG_SECRETS = (('steamgriddb', 'api_key', 'SteamGridDB key'),
                  ('gamedb', 'token', 'Game-db token'),
                  ('torrent', 'password', 'torrent client password'),
                  ('notify', 'ntfy_token', 'ntfy token'))


def config_secrets_doctor(add, group: str) -> None:
    """--doctor: warn when config.toml holds a credential and others can read it
    (the GUI writes it 0600; a hand-made one is usually 0644)."""
    try:
        import tomllib
        data = tomllib.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
        mode = CONFIG_PATH.stat().st_mode & 0o777
    except (OSError, ValueError, ImportError):
        return
    held = [what for sec, key, what in CONFIG_SECRETS
            if isinstance(data.get(sec), dict) and data[sec].get(key)]
    if held and mode & 0o077:
        add(group, 'warn', 'Config file permissions',
            f"{CONFIG_PATH} is {oct(mode)} and holds your {', '.join(held)}",
            f"chmod 600 {CONFIG_PATH} {CONFIG_PATH}.bak*")


def gamedb_doctor(add, group: str) -> None:
    """Game-db checks for --doctor; cache-only, never touches the network."""
    from nexus import gamedb
    s = gamedb.settings()
    if not s['enabled'] and not s['token']:
        add(group, 'info', 'Game-db', 'not set up (optional) — wishlist deals, releases, backlog',
            'Create a token on Game-db (/account) and add it in Settings')
        return
    if s['url_error']:
        add(group, 'warn', 'Game-db', s['url_error'], 'Fix [gamedb] url in Settings')
        return
    if not s['token']:
        add(group, 'warn', 'Game-db', 'enabled but no token',
            f"Create one on {s['url']}/account and add it in Settings")
        return
    st = gamedb.last_state()
    try:
        res = gamedb.summary(cache_only=True)
    except gamedb.GameDbError:
        res = None
    err = (st.get('error') or {})
    if err.get('kind') == 'auth':
        add(group, 'warn', 'Game-db', err.get('message', 'token rejected'),
            f"Create a new token on {s['url']}/account")
    elif res:
        d = res['data']
        on_sale = (d.get('radar') or {}).get('onSaleCount', 0)
        add(group, 'ok', 'Game-db', f"{s['url']} — synced {_ago(res['fetched_at'])} · "
                                    f"{on_sale} on sale"
            + (f" · last error: {err['message']}" if err else ''))
        mine = (resolve_steam_account()[0] or {}).get('accountid')
        theirs = (d.get('user') or {}).get('steamId64')
        if mine and theirs and gamedb.steamid64(mine) != str(theirs):
            add(group, 'info', 'Game-db Steam account',
                f"Game-db tracks Steam {theirs}; Nexus is using {gamedb.steamid64(mine)}")
    else:
        add(group, 'ok' if not err else 'warn', 'Game-db',
            f"{s['url']} — not fetched yet" + (f" ({err['message']})" if err else ''),
            'Open the Deals page or run nexus-cli --deals')


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .apply import _require_entry, _save_profile, cmd_apply, latest_install_log  # noqa: E402
from .downloads import _owned_store_ids  # noqa: E402
from .launchprofile import (empty_profile, format_launch_options, parse_launch_options,
    profile_from_protongen_config, protongen_memory_for, PROTONGEN_STATE, split_wrappers)  # noqa: E402
from .manage import _game_by_id, _protongen_id  # noqa: E402
from .registry import load_registry  # noqa: E402
from .steam import resolve_steam_account  # noqa: E402
from .sunshine import launch_log_path  # noqa: E402
