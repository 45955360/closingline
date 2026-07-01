#!/usr/bin/env python3
"""Offline tests for devig methods (multiplicative/power/shin) + points-adjusted CLV."""
import closing_line as cl

def approx(a, b, tol=1e-6):
    return abs(a - b) < tol

# ---- 1. all three devig methods sum to 1 ----
odds_2way = [1.95, 4.20, 3.75]
for method, fn in cl.DEVIG_METHODS.items():
    probs, over = fn(odds_2way)
    assert approx(sum(probs), 1.0), f"{method} sum {sum(probs)}"
    assert over > 1.0
    print(f"{method:14s} 3-way ->", [round(p, 4) for p in probs], "sum", round(sum(probs), 6))

# ---- 2. methods diverge on longshots ----
longshot = [1.10, 9.0]
d, over = cl.devig_all(longshot)
print("\nlongshot 1.10/9.0, overround", round(over, 4))
for m, p in d.items():
    print(f"  {m:14s} fav={p[0]:.4f} dog={p[1]:.4f}")
assert not approx(d["multiplicative"][1], d["shin"][1], tol=1e-4), "shin should differ on longshots"
print("  -> methods diverge, as expected")

# ---- 3. h2h CLV regression ----
prices = {"Arsenal": 1.95, "Chelsea": 4.20, "Draw": 3.75}
names = list(prices.keys())
d, over = cl.devig_all([prices[n] for n in names])
fair = dict(zip(names, d["multiplicative"]))
clv = cl.compute_clv(2.10, 1.95, fair["Arsenal"], sport_key="soccer_epl", market="h2h")
print("\nh2h CLV:", {k: clv[k] for k in ("clv_price_pct", "ev_vs_close_pct")})
assert clv["clv_price_pct"] > 0
assert clv["clv_points_adj_pct"] is None

# ---- 4. spreads favourable: took -2.5, closed -3.5 ----
clv_sp = cl.compute_clv(1.91, 1.91, 0.50, sport_key="americanfootball_nfl",
    market="spreads", point_taken="-2.5", point_closed="-3.5", selection="Kansas City Chiefs")
print("\nspreads (took -2.5, closed -3.5): move", clv_sp["line_move_points"],
      "adj", clv_sp["clv_points_adj_pct"])
assert clv_sp["line_move_points"] == 1.0
assert clv_sp["clv_points_adj_pct"] > 0

# ---- 5. spreads unfavourable ----
clv_sp2 = cl.compute_clv(1.91, 1.91, 0.50, sport_key="americanfootball_nfl",
    market="spreads", point_taken="-3.5", point_closed="-2.5", selection="Kansas City Chiefs")
print("spreads (took -3.5, closed -2.5): move", clv_sp2["line_move_points"],
      "adj", clv_sp2["clv_points_adj_pct"])
assert clv_sp2["line_move_points"] == -1.0
assert clv_sp2["clv_points_adj_pct"] < 0

# ---- 6. totals orientation ----
clv_over = cl.compute_clv(1.95, 1.95, 0.50, sport_key="basketball_nba",
    market="totals", point_taken="228.5", point_closed="230.5", selection="Over")
print("\ntotals Over (228.5 -> 230.5): move", clv_over["line_move_points"],
      "adj", clv_over["clv_points_adj_pct"])
assert clv_over["line_move_points"] == -2.0
assert clv_over["clv_points_adj_pct"] < 0

clv_under = cl.compute_clv(1.95, 1.95, 0.50, sport_key="basketball_nba",
    market="totals", point_taken="228.5", point_closed="230.5", selection="Under")
print("totals Under (228.5 -> 230.5): move", clv_under["line_move_points"],
      "adj", clv_under["clv_points_adj_pct"])
assert clv_under["line_move_points"] == 2.0
assert clv_under["clv_points_adj_pct"] > 0

print("\nALL TESTS PASSED")
