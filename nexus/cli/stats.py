"""The anonymous install count from the command line (nexus/stats.py does
the work; Settings → System → Privacy shows the same):

    --stats-ping      count this install if it's on and a period is due
                      (the window and the agent run it; quiet otherwise)
    --stats-preview   exactly what a ping would send now, even while it's off
    --stats-sent      the last 100 pings sent from this computer, newest first

All three take --json."""

import json

from nexus import stats as ST
from nexus.report import relay as RL

from .config import load_config

WHY = {'unasked': "not asked yet ([stats] count isn't set)",
       'off': "off ([stats] count = false)",
       'blocked': "on, but {} stops it"}
BLOCKERS = {'NEXUS_NO_STATS': "NEXUS_NO_STATS", 'DO_NOT_TRACK': "DO_NOT_TRACK",
            'relay': "[report] relay = false"}


def _state_text(cfg: dict) -> str:
    st = ST.state_of(cfg)
    if st == 'on':
        return "on"
    return WHY[st].format(BLOCKERS.get(ST.blocked(cfg) or '', ''))


def cmd_stats_ping(as_json: bool) -> None:
    out = ST.ping(load_config())
    if as_json:
        print(json.dumps(out))
    elif out.get('sent'):
        print("Counted." if out['ok'] else f"Not counted ({out['error']}); trying again later.")


def cmd_stats_preview(as_json: bool) -> None:
    cfg = load_config()
    state = ST.load_state()
    due = ST.due(state)
    out = {'state': ST.state_of(cfg), 'count': cfg.get('stats_count'),
           'blocked': ST.blocked(cfg), 'url': RL.relay_url(cfg), 'due': due,
           'payload': ST.payload(due or ['d'], state)}
    if as_json:
        print(json.dumps(out))
        return
    print(f"Install count: {_state_text(cfg)}")
    print(f"Sent to: {out['url'] + '/v1/count' if out['url'] else 'nowhere'}")
    print(json.dumps(out['payload'], indent=2))


def cmd_stats_sent(as_json: bool) -> None:
    rows = ST.load_sent()
    if as_json:
        print(json.dumps({'sent': rows}))
        return
    if not rows:
        print("Nothing sent from this computer.")
    for r in rows:
        a = r['answer']
        print(f"{r['at']}  {a.get('status') or a.get('error') or '?'}  "
              f"{json.dumps(r['payload'], separators=(',', ':'))}")
