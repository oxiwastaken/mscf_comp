"""Regression test for the tender tracker against logged position sequences.

reconcile_tracker must survive the one-cycle settlement lag the server shows
straight after a tender accept, while still doing v16's job of shedding
inventory the arb genuinely moved. Every sequence below is taken from the live
logs, not invented.

    python test_reconcile.py
"""

from etf_arb_bot import reconcile_tracker, UNWIND_THRESHOLD

failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    failures += 0 if ok else 1


def run(tracker, reads):
    """Feed live RITC reads through the reconcile; return the tracker path."""
    bound, path = False, []
    for ritc in reads:
        _, tracker, bound, _ = reconcile_tracker(tracker, ritc, bound)
        path.append(tracker)
    return path


# v21 heat 13: SELL 89,000 accepted at tick 20; settlement flickers once. The
# old write-back took the -5,000 read and stranded -91,000 for 125 ticks.
path = run(-89_000, [-94_000, -5_000, -94_000, -91_000, -91_000])
check("heat 13 flicker does not shrink the tracker",
      min(abs(t) for t in path) >= 89_000, f"path {path}")

# v21 heat 10: +92,000 read, a -2,000 settlement read, then +92,000 again. The
# old code would clamp to 0 - under the unwind threshold - and reset outright.
path = run(92_000, [92_000, -2_000, 92_000])
check("heat 10 flicker does not zero the tracker",
      path[-1] == 92_000, f"path {path}")

# v17 heat 1: flicker through flat on the accept.
path = run(-97_000, [-97_000, 0, -97_000])
check("v17 heat 1 flicker through zero is survived",
      path[-1] == -97_000, f"path {path}")

# v16's reason for existing: the arb's ETF leg really does move RITC and the
# tracker must follow within a cycle, or the unwind buys back the arb's hedge.
path = run(-50_000, [-20_000, -20_000, -20_000])
check("a real, persistent move is still written back",
      path[-1] == -20_000, f"path {path}")
check("...one cycle later than before, and no later",
      path[0] == -50_000 and path[1] == -20_000, f"path {path}")

# The clamp must never push the tracker the wrong way or past the position.
_, t, _, _ = reconcile_tracker(-50_000, 10_000, True)
check("tracker never flips sign on a crossing read", t == 0, f"got {t}")
_, t, _, _ = reconcile_tracker(30_000, 90_000, True)
check("tracker never grows past what was accepted", t == 30_000, f"got {t}")

# A flicker that happens to land under the threshold must not reset the unwind.
target, t, _, skip = reconcile_tracker(92_000, -2_000, False)
check("a sub-threshold flicker skips rather than resets",
      skip and t == 92_000 and abs(target) > UNWIND_THRESHOLD,
      f"target {target} tracker {t} skip {skip}")

print(f"\n{failures} FAILED" if failures else "\nall passed")
raise SystemExit(1 if failures else 0)
