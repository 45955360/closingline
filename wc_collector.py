#!/usr/bin/env python3
"""
wc_collector.py  —  Ongoing Pinnacle CLOSING-LINE collector for the 2026 FIFA World Cup.

WHAT THIS DOES (and how it differs from closing_line.py)
--------------------------------------------------------
closing_line.py enriches a list of *your bets* with CLV. This script needs no bet
list. It walks *every* World Cup match, and for each match that has already kicked
off, captures Pinnacle's CLOSING line for three markets:

  * 1X2               (h2h    — home / draw / away)
  * Asian Handicap    (spreads — Pinnacle soccer spreads ARE Asian handicaps)
  * Asian Total Goals (totals  — Pinnacle soccer totals ARE Asian totals)

Each market is devigged to a fair probability, and one tidy row per match is
appended to a Google Sheet (+ JSON/CSV backups). It is IDEMPOTENT: a match is only
captured once, so re-running never double-charges credits or duplicates rows.

HOW THE CLOSING LINE IS CAPTURED
--------------------------------
The Odds API historical endpoint returns the snapshot closest to (but not after)
the timestamp requested. Asking for a match's true commence_time therefore returns
the last pre-kickoff snapshot = the closing line (at most ~5 min stale).

We only attempt a match once `now >= commence_time` (it has kicked off), so the
closing snapshot exists.

COST MODEL
----------
  * events list (current):          free / does not count against quota
  * historical event-odds snapshot: 10 credits per region per MARKET per event
    -> 3 markets x 1 region (eu) = 30 credits per match, in a single HTTP call.
  ~104 WC matches  ->  ~3,120 credits for the whole tournament. State file ensures
  each match is paid for exactly once.

ENV VARS
--------
  ODDS_API_KEY                 (required)  your The Odds API key
  WC_SPORT_KEY                 (optional)  default soccer_fifa_world_cup
  WC_MARKETS                   (optional)  default "h2h,spreads,totals"
  GOOGLE_SERVICE_ACCOUNT_JSON  (optional)  service-account creds (raw JSON or a path)
  WC_SHEET_ID                  (optional)  target Google Sheet id (enables Sheet write)
  WC_SHEET_TAB                 (optional)  worksheet/tab name, default "closing_lines"
  WC_DATA_DIR                  (optional)  where JSON/CSV/state live, default ./data
  WC_LOOKBACK_HOURS            (optional)  only capture matches kicked off within this
                                           many hours (0 = no limit; default 0)

USAGE
-----
  export ODDS_API_KEY=xxxx
  python wc_collector.py                 # capture any un-captured kicked-off matches
  python wc_collector.py --dry-run       # show what WOULD be captured, spend nothing
  python wc_collector.py --backfill      # ignore lookback, capture every past match
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Reuse the well-tested devig math from the existing tool. Importing is safe:
# closing_line.py guards its CLI behind `if __name__ == "__main__"`.
from closing_line import devig_all, extract_pinnacle_prices

API_BASE = "https://api.the-odds-api.com/v4"
PINNACLE_KEY = "pinnacle"
REGION = "eu"                 # Pinnacle lives in the EU region on this API
ODDS_FORMAT = "decimal"

DEFAULT_SPORT = os.environ.get("WC_SPORT_KEY", "soccer_fifa_world_cup")
DEFAULT_MARKETS = os.environ.get("WC_MARKETS", "h2h,spreads,totals")

# Human labels for the three markets we track.
MARKET_LABELS = {
    "h2h": "1X2",
    "spreads": "Asian Handicap",
    "totals": "Asian Total Goals",
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _get(url):
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=45) as resp:
        body = resp.read().decode("utf-8")
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return json.loads(body), headers


def _quota(headers):
    return headers.get("x-requests-remaining"), headers.get("x-requests-used")


# ---------------------------------------------------------------------------
# The Odds API calls
# ---------------------------------------------------------------------------

def list_events(api_key, sport_key):
    """Current + upcoming events for the sport. Does not count against quota.
    Returns list of {id, home_team, away_team, commence_time}."""
    q = urlencode({"apiKey": api_key})
    url = f"{API_BASE}/sports/{sport_key}/events?{q}"
    data, headers = _get(url)
    return data, headers


def historical_events(api_key, sport_key, date_iso):
    """Events as they appeared at date_iso (fallback id resolver). Cost: 1 credit."""
    q = urlencode({"apiKey": api_key, "date": date_iso})
    url = f"{API_BASE}/historical/sports/{sport_key}/events?{q}"
    return _get(url)


def historical_event_odds(api_key, sport_key, event_id, date_iso, markets):
    """Odds snapshot for one event at/just-before date_iso, all markets in one call.
    Cost: 10 credits per region per market (3 markets -> 30 credits)."""
    q = urlencode({
        "apiKey": api_key,
        "regions": REGION,
        "markets": markets,               # comma-separated
        "oddsFormat": ODDS_FORMAT,
        "bookmakers": PINNACLE_KEY,
        "date": date_iso,
    })
    url = f"{API_BASE}/historical/sports/{sport_key}/events/{event_id}/odds?{q}"
    return _get(url)


# ---------------------------------------------------------------------------
# State (idempotency)
# ---------------------------------------------------------------------------

def load_state(state_path):
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"captured": {}}   # match_id -> captured_at_utc


def save_state(state_path, state):
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, state_path)


# ---------------------------------------------------------------------------
# Devig helper — pick a fair-prob map for a market using Shin (best for a sharp
# book), with multiplicative kept alongside for the JSON detail.
# ---------------------------------------------------------------------------

def devig_market(prices_by_name):
    """prices_by_name: {outcome_name: decimal_odds}. Returns (fair_shin, fair_mult,
    overround) as dicts keyed by outcome name (fair_* are probabilities)."""
    names = list(prices_by_name.keys())
    price_list = [prices_by_name[n] for n in names]
    all_devig, overround = devig_all(price_list)
    fair_shin = dict(zip(names, all_devig["shin"]))
    fair_mult = dict(zip(names, all_devig["multiplicative"]))
    return fair_shin, fair_mult, overround


def _r(x, n=4):
    return round(x, n) if isinstance(x, (int, float)) else x


# ---------------------------------------------------------------------------
# Build one match record from a snapshot payload
# ---------------------------------------------------------------------------

def build_record(event, odds_payload, markets, captured_at):
    """Return (row_dict, detail_dict). row_dict is the flat Google-Sheet row;
    detail_dict is the full nested structure for the JSON backup."""
    snap_ts = odds_payload.get("timestamp")
    home = event.get("home_team", "")
    away = event.get("away_team", "")

    row = {
        "match_id": event.get("id"),
        "commence_time_utc": event.get("commence_time"),
        "home_team": home,
        "away_team": away,
        "closing_snapshot_utc": snap_ts,
        "captured_at_utc": captured_at,
        "status": "ok",
        "note": "",
    }
    detail = {"match_id": event.get("id"), "home_team": home, "away_team": away,
              "commence_time_utc": event.get("commence_time"),
              "closing_snapshot_utc": snap_ts, "captured_at_utc": captured_at,
              "markets": {}}

    found_any = False

    # ---- 1X2 (h2h) ----
    if "h2h" in markets:
        prices, _pts, _ts = extract_pinnacle_prices(odds_payload, "h2h")
        if prices:
            found_any = True
            fair_shin, fair_mult, over = devig_market(prices)
            # Identify home/draw/away by name.
            draw_name = next((n for n in prices if n.lower() == "draw"), None)
            row["h2h_home_odds"] = prices.get(home)
            row["h2h_draw_odds"] = prices.get(draw_name) if draw_name else None
            row["h2h_away_odds"] = prices.get(away)
            row["h2h_home_fair"] = _r(fair_shin.get(home)) if home in fair_shin else None
            row["h2h_draw_fair"] = _r(fair_shin.get(draw_name)) if draw_name else None
            row["h2h_away_fair"] = _r(fair_shin.get(away)) if away in fair_shin else None
            row["h2h_overround"] = _r(over, 5)
            detail["markets"]["h2h"] = {
                "label": MARKET_LABELS["h2h"], "prices": prices,
                "fair_shin": {k: _r(v, 5) for k, v in fair_shin.items()},
                "fair_multiplicative": {k: _r(v, 5) for k, v in fair_mult.items()},
                "overround": _r(over, 5)}

    # ---- Asian handicap (spreads) ----
    if "spreads" in markets:
        prices, pts, _ts = extract_pinnacle_prices(odds_payload, "spreads")
        if prices:
            found_any = True
            fair_shin, fair_mult, over = devig_market(prices)
            row["ah_line_home"] = pts.get(home)
            row["ah_home_odds"] = prices.get(home)
            row["ah_away_odds"] = prices.get(away)
            row["ah_home_fair"] = _r(fair_shin.get(home)) if home in fair_shin else None
            row["ah_away_fair"] = _r(fair_shin.get(away)) if away in fair_shin else None
            row["ah_overround"] = _r(over, 5)
            detail["markets"]["spreads"] = {
                "label": MARKET_LABELS["spreads"], "prices": prices, "points": pts,
                "fair_shin": {k: _r(v, 5) for k, v in fair_shin.items()},
                "fair_multiplicative": {k: _r(v, 5) for k, v in fair_mult.items()},
                "overround": _r(over, 5)}

    # ---- Asian total goals (totals) ----
    if "totals" in markets:
        prices, pts, _ts = extract_pinnacle_prices(odds_payload, "totals")
        if prices:
            found_any = True
            fair_shin, fair_mult, over = devig_market(prices)
            over_name = next((n for n in prices if n.lower().startswith("over")), None)
            under_name = next((n for n in prices if n.lower().startswith("under")), None)
            row["tot_line"] = pts.get(over_name) if over_name else None
            row["tot_over_odds"] = prices.get(over_name) if over_name else None
            row["tot_under_odds"] = prices.get(under_name) if under_name else None
            row["tot_over_fair"] = _r(fair_shin.get(over_name)) if over_name else None
            row["tot_under_fair"] = _r(fair_shin.get(under_name)) if under_name else None
            row["tot_overround"] = _r(over, 5)
            detail["markets"]["totals"] = {
                "label": MARKET_LABELS["totals"], "prices": prices, "points": pts,
                "fair_shin": {k: _r(v, 5) for k, v in fair_shin.items()},
                "fair_multiplicative": {k: _r(v, 5) for k, v in fair_mult.items()},
                "overround": _r(over, 5)}

    if not found_any:
        row["status"] = "no_pinnacle_price"
        row["note"] = "no Pinnacle price in closing snapshot for requested markets"
    return row, detail


# The flat column order for CSV / Google Sheet.
ROW_FIELDS = [
    "match_id", "commence_time_utc", "home_team", "away_team",
    "closing_snapshot_utc", "captured_at_utc",
    "h2h_home_odds", "h2h_draw_odds", "h2h_away_odds",
    "h2h_home_fair", "h2h_draw_fair", "h2h_away_fair", "h2h_overround",
    "ah_line_home", "ah_home_odds", "ah_away_odds", "ah_home_fair", "ah_away_fair", "ah_overround",
    "tot_line", "tot_over_odds", "tot_under_odds", "tot_over_fair", "tot_under_fair", "tot_overround",
    "status", "note",
]


# ---------------------------------------------------------------------------
# Main collection routine
# ---------------------------------------------------------------------------

def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect(api_key, sport_key, markets, data_dir, lookback_hours=0,
            dry_run=False, backfill=False, sleep_s=0.4, sheet_writer=None):
    os.makedirs(data_dir, exist_ok=True)
    state_path = os.path.join(data_dir, "captured_state.json")
    json_path = os.path.join(data_dir, "world_cup_closing_lines.json")
    csv_path = os.path.join(data_dir, "world_cup_closing_lines.csv")

    state = load_state(state_path)
    captured = state.setdefault("captured", {})

    # Load prior detail records so we append rather than overwrite.
    records = []
    if os.path.exists(json_path):
        try:
            with open(json_path, encoding="utf-8") as f:
                records = json.load(f)
        except (json.JSONDecodeError, OSError):
            records = []

    try:
        events, hdr = list_events(api_key, sport_key)
    except (HTTPError, URLError) as e:
        print(f"ERROR listing events: {e}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    market_str = ",".join(markets)

    # Decide which matches to capture: kicked off, not yet captured, within lookback.
    to_capture = []
    for ev in events:
        eid = ev.get("id")
        ct = parse_iso(ev.get("commence_time"))
        if not eid or ct is None:
            continue
        if ct > now:
            continue                                   # not kicked off yet
        if eid in captured:
            continue                                   # already have it
        if lookback_hours and not backfill:
            age_h = (now - ct).total_seconds() / 3600.0
            if age_h > lookback_hours:
                continue
        to_capture.append(ev)

    print(f"{sport_key}: {len(events)} events listed, "
          f"{len(to_capture)} to capture "
          f"({'DRY RUN' if dry_run else 'live'}).", file=sys.stderr)

    if dry_run:
        for ev in to_capture:
            print(f"  WOULD capture {ev.get('commence_time')}  "
                  f"{ev.get('home_team')} v {ev.get('away_team')}  [{ev.get('id')}]",
                  file=sys.stderr)
        return 0

    new_rows = []
    quota_left = None
    for i, ev in enumerate(to_capture, 1):
        eid = ev["id"]
        ct_iso = ev["commence_time"]
        try:
            odds_payload, hdr = historical_event_odds(
                api_key, sport_key, eid, ct_iso, market_str)
            quota_left, _ = _quota(hdr)
        except (HTTPError, URLError) as e:
            print(f"  [{i}/{len(to_capture)}] {eid}: odds fetch failed: {e}",
                  file=sys.stderr)
            continue

        captured_at = utcnow_iso()
        row, detail = build_record(ev, odds_payload, markets, captured_at)
        new_rows.append(row)
        records.append(detail)
        if row["status"] == "ok":
            captured[eid] = captured_at            # only mark captured on success
        print(f"  [{i}/{len(to_capture)}] {ev.get('home_team')} v {ev.get('away_team')}"
              f"  status={row['status']}  1X2={row.get('h2h_home_odds')}/"
              f"{row.get('h2h_draw_odds')}/{row.get('h2h_away_odds')}"
              f"  quota_left={quota_left}", file=sys.stderr)
        time.sleep(sleep_s)

    # Persist JSON + CSV + state.
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
    _write_csv(csv_path, records)
    save_state(state_path, state)

    # Google Sheet append (idempotent inside the writer).
    if sheet_writer is not None and new_rows:
        try:
            added = sheet_writer(new_rows)
            print(f"Google Sheet: appended {added} new row(s).", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — never let Sheets break the run
            print(f"Google Sheet write failed (data still saved to JSON/CSV): {e}",
                  file=sys.stderr)

    print(f"\nDone. {len(new_rows)} match(es) captured this run. "
          f"Total matches on file: {len(records)}. quota_left={quota_left}",
          file=sys.stderr)
    return 0


def _flatten(detail):
    """Turn a nested detail record back into the flat ROW_FIELDS shape."""
    row = {k: detail.get(k) for k in
           ("match_id", "commence_time_utc", "home_team", "away_team",
            "closing_snapshot_utc", "captured_at_utc")}
    home, away = detail.get("home_team", ""), detail.get("away_team", "")
    mk = detail.get("markets", {})
    if "h2h" in mk:
        p = mk["h2h"]["prices"]; fs = mk["h2h"]["fair_shin"]
        dn = next((n for n in p if n.lower() == "draw"), None)
        row.update({"h2h_home_odds": p.get(home), "h2h_draw_odds": p.get(dn),
                    "h2h_away_odds": p.get(away), "h2h_home_fair": fs.get(home),
                    "h2h_draw_fair": fs.get(dn), "h2h_away_fair": fs.get(away),
                    "h2h_overround": mk["h2h"]["overround"]})
    if "spreads" in mk:
        p = mk["spreads"]["prices"]; fs = mk["spreads"]["fair_shin"]; pts = mk["spreads"]["points"]
        row.update({"ah_line_home": pts.get(home), "ah_home_odds": p.get(home),
                    "ah_away_odds": p.get(away), "ah_home_fair": fs.get(home),
                    "ah_away_fair": fs.get(away), "ah_overround": mk["spreads"]["overround"]})
    if "totals" in mk:
        p = mk["totals"]["prices"]; fs = mk["totals"]["fair_shin"]; pts = mk["totals"]["points"]
        on = next((n for n in p if n.lower().startswith("over")), None)
        un = next((n for n in p if n.lower().startswith("under")), None)
        row.update({"tot_line": pts.get(on), "tot_over_odds": p.get(on),
                    "tot_under_odds": p.get(un), "tot_over_fair": fs.get(on),
                    "tot_under_fair": fs.get(un), "tot_overround": mk["totals"]["overround"]})
    row.setdefault("status", "ok"); row.setdefault("note", "")
    return row


def _write_csv(csv_path, records):
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ROW_FIELDS, extrasaction="ignore")
        w.writeheader()
        for d in records:
            w.writerow(_flatten(d))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Collect Pinnacle World Cup closing lines into a Google Sheet / JSON.")
    ap.add_argument("--sport", default=DEFAULT_SPORT)
    ap.add_argument("--markets", default=DEFAULT_MARKETS,
                    help="comma-separated: h2h,spreads,totals")
    ap.add_argument("--data-dir", default=os.environ.get("WC_DATA_DIR", "data"))
    ap.add_argument("--lookback-hours", type=float,
                    default=float(os.environ.get("WC_LOOKBACK_HOURS", "0") or 0))
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be captured; spend no credits")
    ap.add_argument("--backfill", action="store_true",
                    help="ignore lookback; capture every past un-captured match")
    ap.add_argument("--sleep", type=float, default=0.4)
    args = ap.parse_args()

    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        print("ERROR: set ODDS_API_KEY.", file=sys.stderr)
        sys.exit(1)

    markets = [m.strip() for m in args.markets.split(",") if m.strip()]

    # Optional Google Sheet writer.
    sheet_writer = None
    sheet_id = os.environ.get("WC_SHEET_ID")
    if sheet_id:
        try:
            from sheets import make_sheet_writer
            sheet_writer = make_sheet_writer(
                sheet_id,
                os.environ.get("WC_SHEET_TAB", "closing_lines"),
                ROW_FIELDS,
            )
        except Exception as e:  # noqa: BLE001
            print(f"Google Sheet disabled ({e}); writing JSON/CSV only.", file=sys.stderr)

    rc = collect(
        api_key, args.sport, markets, args.data_dir,
        lookback_hours=args.lookback_hours, dry_run=args.dry_run,
        backfill=args.backfill, sleep_s=args.sleep, sheet_writer=sheet_writer,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
