"""Realised tender economics: tender price against the VWAP of the whole unwind.

The bot's per-chunk slippage column measures each fill against top-of-book at
that instant, which cannot see our own unwind dragging the quote between
cycles. It read +0.0123/unit while the real cost of a 92,000 unit unwind was
~0.14/unit of self-inflicted drift, and that gap is what made v8/v9 look safe
while they were destroying the edge they captured.

This scores the only thing that matters: what price we took the tender at
versus what the full unwind actually cost.

    python analyze_tenders.py
"""

import csv
from collections import defaultdict

UNWIND_WINDOW_TICKS = 25


def load(path):
    try:
        return list(csv.DictReader(open(path, newline="")))
    except FileNotFoundError:
        return []


def realised():
    trades = [r for r in load("logs/trades.csv") if r["direction"] == "unwind"]
    rows = []
    for t in load("logs/tenders.csv"):
        if t.get("accept_ok") != "True":
            continue
        try:
            tick, qty, px = int(t["tick"]), float(t["quantity"]), float(t["price"])
        except (ValueError, KeyError):
            continue
        fills = [r for r in trades
                 if r["risk_model"] == t["risk_model"] and r["heat"] == t["heat"]
                 and tick <= int(r["tick"]) <= tick + UNWIND_WINDOW_TICKS
                 and r["vwap"] not in ("", "None")]
        if not fills:
            continue
        filled = sum(float(r["qty"]) for r in fills)
        vwap = sum(float(r["qty"]) * float(r["vwap"]) for r in fills) / filled
        # Sold to them at px and bought back at vwap, or the reverse.
        per_unit = (px - vwap) if t["action"] == "SELL" else (vwap - px)
        rows.append({
            "version": t["risk_model"], "heat": t["heat"], "tick": tick,
            "action": t["action"], "qty": qty, "tender_px": px,
            "gate_edge": float(t["edge_vs_mkt"] or 0), "unwind_vwap": vwap,
            "realised_per_unit": per_unit, "total_usd": per_unit * min(filled, qty),
        })
    return rows


def main():
    rows = realised()
    if not rows:
        print("no accepted tenders with unwind fills yet")
        return

    with open("logs/tender_pnl.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print(f"{'version':<16} {'n':>3} {'gate edge':>10} {'realised':>10} {'slippage':>10} {'total USD':>11}")
    by = defaultdict(list)
    for r in rows:
        by[r["version"]].append(r)
    for v, rs in by.items():
        g = sum(x["gate_edge"] for x in rs) / len(rs)
        p = sum(x["realised_per_unit"] for x in rs) / len(rs)
        print(f"{v:<16} {len(rs):>3} {g:>+10.4f} {p:>+10.4f} {g - p:>+10.4f} "
              f"{sum(x['total_usd'] for x in rs):>+11,.0f}")
    print(f"\nwrote logs/tender_pnl.csv ({len(rows)} tenders)")
    print("gate edge minus realised = what the unwind gave back")


if __name__ == "__main__":
    main()
