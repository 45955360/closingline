#!/usr/bin/env python3
"""
closing_line.py  —  Automated Pinnacle closing-line + CLV capture via The Odds API.

WHAT THIS DOES
--------------
Reads a list of your open/settled bets, and for each one:
  1. Resolves the historical event ID + true commence time (as it appeared at bet time).
  2. Pulls the Pinnacle odds snapshot closest to kickoff  ->  the CLOSING LINE.
  3. Devigs the closing 2-way/3-way market to a fair probability.
  4. Computes CLV vs the price you actually took.
  5. Writes an enriched row back out (CSV), ready to paste into your tracker.

WHY IT'S BUILT THIS WAY
-----------------------
- The Odds API historical odds returns "the closest snapshot equal to or earlier
  than the provided date". Querying at commence_time therefore yields the last
  pre-game snapshot = the closing line.
- Pinnacle lives in the `eu` region for this API.
- Event-odds cost = 10 credits / region / market / event. Events lookup = 1 credit.
  We cache the events lookup per (sport, day) to avoid paying it per bet.

This script NEVER touches any bookmaker account. It only reads odds data.
It does not place, modify, or settle bets anywhere.

USAGE
-----
  export ODDS_API_KEY=xxxxxxxx
  python closing_line.py --in open_bets.csv --out enriched_bets.csv

INPUT CSV COLUMNS (header row required)
--------------------------------------
  bet_id, sport_key, home_team, away_team, commence_time_iso,
  market, selection, odds_taken
    - sport_key: The Odds API key e.g. soccer_epl, americanfootball_nfl, basketball_nba
    - market:    h2h | spreads | totals
    - selection: the exact outcome name you backed (team name, or Over/Under)
    - commence_time_iso: scheduled kickoff, ISO8601 e.g. 2026-06-14T19:00:00Z
    - odds_taken: decimal odds you got

See make_sample_input() below for a generated example.
"""

import argparse
import csv
import os
import sys
import time
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_BASE = "https://api.the-odds-api.com/v4"
PINNACLE_KEY = "pinnacle"
REGION = "eu"          # Pinnacle is in the EU region
ODDS_FORMAT = "decimal"

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(url):
    """GET returning (parsed_json, headers_dict). Raises on non-200."""
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return json.loads(body), headers


def _quota(headers):
    return (headers.get("x-requests-remaining"), headers.get("x-requests-used"),
            headers.get("x-requests-last"))


# ---------------------------------------------------------------------------
# The Odds API calls
# ---------------------------------------------------------------------------

def historical_events(api_key, sport_key, date_iso):
    """List events as they appeared at `date_iso`. Cost: 1 credit."""
    q = urlencode({"apiKey": api_key, "date": date_iso})
    url = f"{API_BASE}/historical/sports/{sport_key}/events?{q}"
    data, headers = _get(url)
    return data, headers


def historical_event_odds(api_key, sport_key, event_id, date_iso, market):
    """Odds snapshot for one event at/just-before `date_iso`.
    Cost: 10 credits per region per market (here 1 region x 1 market = 10)."""
    q = urlencode({
        "apiKey": api_key,
        "regions": REGION,
        "markets": market,
        "oddsFormat": ODDS_FORMAT,
        "bookmakers": PINNACLE_KEY,
        "date": date_iso,
    })
    url = f"{API_BASE}/historical/sports/{sport_key}/events/{event_id}/odds?{q}"
    data, headers = _get(url)
    return data, headers


# ---------------------------------------------------------------------------
# Event matching
# ---------------------------------------------------------------------------

def find_event_id(events_payload, home_team, away_team):
    """Match our bet's teams to an event id from the historical events response.
    Uses a forgiving contains-match on normalised names."""
    def norm(s):
        return "".join(c.lower() for c in s if c.isalnum())

    h, a = norm(home_team), norm(away_team)
    data = events_payload.get("data", events_payload)  # events endpoint wraps in 'data'
    best = None
    for ev in data:
        eh, ea = norm(ev.get("home_team", "")), norm(ev.get("away_team", ""))
        # exact-ish both sides
        if (h in eh or eh in h) and (a in ea or ea in a):
            return ev
        # fall back: either side strongly matches
        if best is None and ((h in eh or eh in h) or (a in ea or ea in a)):
            best = ev
    return best


# ---------------------------------------------------------------------------
# Devig + CLV math
# ---------------------------------------------------------------------------

def devig_multiplicative(decimal_odds):
    """Proportional devig: scale all implied probs so they sum to 1.
    Simple, standard default. Over-taxes favourites vs longshots."""
    implied = [1.0 / o for o in decimal_odds]
    overround = sum(implied)
    return [p / overround for p in implied], overround


def devig_power(decimal_odds, tol=1e-10, max_iter=100):
    """Power devig: find exponent k such that sum(implied_i ** k) == 1.
    Redistributes margin more realistically across favourites/longshots.
    Solved by bisection on k (k > 1 shrinks, k < 1 grows the total)."""
    implied = [1.0 / o for o in decimal_odds]
    overround = sum(implied)

    def total(k):
        return sum(p ** k for p in implied)

    # overround > 1, so we need k > 1 to bring the sum down to 1.
    lo, hi = 1.0, 1.0
    # expand hi until total(hi) <= 1
    while total(hi) > 1.0 and hi < 100:
        hi *= 2.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        t = total(mid)
        if abs(t - 1.0) < tol:
            break
        if t > 1.0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    fair = [p ** k for p in implied]
    s = sum(fair)
    fair = [f / s for f in fair]  # normalise off any residual
    return fair, overround


def devig_shin(decimal_odds, tol=1e-12, max_iter=200):
    """Shin devig: models the vig as arising from a proportion z of insider money.
    Generally regarded as the most accurate for sharp books. Solves for z, then
    backs out the fair probabilities. Falls back to multiplicative if it fails to
    converge (can happen on near-zero margins)."""
    implied = [1.0 / o for o in decimal_odds]
    overround = sum(implied)
    n = len(implied)
    pi = [p / overround for p in implied]  # normalised starting point

    def shin_probs(z):
        # Shin's formula for each outcome given z and booksum (overround)
        out = []
        for p in implied:
            # standard Shin recovery: q_i = ( sqrt(z^2 + 4(1-z)*p_i^2/booksum) - z ) / (2(1-z))
            num = (z * z + 4.0 * (1.0 - z) * (p * p) / overround) ** 0.5 - z
            den = 2.0 * (1.0 - z)
            out.append(num / den)
        return out

    lo, hi = 0.0, 0.5
    try:
        for _ in range(max_iter):
            z = (lo + hi) / 2.0
            q = shin_probs(z)
            s = sum(q)
            if abs(s - 1.0) < tol:
                break
            # larger z lowers the sum
            if s > 1.0:
                lo = z
            else:
                hi = z
        q = shin_probs((lo + hi) / 2.0)
        sq = sum(q)
        q = [x / sq for x in q]
        return q, overround
    except (ValueError, ZeroDivisionError):
        return [p / overround for p in implied], overround


DEVIG_METHODS = {
    "multiplicative": devig_multiplicative,
    "power": devig_power,
    "shin": devig_shin,
}


def devig_all(decimal_odds):
    """Run all three methods, return {method: fair_probs_list} and the shared overround."""
    result = {}
    overround = None
    for name, fn in DEVIG_METHODS.items():
        probs, overround = fn(decimal_odds)
        result[name] = probs
    return result, overround


def extract_pinnacle_prices(odds_payload, market):
    """From an event-odds snapshot, return {outcome_name: decimal_price} for Pinnacle + the market.
    Also returns the snapshot timestamp actually used."""
    snap_ts = odds_payload.get("timestamp")
    data = odds_payload.get("data", odds_payload)
    prices = {}
    point_by_outcome = {}
    for bm in data.get("bookmakers", []):
        if bm.get("key") != PINNACLE_KEY:
            continue
        for mk in bm.get("markets", []):
            if mk.get("key") != market:
                continue
            for oc in mk.get("outcomes", []):
                name = oc.get("name")
                prices[name] = oc.get("price")
                if oc.get("point") is not None:
                    point_by_outcome[name] = oc.get("point")
    return prices, point_by_outcome, snap_ts


# ---------------------------------------------------------------------------
# Points -> probability model (for spreads/totals line-move adjustment)
# ---------------------------------------------------------------------------
# Approximate "points of line movement per 1% win-probability shift near the
# margin". These are rough, sport-level constants drawn from standard
# handicapping rules of thumb; they let us convert a half-point line move into a
# probability delta so CLV on a spread/total is comparable even when the line
# moved. They are intentionally conservative and clearly flagged as approximate.
#
# Interpretation: PTS_PER_PCT[sport] = how many points of line ≈ 1 percentage
# point of win probability, near a pick'em / central total. Bigger number = a
# point is worth less probability (e.g. high-scoring sports).
PTS_PER_PCT = {
    "americanfootball_nfl": 0.20,   # ~ a point near a key number moves win% a lot
    "americanfootball_ncaaf": 0.22,
    "basketball_nba": 0.30,         # points cheap, high scoring
    "basketball_ncaab": 0.28,
    "baseball_mlb": 0.10,           # run line, low scoring — a run is huge
    "icehockey_nhl": 0.09,          # puck line similar
    "soccer": 0.12,                 # goals scarce
}


def _pts_per_pct(sport_key):
    if sport_key in PTS_PER_PCT:
        return PTS_PER_PCT[sport_key]
    # soccer_* family
    if sport_key.startswith("soccer"):
        return PTS_PER_PCT["soccer"]
    return 0.20  # generic fallback


def points_move_to_prob_delta(sport_key, point_move):
    """Convert a favourable line move (in points) into an approximate win-prob
    gain (as a fraction, e.g. 0.03 = +3%). `point_move` should be POSITIVE when
    the line moved in your favour (you got a better number than the close)."""
    ppp = _pts_per_pct(sport_key)
    if ppp <= 0:
        return 0.0
    return (point_move / ppp) / 100.0


def compute_clv(odds_taken, closing_price, fair_prob_taken_side,
                sport_key=None, market="h2h",
                point_taken=None, point_closed=None, selection=None):
    """CLV metrics.

    For h2h: price CLV + EV vs devigged close (unchanged).
    For spreads/totals: additionally computes a POINTS-ADJUSTED CLV that accounts
    for the line having moved between your bet and the close.

    point_taken  : the handicap/total you actually bet (from your input CSV)
    point_closed : the handicap/total at close (from the snapshot)
    selection    : used to orient direction for totals (Over vs Under)
    """
    out = {}
    # --- price CLV (always) ---
    if closing_price and closing_price > 0:
        out["clv_price_pct"] = round((odds_taken / closing_price - 1.0) * 100, 3)
    else:
        out["clv_price_pct"] = None

    # --- EV vs devigged close (always) ---
    if fair_prob_taken_side and fair_prob_taken_side > 0:
        out["ev_vs_close_pct"] = round((fair_prob_taken_side * odds_taken - 1.0) * 100, 3)
        out["fair_prob_close"] = round(fair_prob_taken_side, 5)
        out["fair_odds_close"] = round(1.0 / fair_prob_taken_side, 4)
    else:
        out["ev_vs_close_pct"] = None
        out["fair_prob_close"] = None
        out["fair_odds_close"] = None

    # --- points-adjusted CLV (spreads/totals only, when both points known) ---
    out["clv_points_adj_pct"] = None
    out["line_move_points"] = None
    if market in ("spreads", "totals") and point_taken is not None and point_closed is not None:
        try:
            pt_taken = float(point_taken)
            pt_closed = float(point_closed)
        except (TypeError, ValueError):
            pt_taken = pt_closed = None

        if pt_taken is not None and pt_closed is not None:
            # Determine how many points in YOUR FAVOUR the line moved.
            # spreads: your number is better if it's more positive for your side.
            #   e.g. you took +3.5, closed +2.5 -> you got 1 extra point (favourable).
            #   you took -5.5, closed -6.5 -> closing line is tougher, you were +1 favourable.
            # We express favourable move as (pt_taken - pt_closed) for the side as quoted,
            # but totals need orientation by Over/Under.
            if market == "spreads":
                favourable_move = pt_taken - pt_closed
            else:  # totals
                sel = (selection or "").strip().lower()
                if sel.startswith("over"):
                    # Over is better with a LOWER total -> favourable if closed < taken
                    favourable_move = pt_taken - pt_closed
                elif sel.startswith("under"):
                    # Under is better with a HIGHER total -> favourable if closed > taken
                    favourable_move = pt_closed - pt_taken
                else:
                    favourable_move = 0.0

            out["line_move_points"] = round(favourable_move, 2)
            prob_delta = points_move_to_prob_delta(sport_key, favourable_move)

            # Combine: total CLV in prob terms ≈ (price effect) + (points effect).
            # Price effect in prob terms: convert both prices to fair-ish probs.
            # We approximate price CLV in probability by comparing 1/odds_taken vs
            # devigged fair prob at close, then add the points-driven prob delta.
            if fair_prob_taken_side:
                base_prob = fair_prob_taken_side           # devigged closing prob at CLOSING line
                adj_prob = base_prob + prob_delta           # what your better line is worth
                adj_prob = min(max(adj_prob, 1e-6), 1 - 1e-6)
                # EV if that adjusted prob is the truth and you bet at odds_taken
                out["clv_points_adj_pct"] = round((adj_prob * odds_taken - 1.0) * 100, 3)
                out["fair_prob_close_lineadj"] = round(adj_prob, 5)

    return out


# ---------------------------------------------------------------------------
# Per-bet processing
# ---------------------------------------------------------------------------

def process_bet(api_key, bet, events_cache):
    """Enrich a single bet dict with closing line + CLV. Returns enriched dict."""
    result = dict(bet)
    result["_status"] = "ok"
    result["_note"] = ""

    sport = bet["sport_key"].strip()
    commence = bet["commence_time_iso"].strip()
    market = bet["market"].strip().lower()
    selection = bet["selection"].strip()
    try:
        odds_taken = float(bet["odds_taken"])
    except (ValueError, KeyError):
        result["_status"] = "error"
        result["_note"] = "bad odds_taken"
        return result

    # 1) resolve event id (cache events lookups per sport+commence timestamp)
    cache_key = f"{sport}|{commence}"
    if cache_key not in events_cache:
        try:
            ev_payload, hdr = historical_events(api_key, sport, commence)
            events_cache[cache_key] = ev_payload
            result["_quota_remaining"] = _quota(hdr)[0]
        except (HTTPError, URLError) as e:
            result["_status"] = "error"
            result["_note"] = f"events lookup failed: {e}"
            return result
    ev_payload = events_cache[cache_key]

    ev = find_event_id(ev_payload, bet["home_team"], bet["away_team"])
    if not ev:
        result["_status"] = "no_event_match"
        result["_note"] = "could not match teams to an event id"
        return result

    event_id = ev.get("id")
    true_commence = ev.get("commence_time", commence)
    result["matched_event_id"] = event_id
    result["true_commence_time"] = true_commence

    # 2) pull closing snapshot (query AT commence -> closest earlier snapshot = closing line)
    try:
        odds_payload, hdr = historical_event_odds(api_key, sport, event_id, true_commence, market)
        result["_quota_remaining"] = _quota(hdr)[0]
    except (HTTPError, URLError) as e:
        result["_status"] = "error"
        result["_note"] = f"odds snapshot failed: {e}"
        return result

    prices, points, snap_ts = extract_pinnacle_prices(odds_payload, market)
    result["closing_snapshot_ts"] = snap_ts
    if not prices:
        result["_status"] = "no_pinnacle_price"
        result["_note"] = "no pinnacle price in closing snapshot"
        return result

    # 3) devig the closing market with ALL THREE methods
    names = list(prices.keys())
    price_list = [prices[n] for n in names]
    all_devig, overround = devig_all(price_list)
    # per-method probability by outcome name
    fair_by_method = {m: dict(zip(names, probs)) for m, probs in all_devig.items()}

    # 4) match our selection to a closing outcome name
    def norm(s):
        return "".join(c.lower() for c in s if c.isalnum())
    sel_n = norm(selection)
    matched_name = None
    for n in names:
        if norm(n) == sel_n or sel_n in norm(n) or norm(n) in sel_n:
            matched_name = n
            break

    if matched_name is None:
        result["_status"] = "no_selection_match"
        result["_note"] = f"selection '{selection}' not in {names}"
        result["closing_market_overround"] = round(overround, 5)
        return result

    closing_price = prices[matched_name]
    # primary fair prob = multiplicative (keeps existing columns stable);
    # power & shin exposed as extra columns for method-robustness checks.
    fair_prob = fair_by_method["multiplicative"][matched_name]

    # closing point on our side (for line-move adjustment)
    point_closed = points.get(matched_name)
    point_taken = bet.get("point_taken", "").strip() if bet.get("point_taken") else None

    clv = compute_clv(
        odds_taken, closing_price, fair_prob,
        sport_key=sport, market=market,
        point_taken=point_taken, point_closed=point_closed,
        selection=selection,
    )
    result["closing_price_pinnacle"] = closing_price
    result["closing_market_overround"] = round(overround, 5)
    result.update(clv)
    if point_closed is not None:
        result["closing_point"] = point_closed

    # expose the three devig methods for the taken side
    result["fair_prob_multiplicative"] = round(fair_by_method["multiplicative"][matched_name], 5)
    result["fair_prob_power"] = round(fair_by_method["power"][matched_name], 5)
    result["fair_prob_shin"] = round(fair_by_method["shin"][matched_name], 5)
    # EV vs close under each method (does the edge survive the method choice?)
    result["ev_vs_close_multiplicative_pct"] = round(
        (fair_by_method["multiplicative"][matched_name] * odds_taken - 1.0) * 100, 3)
    result["ev_vs_close_power_pct"] = round(
        (fair_by_method["power"][matched_name] * odds_taken - 1.0) * 100, 3)
    result["ev_vs_close_shin_pct"] = round(
        (fair_by_method["shin"][matched_name] * odds_taken - 1.0) * 100, 3)

    return result


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

OUTPUT_FIELDS = [
    "bet_id", "sport_key", "home_team", "away_team", "commence_time_iso",
    "market", "selection", "point_taken", "odds_taken",
    "matched_event_id", "true_commence_time", "closing_snapshot_ts",
    "closing_price_pinnacle", "closing_point", "closing_market_overround",
    # primary (multiplicative) fair line + headline CLV
    "fair_prob_close", "fair_odds_close",
    "clv_price_pct", "ev_vs_close_pct",
    # points-adjusted CLV (spreads/totals)
    "line_move_points", "clv_points_adj_pct", "fair_prob_close_lineadj",
    # method-robustness: devigged prob + EV vs close under each method
    "fair_prob_multiplicative", "fair_prob_power", "fair_prob_shin",
    "ev_vs_close_multiplicative_pct", "ev_vs_close_power_pct", "ev_vs_close_shin_pct",
    # status
    "_status", "_note", "_quota_remaining",
]


def run(in_path, out_path, api_key, sleep_s=0.3):
    with open(in_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    events_cache = {}
    enriched = []
    for i, bet in enumerate(rows, 1):
        r = process_bet(api_key, bet, events_cache)
        enriched.append(r)
        print(f"[{i}/{len(rows)}] {bet.get('bet_id','?')}: {r['_status']} "
              f"| close={r.get('closing_price_pinnacle','-')} "
              f"| CLV%={r.get('clv_price_pct','-')} "
              f"| EVvsClose%={r.get('ev_vs_close_pct','-')} "
              f"| quota_left={r.get('_quota_remaining','-')}", file=sys.stderr)
        time.sleep(sleep_s)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in enriched:
            w.writerow(r)

    ok = sum(1 for r in enriched if r["_status"] == "ok")
    print(f"\nDone. {ok}/{len(enriched)} bets fully enriched. Output -> {out_path}",
          file=sys.stderr)


def make_sample_input(path):
    sample = [
        ["bet_id", "sport_key", "home_team", "away_team", "commence_time_iso",
         "market", "selection", "point_taken", "odds_taken"],
        # h2h: no point_taken needed
        ["001", "soccer_epl", "Arsenal", "Chelsea", "2026-05-10T15:00:00Z",
         "h2h", "Arsenal", "", "2.10"],
        ["002", "basketball_nba", "Boston Celtics", "Miami Heat",
         "2026-05-11T23:30:00Z", "h2h", "Boston Celtics", "", "1.55"],
        # spreads: point_taken is the handicap you got
        ["003", "americanfootball_nfl", "Kansas City Chiefs", "Buffalo Bills",
         "2026-09-14T17:00:00Z", "spreads", "Kansas City Chiefs", "-2.5", "1.91"],
        # totals: point_taken is the total, selection is Over/Under
        ["004", "basketball_nba", "Denver Nuggets", "Phoenix Suns",
         "2026-05-12T02:00:00Z", "totals", "Over", "228.5", "1.95"],
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(sample)
    print(f"Wrote sample input -> {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Capture Pinnacle closing lines + CLV via The Odds API.")
    ap.add_argument("--in", dest="in_path", help="input bets CSV")
    ap.add_argument("--out", dest="out_path", default="enriched_bets.csv")
    ap.add_argument("--make-sample", action="store_true", help="write sample_input.csv and exit")
    ap.add_argument("--sleep", type=float, default=0.3, help="seconds between bets (rate-limit safety)")
    args = ap.parse_args()

    if args.make_sample:
        make_sample_input("sample_input.csv")
        sys.exit(0)

    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        print("ERROR: set ODDS_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)
    if not args.in_path:
        print("ERROR: --in required (or use --make-sample).", file=sys.stderr)
        sys.exit(1)

    run(args.in_path, args.out_path, api_key, sleep_s=args.sleep)
