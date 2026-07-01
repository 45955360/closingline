#!/usr/bin/env python3
"""Offline tests for wc_collector — no network, no credits.

Feeds a realistic Pinnacle historical-odds payload (1X2 + Asian handicap + Asian
total goals) through build_record and checks the flat row, devig, and the
JSON<->CSV flatten round-trip.
"""
import json
import os
import tempfile

import wc_collector as wc


def approx(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) < tol


# A stand-in for one historical event-odds snapshot as The Odds API returns it
# for Pinnacle, with all three markets present.
SNAPSHOT = {
    "timestamp": "2026-06-14T18:57:00Z",
    "data": {
        "id": "wc_evt_001",
        "commence_time": "2026-06-14T19:00:00Z",
        "home_team": "Brazil",
        "away_team": "Serbia",
        "bookmakers": [
            {
                "key": "pinnacle",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Brazil", "price": 1.44},
                        {"name": "Serbia", "price": 8.10},
                        {"name": "Draw", "price": 4.55},
                    ]},
                    {"key": "spreads", "outcomes": [
                        {"name": "Brazil", "price": 1.95, "point": -1.0},
                        {"name": "Serbia", "price": 1.95, "point": 1.0},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": 2.02, "point": 2.25},
                        {"name": "Under", "price": 1.88, "point": 2.25},
                    ]},
                ],
            },
            # a non-Pinnacle book that must be ignored
            {"key": "betfair", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Brazil", "price": 1.50},
                    {"name": "Serbia", "price": 7.0},
                    {"name": "Draw", "price": 4.2}]}]},
        ],
    },
}

EVENT = {
    "id": "wc_evt_001",
    "commence_time": "2026-06-14T19:00:00Z",
    "home_team": "Brazil",
    "away_team": "Serbia",
}

MARKETS = ["h2h", "spreads", "totals"]


def test_build_record():
    row, detail = wc.build_record(EVENT, SNAPSHOT, MARKETS, "2026-06-14T20:00:00Z")

    assert row["status"] == "ok", row
    assert row["match_id"] == "wc_evt_001"
    assert row["home_team"] == "Brazil" and row["away_team"] == "Serbia"
    assert row["closing_snapshot_utc"] == "2026-06-14T18:57:00Z"

    # 1X2: only Pinnacle prices used (Brazil 1.44, not Betfair's 1.50).
    assert row["h2h_home_odds"] == 1.44
    assert row["h2h_draw_odds"] == 4.55
    assert row["h2h_away_odds"] == 8.10
    # devigged fair probs sum to ~1 and favourite > draw > dog.
    s = row["h2h_home_fair"] + row["h2h_draw_fair"] + row["h2h_away_fair"]
    assert approx(s, 1.0, 1e-3), s
    assert row["h2h_home_fair"] > row["h2h_draw_fair"] > row["h2h_away_fair"]
    assert row["h2h_overround"] > 1.0

    # Asian handicap: home line -1.0, both prices captured, fair sums ~1.
    assert row["ah_line_home"] == -1.0
    assert row["ah_home_odds"] == 1.95 and row["ah_away_odds"] == 1.95
    assert approx(row["ah_home_fair"] + row["ah_away_fair"], 1.0, 1e-3)

    # Asian total goals: line 2.25, over/under oriented correctly.
    assert row["tot_line"] == 2.25
    assert row["tot_over_odds"] == 2.02 and row["tot_under_odds"] == 1.88
    # Under is cheaper -> Under fair prob should exceed Over.
    assert row["tot_under_fair"] > row["tot_over_fair"]
    assert approx(row["tot_over_fair"] + row["tot_under_fair"], 1.0, 1e-3)

    print("test_build_record OK")
    return detail


def test_flatten_roundtrip(detail):
    # A detail record must flatten back to the same key numbers used in the row.
    row2 = wc._flatten(detail)
    assert row2["h2h_home_odds"] == 1.44
    assert row2["ah_line_home"] == -1.0
    assert row2["tot_line"] == 2.25
    assert row2["h2h_home_fair"] == detail["markets"]["h2h"]["fair_shin"]["Brazil"]
    print("test_flatten_roundtrip OK")


def test_csv_written():
    with tempfile.TemporaryDirectory() as d:
        _row, detail = wc.build_record(EVENT, SNAPSHOT, MARKETS, "2026-06-14T20:00:00Z")
        csv_path = os.path.join(d, "out.csv")
        wc._write_csv(csv_path, [detail])
        with open(csv_path, encoding="utf-8") as f:
            head = f.readline().strip().split(",")
            data = f.readline().strip()
        assert head == wc.ROW_FIELDS, "CSV header must match ROW_FIELDS"
        assert "Brazil" in data and "Serbia" in data
        print("test_csv_written OK")


def test_missing_market_marks_status():
    # Snapshot with no Pinnacle book at all -> no_pinnacle_price.
    empty = {"timestamp": "t", "data": {"bookmakers": [
        {"key": "betfair", "markets": []}]}}
    row, _ = wc.build_record(EVENT, empty, MARKETS, "t")
    assert row["status"] == "no_pinnacle_price", row
    print("test_missing_market_marks_status OK")


if __name__ == "__main__":
    d = test_build_record()
    test_flatten_roundtrip(d)
    test_csv_written()
    test_missing_market_marks_status()
    print("\nALL WC COLLECTOR TESTS PASSED")
