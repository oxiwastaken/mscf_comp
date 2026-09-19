"""Split what a tender gives back into execution cost and market drift.

`analyze_tenders.py` scores tender price against the VWAP of the whole unwind.
That is the right bottom line, but it is one number covering two different
problems with two different fixes:

    tender_px  ->  mid at accept   : the edge the tender actually carried
    mid at accept -> mid at fill   : DRIFT - the market moved while we held it
    mid at fill -> our fill price  : EXECUTION - spread, and our own impact

Attacking the wrong one is how v8/v9 were lost. If the gap is execution, the
answer is order placement - rest more, cross less, split across books. If it is
drift, order placement is irrelevant and the answer is holding time, or not
taking the position at all.

Both are measured against the mid, not the touch, because the touch is itself
moved by our own trading and a cost measured against a quote we are pushing
cannot see the push.

    python decompose_unwind.py
"""

import csv
import glob
import statistics
import sys
from collections import defaultdict

UNWIND_WINDOW_TICKS = 25


def load(name):
    rows = []
    for path in sorted(glob.glob(f"logs/{name}*.csv")):
        with open(path, newline="") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def num(row, key):
    try:
        v = float(row.get(key) or "")
    except ValueError:
        return None
    return v


def mid_of(row):
    b, a = num(row, "ritc_bid_usd"), num(row, "ritc_ask_usd")
    if b is None or a is None or b <= 0 or a >= 1e9:
        return None
    return (a + b) / 2.0


def main():
    trades = [r for r in load("trades") if r["direction"] == "unwind"]
    by_heat = defaultdict(list)
    for r in trades:
        by_heat[(r["run_id"], r["risk_model"], r["heat"])].append(r)

    rows = []
    for t in load("tenders"):
        if t.get("accept_ok") != "True":
            continue
        try:
            tick, qty, px = int(t["tick"]), float(t["quantity"]), float(t["price"])
        except (ValueError, KeyError):
            continue
        accept_mid = mid_of(t) if mid_of(t) else None
        fills = [r for r in by_heat.get((t["run_id"], t["risk_model"], t["heat"]), [])
                 if tick <= int(r["tick"]) <= tick + UNWIND_WINDOW_TICKS
                 and num(r, "vwap")]
        if not fills:
            continue
        # The tender log carries no mid of its own in every generation, so fall
        # back to the first fill's snapshot - same cycle, same book fetch.
        if accept_mid is None:
            accept_mid = mid_of(fills[0])
        if accept_mid is None:
            continue

        sign = 1 if t["action"] == "SELL" else -1   # SELL to them = we are short
        filled = exec_cost = drift = 0.0
        vw = 0.0
        for r in fills:
            q = num(r, "quantity_filled") or num(r, "qty") or 0
            fill_px, fill_mid = num(r, "vwap"), mid_of(r)
            if not q or fill_px is None or fill_mid is None:
                continue
            filled += q
            vw += q * fill_px
            # We are buying back a short (sign +1) or selling out a long (-1).
            # Paying above the mid when buying, or below it when selling, is cost.
            exec_cost += q * (fill_px - fill_mid) * sign
            # Market moving against the position we are carrying.
            drift += q * (fill_mid - accept_mid) * sign
        if not filled:
            continue
        vwap = vw / filled
        rows.append({
            "version": t["risk_model"], "heat": t["heat"], "action": t["action"],
            "qty": qty, "filled": filled,
            "gate": float(t["edge_vs_mkt"] or 0),
            "tender_edge": (accept_mid - px) * -sign,
            "exec": exec_cost / filled,
            "drift": drift / filled,
            "realised": (px - vwap) if t["action"] == "SELL" else (vwap - px),
        })

    if not rows:
        print("no accepted tenders with unwind fills")
        return 1

    print(f"{'version':<18}{'n':>3}{'tender edge':>12}{'execution':>11}"
          f"{'drift':>10}{'realised':>10}{'USD/tender':>12}")
    by = defaultdict(list)
    for r in rows:
        by[r["version"]].append(r)
    for v, rs in sorted(by.items()):
        f = lambda k: statistics.mean(x[k] for x in rs)
        usd = statistics.mean(x["realised"] * x["filled"] for x in rs)
        print(f"{v:<18}{len(rs):>3}{f('tender_edge'):>+12.4f}{-f('exec'):>+11.4f}"
              f"{-f('drift'):>+10.4f}{f('realised'):>+10.4f}{usd:>+12,.0f}")

    alls = rows
    ex = statistics.mean(x["exec"] for x in alls)
    dr = statistics.mean(x["drift"] for x in alls)
    print(f"\npooled over {len(alls)} tenders, per unit:")
    print(f"  execution (fill vs contemporaneous mid): {-ex:+.4f}"
          f"   sd {statistics.pstdev([x['exec'] for x in alls]):.4f}")
    print(f"  drift     (mid moved while we held it): {-dr:+.4f}"
          f"   sd {statistics.pstdev([x['drift'] for x in alls]):.4f}")
    share = abs(ex) / (abs(ex) + abs(dr)) if (ex or dr) else 0
    print(f"\n  execution is {share:.0%} of the total give-back.")
    print("  A large, low-spread execution term is fixable by order placement.")
    print("  A large drift term with a wide sd is holding time, and no amount of")
    print("  clever order placement touches it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
