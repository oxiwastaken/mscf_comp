"""Where we actually stand against the 40,000/heat target.

A competition heat is 300 ticks. Most logged heats are not: a run started
mid-heat, or was stopped part way, or the tick reset was caught late. Scoring
those at face value understates them, and dropping them throws away most of the
sample - there are 82 heat rows and only 45 clean ones.

So this reports three things per version and keeps them separate:

  FULL      heats that ran tick 0/1 -> 299. The honest number, small sample.
  SCALED    every heat with >= 60 ticks, P&L scaled to 300 ticks. Bigger
            sample, but it assumes P&L accrues evenly across a heat, which
            tenders do not - they arrive ~6 times spread through it, so a
            60-tick fragment that happened to contain a good one scales up
            absurdly. Treated as a cross-check, never as the headline.
  PER-TICK  P&L per tick pooled across all heats of that version. Immune to
            heat length, and the right thing to compare versions on.

The target is 40,000 per 300-tick heat = 133.3 per tick.

    python heat_report.py
"""

import csv
import glob
import statistics
import sys
from collections import defaultdict

TARGET = 40000.0
HEAT_TICKS = 300
MIN_TICKS = 60

# Chronological, so the table reads as the project's actual trajectory.
ORDER = ["ritc_x1", "ritc_x2", "ritc_x2_adaptive", "netbar_fxhedge",
         "v5_depthunwind", "v6_depthgate", "v7_edgecal", "v8_capacity",
         "v9_slipgated", "v10_revert", "v11_hedgesafe", "v12_passiveunwind",
         "v13_prehedge", "v14_holdout", "v15_exitbasis", "v16_trackersync",
         "v17_holdtick"]


def load():
    rows = []
    for path in sorted(glob.glob("logs/heats*.csv")):
        with open(path, newline="") as fh:
            rows.extend(csv.DictReader(fh))
    out = []
    for r in rows:
        try:
            pnl = float(r["heat_pnl"])
            first, last = int(r["first_tick"]), int(r["last_tick"])
        except (ValueError, TypeError, KeyError):
            continue
        ticks = last - first + 1
        if ticks < MIN_TICKS:
            continue
        out.append({
            "version": r.get("risk_model", "?"), "run": r.get("run_id", ""),
            "heat": r.get("heat", ""), "pnl": pnl, "ticks": ticks,
            "full": first <= 1 and last >= 299,
            "tenders": _int(r, "tenders_seen"), "acc": _int(r, "tenders_accepted"),
            "arbs": _int(r, "arbs_fired"),
            "passive": _float(r, "passive_share"),
        })
    return out


def _int(r, k):
    try:
        return int(r.get(k) or 0)
    except ValueError:
        return 0


def _float(r, k):
    try:
        return float(r.get(k) or 0)
    except ValueError:
        return 0.0


def stat(vals):
    if not vals:
        return None
    return (len(vals), statistics.mean(vals),
            statistics.median(vals), min(vals), max(vals),
            sum(1 for v in vals if v > 0))


def main():
    heats = load()
    if not heats:
        print("no heat data")
        return 1
    by = defaultdict(list)
    for h in heats:
        by[h["version"]].append(h)

    print(f"target {TARGET:,.0f}/heat over {HEAT_TICKS} ticks "
          f"= {TARGET / HEAT_TICKS:.1f}/tick\n")

    print(f"{'version':<19}{'FULL heats':>22}{'per-tick (all heats)':>30}")
    print(f"{'':<19}{'n':>4}{'mean':>10}{'win':>8}"
          f"{'n':>5}{'ticks':>8}{'P&L/tick':>11}{'-> /heat':>12}{'vs target':>11}")
    for v in ORDER:
        hs = by.get(v)
        if not hs:
            continue
        full = [h["pnl"] for h in hs if h["full"]]
        ticks = sum(h["ticks"] for h in hs)
        pnl = sum(h["pnl"] for h in hs)
        per_tick = pnl / ticks
        scaled = per_tick * HEAT_TICKS
        f = stat(full)
        fcol = (f"{f[0]:>4}{f[1]:>10,.0f}{f[5]}/{f[0]:<6}" if f
                else f"{'-':>4}{'-':>10}{'-':>8}")
        print(f"{v:<19}{fcol}{len(hs):>5}{ticks:>8,}{per_tick:>11,.1f}"
              f"{scaled:>12,.0f}{scaled / TARGET:>10.0%}")

    unlisted = [v for v in by if v not in ORDER]
    for v in unlisted:
        print(f"  (unlisted version in logs: {v}, {len(by[v])} heats)")

    # The headline: what has actually run, most recent generation first.
    print()
    tender_era = [h for h in heats if h["tenders"] > 0]
    if tender_era:
        t = sum(h["ticks"] for h in tender_era)
        p = sum(h["pnl"] for h in tender_era)
        print(f"TENDER ERA (every heat that saw a tender, {len(tender_era)} heats, "
              f"{t:,} ticks):")
        print(f"  {p / t:,.1f}/tick -> {p / t * HEAT_TICKS:,.0f} per 300-tick heat "
              f"= {p / t * HEAT_TICKS / TARGET:.0%} of target")
        full = [h["pnl"] for h in tender_era if h["full"]]
        if full:
            print(f"  full heats only: n={len(full)} mean {statistics.mean(full):,.0f} "
                  f"median {statistics.median(full):,.0f} "
                  f"({sum(1 for x in full if x > 0)}/{len(full)} winners)")
            need = TARGET - statistics.mean(full)
            print(f"  gap to target: {need:,.0f}/heat")

    # Tender capture, which is where the money is and therefore where a
    # shortfall has to be explained.
    print()
    print(f"{'version':<19}{'heats':>7}{'tenders seen':>14}{'accepted':>10}"
          f"{'capture':>9}{'passive share':>15}")
    for v in ORDER:
        hs = by.get(v)
        if not hs:
            continue
        seen = sum(h["tenders"] for h in hs)
        acc = sum(h["acc"] for h in hs)
        if not seen:
            continue
        pas = [h["passive"] for h in hs if h["passive"]]
        pcol = f"{statistics.mean(pas):.0%}" if pas else "n/a"
        print(f"{v:<19}{len(hs):>7}{seen:>14}{acc:>10}{acc / seen:>9.1%}{pcol:>15}")
    print("\nnote: `tenders seen` counts one row per cycle of a tender's ~6-cycle")
    print("life, so capture % is per evaluation, not per distinct tender.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
