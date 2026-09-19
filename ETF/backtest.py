"""Per-heat scoreboard with P&L attribution, rebuilt from the logs.

`heats.csv` records what each heat made. It does not record WHERE that came
from, so a version comparison can say "v10 beat v8" without saying whether the
difference was tender capture, unwind cost, the arb, or luck. This reconstructs
the decomposition retroactively, so it covers every heat ever logged rather
than only heats run after some new column was added.

Per heat it attributes:

  tender_gate     what the accepted tenders were worth at the moment we took
                  them: gate edge x quantity. The opportunity captured.
  tender_real     what they actually paid: tender price against the VWAP of
                  the unwind that followed, x units filled.
  unwind_leak     tender_gate - tender_real. What the exit gave back.
  arb_net         3-leg spread arbs, realised and net of fees.
  missed          positive-edge tenders that were refused, and why.

and writes `logs/scoreboard.csv`, one row per heat, so version-over-version
movement can be read directly instead of re-derived each time.

Caveats worth keeping in view:

  - The unwind window is a fixed 25 ticks after the accept. A held tender
    (v14+) has no unwind, so `tender_real` is absent for it by construction,
    not because it lost money - those rows are marked `held`.
  - Heat P&L is NLV end minus NLV start, which includes everything: FX drift,
    mark-to-market on open positions, fines. The attribution columns will not
    sum to it, and are not meant to.

    python backtest.py
"""

import csv
import glob
import statistics
import sys
from collections import defaultdict

import calibration

UNWIND_WINDOW_TICKS = 25
HEAT_TICKS = 300
TARGET = 40000.0
FEE_MKT = 0.02


def load(name):
    rows = []
    for path in sorted(glob.glob(f"logs/{name}*.csv")):
        with open(path, newline="") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def f(row, key, default=None):
    try:
        return float(row.get(key) or "")
    except (ValueError, TypeError):
        return default


def i(row, key, default=0):
    try:
        return int(row.get(key) or "")
    except (ValueError, TypeError):
        return default


def mid_of(row):
    b, a = f(row, "ritc_bid_usd"), f(row, "ritc_ask_usd")
    if b is None or a is None or b <= 0 or a >= 1e9:
        return None
    return (a + b) / 2.0


def filled_qty(row):
    for key in ("quantity_filled", "qty"):
        v = f(row, key)
        if v:
            return v
    return 0.0


def build():
    heats = {}
    for r in load("heats"):
        pnl, first, last = f(r, "heat_pnl"), i(r, "first_tick", -1), i(r, "last_tick", -1)
        if pnl is None or first < 0 or last < first:
            continue
        key = (r.get("run_id", ""), r.get("heat", ""))
        heats[key] = {
            "run_id": key[0], "heat": key[1],
            "version": r.get("risk_model") or "(pre-versioning)",
            "pnl": pnl, "ticks": last - first + 1,
            "first_tick": first, "last_tick": last,
            "full": first <= 1 and last >= 299,
            "tenders_seen": 0, "tenders_accepted": 0,
            "tender_gate": 0.0, "tender_real": 0.0, "held": 0,
            "gate_unmeasured": 0.0, "gate_measured": 0.0,
            "unwind_units": 0.0, "passive_units": 0.0,
            "arb_count": 0, "arb_net": 0.0,
            "missed_edge": 0.0, "missed_n": 0, "missed_reason": "",
        }

    # --- unwind fills, indexed for the tender join ------------------------
    fills = defaultdict(list)
    for r in load("trades"):
        if r.get("direction") != "unwind":
            continue
        key = (r.get("run_id", ""), r.get("heat", ""))
        fills[key].append(r)
        h = heats.get(key)
        if h:
            q = filled_qty(r)
            h["unwind_units"] += q
            if r.get("exec_style") == "passive":
                h["passive_units"] += q

    # --- tenders ----------------------------------------------------------
    per_tender = defaultdict(list)
    for r in load("tenders"):
        if r.get("tender_id"):
            per_tender[(r.get("run_id", ""), r.get("heat", ""),
                        r["tender_id"])].append(r)

    reasons = defaultdict(lambda: defaultdict(float))
    for (run, heat, _tid), rows in per_tender.items():
        h = heats.get((run, heat))
        if h is None:
            continue
        h["tenders_seen"] += 1
        edges = [(f(x, "edge_vs_mkt", -9), x) for x in rows]
        best_edge, best = max(edges, key=lambda z: z[0])
        qty = f(best, "quantity", 0.0) or 0.0
        accepted = [x for x in rows if x.get("accept_ok") == "True"]
        if accepted:
            a = accepted[0]
            tick, px = i(a, "tick"), f(a, "price", 0.0)
            gate = f(a, "edge_vs_mkt", 0.0) or 0.0
            h["tenders_accepted"] += 1
            h["tender_gate"] += gate * qty
            win = [x for x in fills[(run, heat)]
                   if tick <= i(x, "tick") <= tick + UNWIND_WINDOW_TICKS
                   and f(x, "vwap")]
            got = sum(filled_qty(x) for x in win)
            if got:
                vwap = sum(filled_qty(x) * f(x, "vwap") for x in win) / got
                per_unit = (px - vwap) if a.get("action") == "SELL" else (vwap - px)
                h["tender_real"] += per_unit * min(got, qty)
                h["gate_measured"] += gate * qty
            else:
                # No unwind followed. Three different things look identical
                # here and only one of them is a result:
                #   - the version predated unwind logging (netbar_fxhedge has
                #     56 accepts and zero unwind rows), so it is UNMEASURABLE
                #   - the tender was held to close-out (v14+), so there is
                #     correctly nothing to measure
                #   - the heat ended first
                # Counting any of them as "realised 0" drags the average down
                # by 65% of the sample and makes tender capture look broken
                # when it is only unrecorded.
                h["held"] += 1
                h["gate_unmeasured"] += gate * qty
        elif best_edge > 0:
            h["missed_n"] += 1
            h["missed_edge"] += best_edge * qty
            why = (best.get("safe_reason") or "").split(" ")[0] or "?"
            reasons[h["version"]][why] += best_edge * qty

    # --- arbs -------------------------------------------------------------
    for a in calibration.load_arbs(sorted(glob.glob("logs/trades*.csv"))):
        h = heats.get((a.run_id, a.heat))
        if h:
            h["arb_count"] += 1
            h["arb_net"] += a.net * 5000      # ORDER_QTY per leg

    for h in heats.values():
        # Leak is only meaningful against the gate value we could actually
        # measure - never against tenders whose unwind was never recorded.
        h["unwind_leak"] = h["gate_measured"] - h["tender_real"]
    return heats, reasons


ORDER = ["(pre-versioning)", "ritc_x1", "ritc_x2", "ritc_x2_adaptive",
         "netbar_fxhedge", "v5_depthunwind", "v6_depthgate", "v7_edgecal",
         "v8_capacity", "v9_slipgated", "v10_revert", "v11_hedgesafe",
         "v12_passiveunwind", "v13_prehedge", "v14_holdout", "v15_exitbasis",
         "v16_trackersync", "v17_holdtick"]


def main():
    heats, reasons = build()
    if not heats:
        print("no heat data")
        return 1

    fields = ["run_id", "heat", "version", "full", "ticks", "pnl",
              "tenders_seen", "tenders_accepted", "held", "tender_gate",
              "tender_real", "gate_measured", "gate_unmeasured",
              "unwind_leak", "unwind_units", "passive_units",
              "arb_count", "arb_net", "missed_n", "missed_edge"]
    rows = sorted(heats.values(), key=lambda h: (h["run_id"], int(h["heat"] or 0)))
    with open("logs/scoreboard.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 2) if isinstance(v, float) else v)
                        for k, v in r.items()})

    by = defaultdict(list)
    for h in rows:
        by[h["version"]].append(h)

    print(f"target {TARGET:,.0f} per {HEAT_TICKS}-tick heat\n")
    print(f"{'version':<19}{'heats':>6}{'ticks':>7}{'P&L/tick':>10}"
          f"{'/heat':>10}{'tender gate':>13}{'meas':>7}{'realised':>10}{'leak':>9}"
          f"{'arb':>8}{'missed':>10}")
    for v in ORDER:
        hs = by.get(v)
        if not hs:
            continue
        t = sum(h["ticks"] for h in hs)
        scale = HEAT_TICKS / t if t else 0
        p = sum(h["pnl"] for h in hs)
        gm = sum(h["gate_measured"] for h in hs)
        gu = sum(h["gate_unmeasured"] for h in hs)
        cover = gm / (gm + gu) if (gm + gu) else 0.0
        real = f"{sum(h['tender_real'] for h in hs) * scale:>10,.0f}" if gm else f"{'n/a':>10}"
        leak = f"{sum(h['unwind_leak'] for h in hs) * scale:>9,.0f}" if gm else f"{'-':>9}"
        print(f"{v:<19}{len(hs):>6}{t:>7,}{p / t:>10,.1f}{p / t * HEAT_TICKS:>10,.0f}"
              f"{sum(h['tender_gate'] for h in hs) * scale:>13,.0f}{cover:>7.0%}"
              f"{real}{leak}"
              f"{sum(h['arb_net'] for h in hs) * scale:>8,.0f}"
              f"{sum(h['missed_edge'] for h in hs) * scale:>10,.0f}")
    print("\ncolumns after P&L are scaled to a 300-tick heat for comparability.")
    print("tender gate/realised/missed are USD; P&L and arb are CAD.")

    full = [h for h in rows if h["full"] and h["tenders_seen"]]
    if full:
        p = [h["pnl"] for h in full]
        print(f"\nHEADLINE - full 300-tick heats that saw tenders (n={len(p)}):")
        print(f"  mean {statistics.mean(p):,.0f}  median {statistics.median(p):,.0f}  "
              f"min {min(p):,.0f}  max {max(p):,.0f}  "
              f"({sum(1 for x in p if x > 0)}/{len(p)} winners)")
        print(f"  gap to target: {TARGET - statistics.mean(p):,.0f}/heat")
        print(f"  mean missed edge: "
              f"{statistics.mean([h['missed_edge'] for h in full]):,.0f} USD/heat")

    print("\nwhy positive-edge tenders were refused (USD, scaled per heat):")
    for v in ORDER:
        if v not in reasons:
            continue
        n = sum(h["ticks"] for h in by[v]) / HEAT_TICKS
        tot = {k: x / n for k, x in reasons[v].items()}
        line = "  ".join(f"{k} {x:,.0f}" for k, x in
                         sorted(tot.items(), key=lambda z: -z[1]))
        print(f"  {v:<19}{line}")
    print("\nwrote logs/scoreboard.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
