"""How long should the passive layer be given before it has to cross?

The passive unwind trades a certain saving for an uncertain one. The fee term
is deterministic: every passively filled share turns a -0.02 fee into a +0.01
rebate. The price term is not - resting instead of crossing means holding the
position for more cycles, and over those cycles the price moves for reasons
that have nothing to do with us.

So the deadline is the whole design. Too short and nothing fills passively; too
long and the fee saving is swamped by drift on a position we are being paid to
carry for no reason.

This sweeps `UNWIND_DEADLINE_CYCLES` over every 60-cycle window of the logged
RITC tape and reports, per setting: how much fills passively, the fee saved, and
- the number that matters - the MEAN and SPREAD of the price term. A mean near
zero with a wide spread means the extra cycles add variance, not cost, and the
right deadline is then the shortest one that still earns most of the fee.

    python sweep_deadline.py
"""

import statistics
import sys

import etf_arb_bot as bot
import sim_unwind as sim

DEADLINES = [3, 5, 8, 12, 18, 25]
WINDOW = 60


def windows(quotes, stride=20):
    return [quotes[i:i + WINDOW] for i in range(0, len(quotes) - WINDOW, stride)]


def trial(start, w, deadline, passive):
    bot.UNWIND_DEADLINE_CYCLES = deadline
    ex, st, trace = sim.run(start, w, passive=passive, cycles=WINDOW - 1)
    s = sim.summarise(ex, start)
    flat = next((c for c, p, r in trace if abs(p) <= bot.UNWIND_THRESHOLD), None)
    return s, flat


def main():
    quotes = sim.quote_series(limit=0)
    ws = windows(quotes)
    if len(ws) < 20:
        print("not enough logged quotes")
        return 1
    print(f"{len(ws)} windows of {WINDOW} cycles from the logged RITC tape\n")

    for start in (92000, -87000):
        print(f"=== start {start:+,} ===")
        # Baseline: v10, market orders only.
        base = [trial(start, w, 25, False) for w in ws]
        base_vwap = statistics.mean(s["vwap"] for s, _ in base if s)
        base_fee = statistics.mean(s["fees"] for s, _ in base if s)
        base_flat = statistics.mean(f for _, f in base if f is not None)
        sign = 1 if start > 0 else -1   # selling: a higher vwap is better
        # Paired per WINDOW, not against the pooled mean. RITC ranges 22-27
        # across these logs, so differencing a window against the average of all
        # windows measures where that window sat in the price range, not what
        # the execution choice did - it reported a 0.94/unit spread that was
        # entirely cross-window price level.
        pair = {id(w): s["vwap"] for w, (s, _) in zip(ws, base) if s}
        print(f"  v10 market-only: flat at cycle {base_flat:.1f}, "
              f"fees {base_fee:>+8,.0f}, vwap {base_vwap:.4f}")
        print(f"\n  {'deadline':>8} {'passive':>8} {'flat@':>6} {'fee saved':>10} "
              f"{'price/unit':>11} {'price sd':>9} {'total':>10} {'worst':>10}")
        for d in DEADLINES:
            res = [(w, trial(start, w, d, True)) for w in ws]
            ok = [(s, f) for _, (s, f) in res if s]
            share = statistics.mean(s["passive_share"] for s, _ in ok)
            flat = statistics.mean(f for _, f in ok if f is not None)
            fee = base_fee - statistics.mean(s["fees"] for s, _ in ok)
            px = [sign * (s["vwap"] - pair[id(w)])
                  for w, (s, _) in res if s and id(w) in pair]
            mean_px, sd_px = statistics.mean(px), statistics.pstdev(px)
            total = fee + mean_px * abs(start)
            worst = fee + min(px) * abs(start)
            print(f"  {d:>8} {share:>7.0%} {flat:>6.1f} {fee:>+10,.0f} "
                  f"{mean_px:>+11.4f} {sd_px:>9.4f} {total:>+10,.0f} {worst:>+10,.0f}")
        print()

    print("fee saved is deterministic; price/unit is drift and should average to ~0.")
    print("'worst' is the single worst window - the tail this exposes us to.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
