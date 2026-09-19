"""Pick HOLD_TO_CLOSEOUT_TICK by expected value rather than by eye.

Holding a tender to close-out saves its execution cost outright. It costs the
tenders it then blocks. Both sides are measurable from the logs:

  SAVED   the uplift from exiting at close-out (the mid, no fee) instead of
          through a market unwind, measured per tender from the logged
          `chunk_vwap` - mean +0.0626/unit across 385 observations

  BLOCKED only LATER tenders in the same heat that clear the bar AND run the
          same direction. An opposite-direction tender still fits - it reduces
          net rather than adding to it - and is in fact the ideal outcome,
          since it flattens the held position and pays us for it. 43% of
          consecutive tenders are opposite, so counting all later tenders as
          blocked (as the first cut of this did) roughly doubles the cost.

Both sides are per-tender totals over the same logged history, so the sweep is
a like-for-like comparison and the argmax is the threshold to use.

What this does NOT price is drift. Holding longer widens the distribution of
heat P&L without moving its mean (-0.0126/unit against a 0.0779 sd), so it does
not belong in an expected-value sweep - but it is the reason not to push the
threshold far below the argmax for a few hundred dollars.

    python sweep_holdtick.py
"""

import csv
import glob
import statistics
import sys
from collections import defaultdict

BAR = 0.08          # MIN_TENDER_EDGE, the bar a later tender must clear
CANDIDATES = [0, 60, 90, 120, 150, 175, 200, 225, 250, 301]


def load_heats():
    """{heat: [tender]} with arrival tick, direction, size and best edge."""
    u = defaultdict(list)
    for path in sorted(glob.glob("logs/tenders*.csv")):
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("tender_id"):
                    u[(r["run_id"], r["heat"], r["tender_id"])].append(r)

    uplifts, heats = [], defaultdict(list)
    for key, rows in u.items():
        edges = [float(x["edge_vs_mkt"]) for x in rows
                 if x.get("edge_vs_mkt") not in (None, "", "None")]
        if not edges:
            continue
        # Per-tender uplift where the book was logged; pooled mean otherwise.
        ups = []
        for x in rows:
            try:
                cv, b, a = (float(x["chunk_vwap"]), float(x["mkt_bid"]),
                            float(x["mkt_ask"]))
            except (ValueError, TypeError, KeyError):
                continue
            if cv <= 0 or not (0 < b < a < 1e9):
                continue
            mid = (a + b) / 2.0
            ups.append((cv - mid + 0.02) if x["action"].upper() == "SELL"
                       else (mid - cv + 0.02))
        uplifts.extend(ups)
        heats[(key[0], key[1])].append({
            "tick": min(int(x["tick"]) for x in rows),
            "act": rows[0]["action"], "qty": float(rows[0]["quantity"]),
            "best": max(edges),
            "uplift": statistics.mean(ups) if ups else None,
        })
    pooled = statistics.mean(uplifts) if uplifts else 0.0626
    for ts in heats.values():
        ts.sort(key=lambda x: x["tick"])
        for t in ts:
            if t["uplift"] is None:
                t["uplift"] = pooled
    return heats, pooled


def captured_value():
    """What one captured tender is actually worth, from realised unwind P&L."""
    try:
        rows = list(csv.DictReader(open("logs/tender_pnl.csv", newline="")))
    except FileNotFoundError:
        return None
    vals = [float(r["realised_per_unit"]) * float(r["qty"])
            for r in rows if r.get("realised_per_unit")]
    return statistics.median(vals) if vals else None


def main():
    heats, pooled = load_heats()
    if not heats:
        print("no tender history")
        return 1
    per_tender = captured_value()
    if per_tender is None:
        print("run analyze_tenders.py first")
        return 1

    n_heats = len(heats)
    print(f"{n_heats} heats, {sum(len(v) for v in heats.values())} tenders")
    print(f"pooled uplift {pooled:+.4f}/unit; a captured tender is worth "
          f"{per_tender:,.0f} USD (median realised)\n")

    print(f"{'threshold':>10}{'held/heat':>11}{'saved/heat':>12}"
          f"{'blocked/heat':>14}{'cost/heat':>11}{'NET/heat':>11}")
    best = None
    for T in CANDIDATES:
        saved = blocked = held = 0.0
        for ts in heats.values():
            for i, t in enumerate(ts):
                if t["tick"] < T:
                    continue
                held += 1
                saved += t["uplift"] * t["qty"]
                # Only later, same-direction tenders over the bar are blocked.
                blocked += sum(1 for x in ts[i + 1:]
                               if x["best"] >= BAR and x["act"] == t["act"])
        net = (saved - blocked * per_tender) / n_heats
        print(f"{T:>10}{held / n_heats:>11.2f}{saved / n_heats:>12,.0f}"
              f"{blocked / n_heats:>14.2f}{blocked * per_tender / n_heats:>11,.0f}"
              f"{net:>+11,.0f}")
        if best is None or net > best[1]:
            best = (T, net)

    print(f"\nargmax: threshold {best[0]} at {best[1]:+,.0f} USD/heat")

    # The argmax is only as good as `per_tender`, which is a median over 32
    # noisy observations. Sweep it too, and pick for lowest MAXIMUM REGRET
    # rather than best central case - a threshold that is near-optimal across
    # the whole plausible range beats one that wins the point estimate and
    # collapses either side of it.
    print(f"\nsensitivity - argmax moves with what a captured tender is worth:")
    grid = [4000, 6000, round(per_tender), 12000, 16000, 20000]
    cands = [T for T in CANDIDATES if T < 250]
    print(f"{'per tender':>12} " + "".join(f"{T:>9}" for T in cands) + "   argmax")
    table = {}
    for pv in grid:
        row, top = [], None
        for T in cands:
            saved = blocked = 0.0
            for ts in heats.values():
                for i, t in enumerate(ts):
                    if t["tick"] < T:
                        continue
                    saved += t["uplift"] * t["qty"]
                    blocked += sum(1 for x in ts[i + 1:]
                                   if x["best"] >= BAR and x["act"] == t["act"])
            net = (saved - blocked * pv) / n_heats
            row.append(net)
            if top is None or net > top:
                top = net
        table[pv] = (row, top)
        argmax = cands[row.index(max(row))]
        print(f"{pv:>12,} " + "".join(f"{v:>9,.0f}" for v in row) + f"   {argmax}")

    print(f"\n{'threshold':>10}{'worst regret vs that row max':>32}")
    regrets = {}
    for j, T in enumerate(cands):
        regrets[T] = max(top - row[j] for row, top in table.values())
    for T in cands:
        print(f"{T:>10}{regrets[T]:>32,.0f}")
    pick = min(regrets, key=regrets.get)
    print(f"\nminimax-regret choice: {pick} "
          f"(worst regret {regrets[pick]:,.0f} USD/heat)")
    print("Holding EVERY tender (threshold 0) is the degenerate case - it takes")
    print("the first tender of the heat and refuses the rest, which is why the")
    print("blocked column has to be counted per tender rather than per heat.")
    print("\nDrift is deliberately not priced here: mean-zero, so it cannot move")
    print("the argmax, but it is the reason to sit at the argmax rather than")
    print("below it chasing the last few hundred dollars.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
