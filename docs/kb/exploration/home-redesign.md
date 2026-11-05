# Home redesign: "Tonight"

Status: exploration, nothing built. Mockup: [home-redesign-mockup.html](home-redesign-mockup.html)
(open in a browser; change Time and Mood to see the ranking react; "Design notes" numbers each zone).

## The problem with Home today

Home is a dashboard: a hero banner, then up to 14 widget kinds (`DASH_KINDS` in
`nexus/gui/dashboard.py`) in a user-arranged grid. It is flexible, but:

- It makes you scan. Jump back in, Surprise me, Installed, Favourites, Collections, Smart
  shelves and Updates are all rows of the same game tiles, so the page says "here are your
  games" seven ways and never says "play this".
- Customise is the answer to every taste question, so the default is a compromise.
- Status (downloads, updates, deals, disk, health, news) competes with games for the same
  visual weight.
- It doesn't know when you are. A 30-minute weeknight and a Saturday afternoon get the same page.

## The idea

Home stops being a board of widgets and becomes one decision: **what are we playing now?**

Four zones, one line of status:

| # | Zone | What it does |
|---|------|--------------|
| 1 | **Ask** | Two inputs: *time you have* (30 min, 1 h, 2 h, all night) and *mood* (pick up, finish something, play with someone, try something new). Defaults come from the clock, the day and your history, so most nights you never touch it. |
| 2 | **Stage** | The single best pick, full bleed, tinted by its art. Says *why* in a sentence and whether it fits ("next save point in 25 min, inside your hour"). Play is the biggest thing on screen. |
| 3 | **Deck** | Four runners-up with their own reasons and a fits/over pill. Selecting one swaps it onto the stage. "Not this one" and "Shuffle" re-roll. |
| 4 | **Fortnight** | A stacked bar per day for 14 days, coloured by game, drawn to scale. Replaces the Play time and Achievements cards. |
| 5 | **Wire** | One scrollable line: downloads with progress, repack and Proton updates, wishlist deals, disk, news. Each chip opens its page. |

What goes away from the default Home: the widget grid, Customise's move/resize bars, and the
separate Jump back in, Installed, Favourites, Collections, Smart shelf and Surprise rows
(Surprise becomes "Shuffle the deck"). Those rows still exist as Library shelves.

The radical part is the *reason line*. Ranking is only trusted if it explains itself, so every
pick says what it is based on, in plain words.

## How ranking could work

Everything needed is already in the codebase:

- last played, play time, progress: `nexus/session.py` (sessions.jsonl), `nexus/history.py`
- how long to beat, to estimate time left: `nexus/hltb.py`
- rules for "nearly through", "unfinished", "short games": `nexus/smart.py`
- co-op flag and genres: `nexus/facts.py`
- save-point length per game: **new**. Start with a guess from the median length of your past
  sessions of that game; later let the user set it on the game page.

Score = mood term + time-fit term + small recency term (the prototype's `score()` is about ten
lines). Keep it pure functions over plain dicts, like `smart.py`, so it can be unit tested
without Qt, and put it in a new `nexus/tonight.py`.

## Behaviour details

- **Defaults:** time budget from the median length of sessions you start at this hour and
  weekday; mood from what you did last night. Remember the last choice per session only.
- **Empty and small libraries:** under 3 installed games, drop the deck and show the stage alone.
- **Stage tint:** reuses the colour-from-art logic Home and Theatre already have.
- **Controller / Theatre:** the zones map to one focus axis: Ask (left/right), Stage (Play
  default focus), Deck (down), Fortnight (skipped, read-only), Wire (bottom row). Theatre Home
  could use the same Stage and Deck with larger type and no Ask row (shown on Menu).
- **Windows and Linux:** same page. Nothing in it is platform specific.
- **Accessibility:** deck cards are real buttons with labels; the stage is an `aria-live`
  region so a swap is announced; the fortnight chart has a text equivalent (totals per game).

## Rollout without a big bang

1. Build `nexus/tonight.py` and its tests. No UI change.
2. Add a Stage widget as the new default hero. Keep the widget grid below it, behind a
   "More" fold. Ship as a setting: *Home style: Tonight / Classic*.
3. Add Deck and the Wire; fold Surprise into Shuffle.
4. Replace Play time with the Fortnight chart. Make Tonight the default once feedback is in.
5. Retire the widget grid only if nobody uses Classic.

## Risks and open questions

- Is a recommendation-first Home too opinionated for people who just want a shelf? Keeping
  *Classic* as a setting covers it.
- Poor first-week data: new installs have no history, so mood and time defaults are blind.
  Fall back to recency and "never played".
- Save-point estimates may be wrong. The reason line must say "about", and the fit pill
  should be hidden when the estimate is a guess.
- Do we still want user-arranged widgets at all, or is a fixed Home better?

## Two other directions considered

- **Shelf-first ("Library on the front page"):** Home is a single infinite poster wall with
  one sticky filter bar and no widgets. Cheapest to build, least new. Good for big libraries,
  weak on "what should I play".
- **Cockpit ("everything live"):** a dense, three-column mission-control Home: now playing,
  queue, downloads, sales, system, in tiny type. Good for power users and Linux tinkerers,
  poor on a couch or a controller.

Tonight was chosen because it makes Home do something no other tab does, and because it is
the one direction that gets better with the data Nexus already collects.
