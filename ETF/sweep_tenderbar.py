"""Where should MIN_TENDER_EDGE sit?

The bar exists to cover what a tender costs to get out of. So the first job is
to measure that cost, and the second is to check whether capacity makes the bar
higher than the cost alone would justify.

BREAK-EVEN is estimated two independent ways, which is the only reason to
believe either:

  STRUCTURAL   fee + execution - uplift, each measured separately.
               execution = fill vs contemporaneous mid, 32 live tenders.
               uplift    = how much the gate understates the tender by pricing
                           the exit at chunk VWAP instead of the mid, 80
                           distinct logged tender books.
  DIRECT       mean of (gate edge - realised per unit) over the same 32
               tenders. One number, no model.

They should agree. If they ever stop agreeing, one of the measurements has
drifted and the bar should not be moved until it is understood.

Then the bar is swept on expected value, using
    E[realised per unit] = gate_edge - break_even
over every distinct tender in the logs, and reported alongside the standard
deviation it buys, because a marginal tender is a large coin flip with a small
mean.

    python sweep_tenderbar.py
"""

import csv
import glob
import statistics
import sys
from collections import defaultdict

FEE = 0.02
EXEC_COST = 0.0550          # decompose_unwind.py, fill vs contemporaneous mid
EXEC_SD = 0.0304
BARS = [0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12]


def distinct_tenders():
    u = defaultdict(list)
    for path in sorted(glob.glob("logs/tenders*.csv")):
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("tender_id"):
                    u[(r["run_id"], r["heat"], r["tender_id"])].append(r)
    return u


def uplift_per_tender(u):
    """How much the gate understates a tender by pricing the exit at chunk
    VWAP rather than the mid. Averaged within a tender first - the ~6 rows of
    one tender's life are not independent observations."""
    out = []
    for rows in u.values():
        ups = []
        for r in rows:
            try:
                cv, b, a = (float(r["chunk_vwap"]), float(r["mkt_bid"]),
                            float(r["mkt_ask"]))
            except (ValueError, TypeError, KeyError):
                continue
            if cv <= 0 or not (0 < b < a < 1e9):
                continue
            mid = (a + b) / 2.0
            ups.append((cv - mid + FEE) if r["action"].upper() == "SELL"
                       else (mid - cv + FEE))
        if ups:
            out.append(statistics.mean(ups))
    return out


def main():
    u = distinct_tenders()
    if not u:
        print("no tender history")
        return 1
    try:
        pnl = list(csv.DictReader(open("logs/tender_pnl.csv", newline="")))
    except FileNotFoundError:
        print("run analyze_tenders.py first")
        return 1

    ups = uplift_per_tender(u)
    leak = [float(r["gate_edge"]) - float(r["realised_per_unit"]) for r in pnl]
    resid_sd = statistics.pstdev(leak)

    mu, su = statistics.mean(ups), statistics.pstdev(ups) / len(ups) ** 0.5
    se_exec = EXEC_SD / len(pnl) ** 0.5
    struct, se_struct = FEE + EXEC_COST - mu, (se_exec ** 2 + su ** 2) ** 0.5
    direct, se_direct = statistics.mean(leak), resid_sd / len(leak) ** 0.5

    w1, w2 = 1 / se_struct ** 2, 1 / se_direct ** 2
    be = (struct * w1 + direct * w2) / (w1 + w2)
    se_be = (1 / (w1 + w2)) ** 0.5

    print("BREAK-EVEN GATE EDGE")
    print(f"  structural  {struct:+.4f}  se {se_struct:.4f}   "
          f"(fee {FEE:.4f} + exec {EXEC_COST:.4f} - uplift {mu:.4f}, "
          f"n={len(ups)} tenders)")
    print(f"  direct      {direct:+.4f}  se {se_direct:.4f}   "
          f"(mean gate - realised, n={len(leak)} tenders)")
    print(f"  combined    {be:+.4f}  se {se_be:.4f}   "
          f"95% upper bound {be + 1.96 * se_be:+.4f}")
    if abs(struct - direct) > 2 * (se_struct ** 2 + se_direct ** 2) ** 0.5:
        print("  *** THE TWO ESTIMATORS DISAGREE - do not move the bar on this ***")
        return 1
    print(f"  they agree to {abs(struct - direct):.4f}/unit, so the estimate stands.")

    # --- sweep ------------------------------------------------------------
    heats = len({(k[0], k[1]) for k in u})
    tenders = []
    for rows in u.values():
        edges = [float(x["edge_vs_mkt"]) for x in rows
                 if x.get("edge_vs_mkt") not in (None, "", "None")]
        if edges:
            tenders.append((max(edges), float(rows[0]["quantity"] or 0)))

    print(f"\nEXPECTED VALUE BY BAR  ({len(tenders)} distinct tenders over "
          f"{heats} heats; E[realised] = gate - {be:.4f})")
    print(f"{'bar':>7}{'se above BE':>13}{'tenders/heat':>14}"
          f"{'EV USD/heat':>13}{'sd/heat':>10}{'vs 0.08':>10}")
    base = None
    rows_out = []
    for bar in BARS:
        take = [(g, q) for g, q in tenders if g >= bar]
        ev = sum((g - be) * q for g, q in take) / heats
        sd = (sum((resid_sd * q) ** 2 for _, q in take) / heats) ** 0.5
        rows_out.append((bar, ev, sd, len(take) / heats))
        if bar == 0.08:
            base = ev
    for bar, ev, sd, n in rows_out:
        delta = "" if base is None else f"{ev - base:>+10,.0f}"
        print(f"{bar:>7.3f}{(bar - be) / se_be:>13.1f}{n:>14.2f}"
              f"{ev:>13,.0f}{sd:>10,.0f}{delta}")

    best = max(rows_out, key=lambda r: r[1])
    print(f"\nEV peaks at bar {best[0]:.3f} ({best[1]:,.0f}/heat). The curve is flat:")
    near = [r for r in rows_out if r[1] >= best[1] - 1000]
    print(f"  within 1,000 USD/heat of the peak: "
          f"bars {near[0][0]:.3f} to {near[-1][0]:.3f}")
    print("\nSo the bar is not chosen at the peak but at the lowest value that is")
    print("comfortably clear of break-even, since everything in that range earns")
    print("within noise of the same amount and a lower bar only adds the")
    print("marginal, highest-variance tenders.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
