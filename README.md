# Closing-Line & CLV Capture (The Odds API)

Automated Pinnacle closing-line capture + CLV/EV computation for your bet tracker.
**Reads odds data only. Never logs into or places bets at any bookmaker.**

---

## Files in this folder — what each is and what to do with it

| file                | purpose                                                                 | do you edit it? |
|---------------------|-------------------------------------------------------------------------|-----------------|
| `closing_line.py`   | The tool. Fetches closing lines, devigs, computes CLV/EV. Run this.      | No — just run it |
| `sample_input.csv`  | A generated template showing the exact input format (incl. spreads/totals). | Copy it, fill with your bets |
| `test_offline.py`   | Verifies the math with no network. Run once to confirm nothing's broken. | No |
| `README.md`         | This file.                                                              | No |
| `.env`              | **You create this.** Holds your API key so it's never pasted into code. | Yes — you make it |

### What YOU need to add
1. **Create a `.env` file** in this folder containing one line:
   ```
   ODDS_API_KEY=your_rotated_key_here
   ```
   (Rotate your key first if it's ever been exposed. `.gitignore` excludes `.env` so it won't get committed.)
2. **Create your bets CSV** by copying `sample_input.csv` and replacing the rows with your real bets.

That's it. You don't edit the Python.

---

## Setup & run (in your own environment — network required)

```bash
python3 --version            # 3.8+, standard library only, no pip installs
# put your key in .env  (see above), then:
python3 closing_line.py --make-sample          # regenerate the template if needed
python3 closing_line.py --in your_bets.csv --out enriched_bets.csv
```

Open `enriched_bets.csv`, paste the new columns into your master tracker.

---

## Input columns

| column              | meaning                                                        |
|---------------------|----------------------------------------------------------------|
| bet_id              | your own id                                                    |
| sport_key           | The Odds API sport key (soccer_epl, americanfootball_nfl, ...) |
| home_team, away_team| team names (fuzzy-matched, needn't be exact)                  |
| commence_time_iso   | scheduled kickoff, ISO8601, e.g. 2026-06-14T19:00:00Z          |
| market              | h2h \| spreads \| totals                                       |
| selection           | outcome backed (team name, or Over / Under)                    |
| **point_taken**     | **NEW.** The handicap/total you got. Leave blank for h2h.      |
| odds_taken          | decimal odds you got                                           |

---

## Output columns — the two new features

### 1. Three devig methods (was: multiplicative only)
For your side, the closing fair line is now computed three ways:

- `fair_prob_multiplicative` — proportional (the old default)
- `fair_prob_power` — power method; redistributes vig, better on longshots
- `fair_prob_shin` — Shin method; models insider money, best for sharp books
- `ev_vs_close_multiplicative_pct` / `_power_pct` / `_shin_pct` — EV vs close under each

**Why:** on h2h favourites the three agree. On longshots and 3-way markets they diverge —
enough to flip a marginal play from +EV to −EV. If an edge only survives under one method,
it's fragile. If it survives all three, it's real. `fair_prob_close` / `ev_vs_close_pct`
still show the multiplicative figures so your existing columns don't move.

### 2. Points-adjusted CLV (spreads/totals)
- `closing_point` — the handicap/total at close
- `line_move_points` — how many points the line moved **in your favour** (positive = good)
- `clv_points_adj_pct` — CLV that accounts for the line move, not just the price
- `fair_prob_close_lineadj` — closing fair prob adjusted for the better number you got

**Why:** if you bet Chiefs −2.5 and it closed −3.5, comparing prices alone is misleading —
you also got a full point of value. This converts that point into probability terms
(using a per-sport points→win% model) so spread/total CLV is finally apples-to-apples.
On h2h these columns are blank (not applicable).

> The points→probability constants (`PTS_PER_PCT` in the script) are approximate,
> sport-level rules of thumb. They're clearly flagged and easy to tune once you have
> your own settled data to calibrate against.

---

## Cost model (quota planning)

- Historical **events** lookup: **1 credit** — cached per (sport, kickoff), so multiple
  bets on the same game pay it once.
- Historical **event-odds** snapshot: **10 credits per region per market per event.**
  This tool uses 1 region (`eu`) × 1 market per bet = **10 credits per bet.**

~300 single-market bets/month ≈ ~3,000 credits + a few hundred for lookups. Check your
plan's monthly quota before a large backfill. The script prints `quota_left` after each bet.

---

## How the closing line is captured

The historical endpoint returns the closest snapshot **≤** the timestamp you ask for.
We ask for the game's true commence time → we get the last snapshot before kickoff = the
closing line. Snapshots are at 5-minute intervals, so the close is at most ~5 min stale.

---

## Next steps (recommended order)

1. **Rotate your API key** if it's ever been exposed, put the new one in `.env`.
2. **Run `test_offline.py`** — confirms the math with no network/credits used.
3. **First live batch of 5–10 real bets**, watched — this is where you confirm team-name
   matching works for *your* leagues and Pinnacle returns in the `eu` region. Eyeball the
   output before trusting it.
4. **Then schedule it** (e.g. a job a few hours after each day's games settle). This only
   works once your bet log is the source of truth the job reads from — which points at the
   real bottleneck: manual bet entry. The higher-leverage next build is bet-slip → row
   capture, so the log feeds this tool automatically.

---

## Alternative data source

If you only ever want **Pinnacle** closing lines + CLV (not your AU soft books in the same
feed), a Pinnacle-only provider with a purpose-built CLV endpoint (pass team names +
kickoff + your price → closing odds + CLV%, fuzzy-matched) would be cheaper and skip the
event-ID step. The CLV math here is isolated from the fetch, so swapping the source later
is a contained change.
