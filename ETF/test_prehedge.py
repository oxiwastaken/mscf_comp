"""Replay every tender the net limit actually refused, and check the pre-hedge.

Not a synthetic scenario: this reads `logs/tenders*.csv` for rows whose
`safe_reason` starts with "net would hit", pairs each with the position from
`logs/ticks*.csv` at that tick, and runs the real `net_shortfall` /
`prehedge_for_tender` / `unwind_prehedge` against it.

Two regimes, because the logs predate v11 and the pre-hedge is aimed at the
world v11 creates:

  AS LOGGED - the book as it actually stood. Every one of these rejections has
  the arb's BULL/BEAR in it (ritc sits at the +/-2,000 unwind residual in all
  of them), so the shortfall is dominated by a position v11 no longer allows to
  accumulate. Reported as a statistic, not asserted on.

  V11-CLEAN - the same tenders against a book carrying only the unwind residual,
  which is what the arb looks like once it stops having its ETF hedge leg eaten.
  This is the regime the pre-hedge is designed for, and it IS asserted on: every
  tender that is too large for the net cap on a flat book must be reachable, and
  reachable cheaply.

    python test_prehedge.py
"""

import csv
import glob
import sys
from collections import defaultdict

import etf_arb_bot as bot
import sim_unwind as sim


def load_blocked():
    """Every distinct tender the net check refused, with the book at that tick."""
    ticks = defaultdict(dict)
    for path in sorted(glob.glob("logs/ticks*.csv")):
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    ticks[(r["run_id"], r["heat"])][int(r["tick"])] = r
                except (ValueError, KeyError):
                    continue
    out, seen = [], set()
    for path in sorted(glob.glob("logs/tenders*.csv")):
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                if not (r.get("safe_reason") or "").startswith("net would hit"):
                    continue
                key = (r["run_id"], r["heat"], r["tender_id"])
                if key in seen:
                    continue
                t = ticks.get((r["run_id"], r["heat"]), {}).get(int(r["tick"]))
                if not t:
                    continue
                seen.add(key)
                out.append((r, t))
    return out


def as_offer(r):
    return {"tender_id": r["tender_id"], "ticker": "RITC", "action": r["action"],
            "quantity": float(r["quantity"]), "price": float(r["price"]),
            "is_fixed_bid": True}


def attempt(r, t, pos):
    """Run the real pre-hedge against `pos`. Returns (shortfall_before, hedged,
    shortfall_after, gross_after)."""
    offer = as_offer(r)
    before = bot.net_shortfall(offer, pos)
    ex = sim.SimExchange([(float(t["ritc_bid_usd"]), float(t["ritc_ask_usd"]))])
    bot.api_request = ex.request
    bot.prehedge_position = 0
    hedged = bot.prehedge_for_tender(bot, offer, pos, sim.NullLogs(), 1,
                                     int(r["tick"]), {k: 0 for k in bot.SNAP_FIELDS})
    qty = float(r["quantity"])
    signed = -qty if r["action"] == "SELL" else qty
    gross = (abs(pos[bot.BULL]) + abs(pos[bot.BEAR])
             + 2 * abs(pos[bot.RITC] + signed))
    return before, hedged, bot.net_shortfall(offer, pos), gross


def main():
    blocked = load_blocked()
    if not blocked:
        print("no net-blocked tenders in the logs")
        return 1

    failures = []

    def check(name, cond, detail=""):
        if not cond:
            failures.append(name)
            print(f"  [FAIL] {name} - {detail}")

    cap = bot.SERVER_NET_LIMIT - bot.LIMIT_BUFFER
    gross_cap = bot.SERVER_GROSS_LIMIT - bot.LIMIT_BUFFER
    live = [(r, t) for r, t in blocked
            if bot.net_shortfall(as_offer(r), {
                bot.BULL: int(t["pos_bull"]), bot.BEAR: int(t["pos_bear"]),
                bot.RITC: int(t["pos_ritc"]), bot.USD: 0, bot.CAD: 0}) > 0]
    stale = len(blocked) - len(live)
    print(f"{len(blocked)} net-blocked tenders in the logs")
    if stale:
        # The netbar era used a percentage buffer, which refused a legal
        # 182,000 outright; those rows are legal under today's absolute buffer.
        print(f"  {stale} are already legal under the current LIMIT_BUFFER "
              f"(percentage-buffer era) - excluded")

    print(f"\nAS LOGGED (arb basket still in the book, pre-v11):")
    reach = 0
    for r, t in live:
        pos = {bot.BULL: int(t["pos_bull"]), bot.BEAR: int(t["pos_bear"]),
               bot.RITC: int(t["pos_ritc"]), bot.USD: 0, bot.CAD: 0}
        before, hedged, after, gross = attempt(r, t, pos)
        if hedged:
            reach += 1
            check("as-logged hedge clears the bar", after == 0,
                  f"h{r['heat']} t{r['tick']} left {after:,.0f}")
            check("as-logged hedge respects gross", gross <= gross_cap,
                  f"h{r['heat']} t{r['tick']} gross {gross:,.0f}")
    print(f"  reachable within MAX_PREHEDGE={bot.MAX_PREHEDGE:,}/side: "
          f"{reach}/{len(live)}")
    print(f"  the rest are blocked by the arb's own basket, which is v11's fix,")
    print(f"  not this one - the shortfall there is far larger than a hedge should be")

    print(f"\nV11-CLEAN (book carries only the unwind residual):")
    oversized = reached = 0
    hedges, fees = [], []
    for r, t in live:
        qty = float(r["quantity"])
        # Only the residual the unwind deliberately leaves behind.
        resid = bot.UNWIND_THRESHOLD if int(t["pos_ritc"]) >= 0 else -bot.UNWIND_THRESHOLD
        pos = {bot.BULL: 0, bot.BEAR: 0, bot.RITC: resid, bot.USD: 0, bot.CAD: 0}
        before, hedged, after, gross = attempt(r, t, pos)
        if before <= 0:
            continue          # v11 alone already unblocks it
        oversized += 1
        if hedged:
            reached += 1
            hedges.append(hedged)
            fees.append(hedged * 2 * bot.FEE_MKT)
            edge = float(r["edge_vs_mkt"] or 0)
            print(f"    h{r['heat']:<3} t{r['tick']:<4} {qty:>8,.0f} "
                  f"edge {edge:+.4f} = {edge * qty:>+9,.0f} USD | "
                  f"hedge {hedged:>5,}/side, fee {hedged * 2 * bot.FEE_MKT:>5,.0f}")
            check("v11-clean hedge clears the bar", after == 0,
                  f"h{r['heat']} t{r['tick']} left {after:,.0f}")
            check("v11-clean hedge respects gross", gross <= gross_cap,
                  f"h{r['heat']} t{r['tick']} gross {gross:,.0f}")
            # the hedge must come off once the ETF leg has cleared
            bot.tender_position = 0
            bot.unwind_prehedge(bot, pos, sim.NullLogs(), 1, int(r["tick"]),
                                {k: 0 for k in bot.SNAP_FIELDS})
            check("hedge is flattened after the tender clears",
                  bot.prehedge_position == 0, f"left {bot.prehedge_position:,}")
        else:
            print(f"    h{r['heat']} t{r['tick']} {qty:,.0f} NOT REACHED "
                  f"(needs {before:,.0f} of net room)")
    print(f"\n  still short on a clean book: {oversized} "
          f"(these need 2*qty > {cap:,})")
    print(f"  reached by pre-hedge: {reached}/{oversized}")
    if hedges:
        print(f"  hedge {min(hedges):,}-{max(hedges):,} per side, "
              f"fee {min(fees):,.0f}-{max(fees):,.0f} USD")
    check("every oversized tender is reached on a clean book",
          reached == oversized, f"{reached}/{oversized}")

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
