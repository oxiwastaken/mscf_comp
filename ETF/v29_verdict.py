"""Did v29 actually do what it claims? One heat, judged on MECHANISM.

    python v29_verdict.py

One heat cannot answer "is v29 better" on P&L. The modelled gain is ~300 CAD a
heat (+111/arb x 2.7 arbs) against a per-heat standard deviation of 49,434 -
you would be reading noise and calling it a result.

What one heat CAN answer is whether the machinery does what it was built to do.
Every row below is near-deterministic given the code: cycles per tick, looks per
tender and limit breaches follow from the implementation, not from whether the
market was kind. That is the same reasoning used to judge the volatility
changes, where single-heat P&L was worthless at a $60k spread but hedge volume
and delta breaches settled a question in one run.

Compares the newest v29 heat against the pooled v27 history in the same logs.
"""

import csv
import collections
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")


def rows(name):
    path = os.path.join(LOGS, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as fh:
        return list(csv.DictReader(fh))


def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def looks_per_tender(tender_rows, run_ids):
    counts = collections.Counter()
    for r in tender_rows:
        if r.get("run_id") in run_ids:
            counts[r.get("tender_id") or r.get("caption", "")[:24]] += 1
    return sorted(counts.values())


def main():
    heats = rows("heats.csv")
    if not heats:
        print("  no heats.csv yet")
        return 1
    v29 = [h for h in heats if "v29" in (h.get("risk_model") or "")]
    old = [h for h in heats if "v29" not in (h.get("risk_model") or "")]
    if not v29:
        print("  no v29 heat logged yet - run one, then re-run this")
        return 1

    latest = v29[-1]
    ticks = rows("ticks.csv")
    tenders = rows("tenders.csv")
    new_ids = {latest.get("run_id")}
    old_ids = {h.get("run_id") for h in old}

    def cyc(h):
        span = num(h.get("last_tick")) - num(h.get("first_tick")) + 1
        return num(h.get("cycles")) / span if span > 0 else 0.0

    def capture(h):
        seen = num(h.get("tenders_seen"))
        return num(h.get("tenders_accepted")) / seen if seen else 0.0

    new_looks = looks_per_tender(tenders, new_ids)
    old_looks = looks_per_tender(tenders, old_ids)
    breach_new = sum(1 for t in ticks if t.get("run_id") in new_ids
                     and str(t.get("within_limits")).lower() in ("false", "0"))
    breach_old = sum(1 for t in ticks if t.get("run_id") in old_ids
                     and str(t.get("within_limits")).lower() in ("false", "0"))

    print(f"\n  v29 heat {latest.get('heat')}  vs  {len(old)} earlier heats\n")
    print(f"  {'metric':<26} {'v27 baseline':>14} {'v29 (this heat)':>17}  verdict")

    def line(name, a, b, good, fmt="{:.2f}"):
        mark = "OK" if good else "<-- CHECK"
        print(f"  {name:<26} {fmt.format(a):>14} {fmt.format(b):>17}  {mark}")

    c_old = statistics.mean([cyc(h) for h in old]) if old else 0.0
    c_new = cyc(latest)
    line("cycles per tick", c_old, c_new, c_new > c_old * 3)

    if old_looks and new_looks:
        l_old = statistics.median(old_looks)
        l_new = statistics.median(new_looks)
        line("looks per tender (median)", l_old, l_new, l_new >= l_old * 1.5)

    line("tender capture rate",
         statistics.mean([capture(h) for h in old]) if old else 0.0,
         capture(latest), True, "{:.1%}")

    a_old = statistics.mean([num(h.get("arbs_fired")) for h in old]) if old else 0.0
    a_new = num(latest.get("arbs_fired"))
    # Their prediction is explicit: arbs should be ROUGHLY UNCHANGED, because
    # the arb is limit-bound not observation-bound. A big jump is not a win, it
    # is a sign the net gate was misread - check limits before celebrating.
    line("arbs fired", a_old, a_new, a_new <= a_old * 2.5)

    line("limit-breach ticks", breach_old, breach_new,
         breach_new == 0, "{:.0f}")

    print(f"\n  heat P&L this heat: {num(latest.get('heat_pnl')):,.0f}"
          f"   (baseline mean {statistics.mean([num(h.get('heat_pnl')) for h in old]):,.0f}"
          f", sd {statistics.pstdev([num(h.get('heat_pnl')) for h in old]):,.0f})")
    print("  P&L on ONE heat is not evidence at this spread - read the rows above.\n")

    print("  The thesis rows: cycles/tick and looks-per-tender must move, or the")
    print("  speed did nothing. Limit breaches must stay at zero, or concurrency")
    print("  broke sequential consistency and v29 should come off.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
