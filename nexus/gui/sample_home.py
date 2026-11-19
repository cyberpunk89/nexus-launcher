"""Sample data for the Customise gallery's previews (w-158): a small made-up
library and the answers Home's widgets read (insights, play history, news,
deals, friends, system), all relative to `today`. Plain data, no Qt and no
paths: the previews never read the user's files, ask the CLI or fetch art.
Nothing here is Linux-only, so a preview looks the same on Windows."""

from datetime import date, datetime, timedelta

GB = 1024 ** 3

# (slug, name, hours played, days since last played or None, extra)
_GAMES = (
    ('octopath2', "OCTOPATH TRAVELER II", 25.0, 1, {'favorite': True, 'added_days': 20}),
    ('lad-iw', "Like a Dragon: Infinite Wealth", 61.0, 2, {'added_days': 60}),
    ('disco', "Disco Elysium", 41.0, 45, {'favorite': True, 'collections': ['Story']}),
    ('tunic', "TUNIC", 12.0, 5, {'update_pending': True, 'added_days': 3}),
    ('hades', "Hades", 0.0, None, {'collections': ['Couch']}),
    ('pop-tlc', "Prince of Persia The Lost Crown", 15.0, 9, {'added_days': 12}),
    ('papers', "Papers, Please", 0.0, None, {'collections': ['Story']}),
    ('ff7r', "FINAL FANTASY VII REMAKE INTERGRADE", 23.0, 420, {'favorite': True}),
    ('yakuza6', "Yakuza 6: The Song of Life", 20.7, 700, {'update_pending': True}),
    ('lis-de', "Life is Strange: Double Exposure", 9.8, 15, {'collections': ['Story']}),
)
_APPID = {'octopath2': 1971650, 'lad-iw': 2072450, 'disco': 632470, 'tunic': 553420,
          'hades': 1145360}


def games(today: date) -> list[dict]:
    out = []
    for slug, name, hours, ago, extra in _GAMES:
        g = {'slug': slug, 'display_name': name, 'source': 'nexus', 'launcher': f'/sample/{slug}',
             'installed': True, 'playtime_minutes': int(hours * 60)}
        if ago is not None:
            g['last_played'] = (datetime.combine(today, datetime.min.time())
                                - timedelta(days=ago, hours=-20)).isoformat(timespec='seconds')
        added = extra.get('added_days', 200)
        g['added_at'] = (datetime.combine(today, datetime.min.time())
                         - timedelta(days=added)).isoformat(timespec='seconds')
        g.update({k: v for k, v in extra.items() if k != 'added_days'})
        if slug in _APPID:
            g['store_appid'] = _APPID[slug]
        out.append(g)
    return out


_MAIN = {'octopath2': 61, 'lad-iw': 100, 'disco': 32, 'tunic': 14, 'hades': 23, 'pop-tlc': 24,
         'papers': 5, 'ff7r': 33, 'yakuza6': 25, 'lis-de': 11}


def facts() -> dict:
    """Store facts by slug (smart rules, Your save's main story)."""
    return {slug: {'length': {'main': h},
                   'modes': ['single'] + (['coop'] if slug == 'hades' else [])}
            for slug, h in _MAIN.items()}


def insights(today: date) -> dict:
    """The --insights payload's shape (HomeFeed.data), with every part filled."""
    days = {(today - timedelta(days=i)).isoformat(): float((i * 37) % 180 if i % 3 else 0)
            for i in range(0, 365)}
    return {
        'v': 1, 'today': today.isoformat(), 'days': days,
        'streak': {'now': 3, 'best': 9}, 'busiest_day': 'Saturday', 'year_minutes': 8520.0,
        'on_this_day': [{'slug': 'disco', 'name': "Disco Elysium", 'years_ago': 1,
                         'date': (today - timedelta(days=365)).isoformat(), 'minutes': 160.0},
                        {'slug': 'ff7r', 'name': "FINAL FANTASY VII REMAKE INTERGRADE",
                         'years_ago': 2, 'date': (today - timedelta(days=730)).isoformat(),
                         'minutes': 95.0}],
        'daily': {'slug': 'pop-tlc', 'reason': 'released', 'years': 2},
        'daily_alt': {'slug': 'hades', 'reason': 'pick', 'years': None},
        'milestones': [{'kind': 'hours', 'slug': 'lis-de', 'have': 9.8, 'goal': 10,
                        'name': "Life is Strange: Double Exposure"},
                       {'kind': 'hours', 'slug': 'ff7r', 'have': 23.0, 'goal': 25,
                        'name': "FINAL FANTASY VII REMAKE INTERGRADE"},
                       {'kind': 'hours', 'slug': 'yakuza6', 'name': "Yakuza 6: The Song of Life",
                        'have': 20.7, 'goal': 25},
                       {'kind': 'year', 'slug': None, 'name': None, 'have': 142.0, 'goal': 150}],
        'backlog': {'done': 17, 'playing': 8, 'started': 9, 'never': 14, 'main_hours_left': 376.0,
                    'pace_hours_week': 9.4, 'idle_year_bytes': 287 * GB,
                    'shortest': [{'slug': 'papers', 'name': "Papers, Please", 'main_hours': 5.0}]},
        'space': [{'slug': 'ff7r', 'bytes': 148 * GB, 'idle_days': 420},
                  {'slug': 'yakuza6', 'bytes': 61 * GB, 'idle_days': 700},
                  {'slug': 'lad-iw', 'bytes': 82 * GB, 'idle_days': 2},
                  {'slug': 'octopath2', 'bytes': 9 * GB, 'idle_days': 1},
                  {'slug': 'disco', 'bytes': 20 * GB, 'idle_days': 30}],
        'space_more': {'n': 28, 'bytes': 300 * GB},
        'facts': [{'kind': 'library', 'slug': None, 'name': None, 'n': 48, 'hours': 812.4},
                  {'kind': 'shortest_unplayed', 'slug': 'papers', 'name': "Papers, Please",
                   'n': 5.0}],
    }


def history(today: date) -> dict:
    """--history's shape (Your week, Your save)."""
    monday = today - timedelta(days=today.weekday())
    mins = (95, 140, 0, 60, 125, 0, 0)
    week_days = [{'date': (monday + timedelta(days=i)).isoformat(),
                  'minutes': mins[i] if monday + timedelta(days=i) <= today else 0}
                 for i in range(7)]
    total = sum(d['minutes'] for d in week_days)
    return {'since': (today - timedelta(days=400)).isoformat(),
            'weeks': [{'start': monday.isoformat(), 'minutes': total}],
            'by_game': {'octopath2': {'sessions': 31}, 'lad-iw': {'sessions': 44}},
            'periods': {'today': today.isoformat(),
                        'week': {'minutes': total, 'days': week_days,
                                 'games': [{'slug': 'lad-iw', 'minutes': int(total * 0.6),
                                            'name': "Like a Dragon: Infinite Wealth"},
                                           {'slug': 'octopath2', 'name': "OCTOPATH TRAVELER II",
                                            'minutes': int(total * 0.3)},
                                           {'slug': 'tunic', 'name': "TUNIC",
                                            'minutes': int(total * 0.1)}]},
                        'last_week': {'minutes': max(0, total - 126),
                                      'to_date': max(0, total - 126)},
                        'month': {'minutes': total * 3, 'days': [], 'games': []}}}


def news(today: date) -> list[dict]:
    """News page items (no images: nothing is fetched)."""
    at = datetime.combine(today, datetime.min.time()) + timedelta(hours=9)
    rows = (("Proton 9.0-4 fixes 40 more games, including Like a Dragon: Infinite Wealth",
             'GamingOnLinux', 'rss', None, 2),
            ("TUNIC update 1.3 adds a new accessibility mode", 'Steam', 'steam', 553420, 5),
            ("Steam Deck client beta: per-game frame limiter", 'Steam', 'rss', None, 26),
            ("OCTOPATH TRAVELER II: patch notes", 'Steam', 'steam', 1971650, 30),
            ("Hades II leaves early access next month", 'PC Gamer', 'rss', None, 40),
            ("Disco Elysium's writers on their next game", 'Rock Paper Shotgun', 'rss', None, 50))
    return [{'title': t, 'source': s, 'kind': k, 'appid': a, 'url': '',
             'date': (at - timedelta(hours=h)).isoformat(timespec='seconds')}
            for t, s, k, a, h in rows]


def deals(today: date) -> dict:
    """The Game-db summary the Deals page holds (no images)."""
    def price(cur, cut):
        return {'onSale': True, 'cutPct': cut, 'currentStr': cur}
    soon = today + timedelta(days=9)
    return {'currency': 'EUR',
            'wishlist': [{'appid': 1, 'title': "Sea of Stars", 'price': price("12,49 €", 50)},
                         {'appid': 2, 'title': "Dredge", 'price': price("9,99 €", 60)},
                         {'appid': 3, 'title': "Cocoon", 'price': price("14,99 €", 40)},
                         {'appid': 4, 'title': "Celeste", 'price': price("4,99 €", 75)},
                         {'appid': 5, 'title': "Hollow Knight: Silksong", 'comingSoon': True,
                          'releaseDate': f"{soon.isoformat()}T00:00:00Z",
                          'releaseDateText': soon.strftime('%d %b %Y')},
                         {'appid': 6, 'title': "Slay the Spire 2", 'comingSoon': True,
                          'releaseDateText': "March"}],
            'radar': {'onSaleCount': 4, 'totalSavings': 41}}


def friends(now: float) -> dict:
    """A --friends answer: four public friends, three in games (no avatars)."""
    def rec(sid, name, playing=None, owned=None, state=1):
        r = {'steamid': sid, 'name': name, 'source': 'steam', 'visible': True,
             'evidence': {'personastate': state}, 'owned': owned or {}}
        if playing:
            r['playing'] = {'appid': playing, 'since': now - 3600}
        return r
    return {'enabled': True, 'updated': now,
            'friends': [rec('76561190000000001', "Ana", 1145360, {'1145360': 3840}),
                        rec('76561190000000002', "Ben", 1145360, {'1145360': 720}),
                        rec('76561190000000003', "Mo", 632470, {'632470': 2460}),
                        rec('76561190000000004', "Kit", None, {'553420': 300})],
            'by_app': {'1145360': ['76561190000000001', '76561190000000002'],
                       '632470': ['76561190000000003'], '553420': ['76561190000000004']}}


def system() -> dict:
    """What System's tiles read: disks, checks, Proton."""
    return {'disks': [{'path': '/games', 'total': 2000 * GB, 'used': 1240 * GB,
                       'free': 760 * GB}],
            'checks': [{'status': 'pass', 'title': f"Check {i}"} for i in range(12)],
            'proton': {'families': {'ge': {'update': False, 'latest': 'GE-Proton9-20'}},
                       'default': 'GE-Proton9-20'}}


def moments() -> list[dict]:
    """For you now's moments (the Strip)."""
    return [{'id': 'sample-resume', 'title': "Like a Dragon: Infinite Wealth",
             'why': "Paused 2 days ago at 61 %", 'action': {'label': "Resume"},
             'badge': ('play', 'accent')},
            {'id': 'sample-update', 'title': "TUNIC", 'why': "An update is ready",
             'action': {'label': "Update"}, 'badge': ('download', 'warning')}]
