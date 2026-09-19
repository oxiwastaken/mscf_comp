"""
Structural experiments on the replay harness, scored for how the case is ranked.

The ETF case ranks teams per heat and averages the ranks. A +140,000 heat earns
no more than first place, while a -50,000 heat can drop a team to the bottom of
that heat - so once a strategy clears the field, consistency is worth more than
size, and the case-level tie-break favours lower variance too. Every policy is
therefore reported on its downside (10th and 25th percentile heat, losing
heats) as well as its mean, and every comparison is paired on the same heats.

    python structural.py

Two ideas are tested:

  SWAP    When a tender clears the edge bar but not the position limits, dump
          just enough same-direction inventory at market to make room, if the
          new tender's edge pays for the dump. Raises the mean, if anything.
  HEDGE   Hedge HELD inventory with BULL + BEAR up to the gross limit. Close-out
          prices the basket as well, so the hedge costs only its entry, and the
          exposure left is the RITC-to-NAV gap instead of the whole NAV and the
          FX that moves it. Aimed at the downside, not the mean.

Both depend on modelled execution (`dump_cost`, `basket_cost`), so each is swept
across a range of costs; a result that only survives at the cheapest cost is not
a result.
"""

import statistics as st
from dataclasses import replace

import replay_tenders as R


def dist(v):
    s, n = sorted(v), len(v)
    q = lambda p: s[min(n - 1, round(p * (n - 1)))]
    return (f"mean {st.mean(v):>+8,.0f}  med {st.median(v):>+8,.0f}  "
            f"p10 {q(0.10):>+8,.0f}  p25 {q(0.25):>+8,.0f}  "
            f"sd {st.stdev(v):>7,.0f}  losing {sum(x < 0 for x in v):>2}/{n}")


def run(heats, pol):
    return [R.simulate(h, pol)[0] for h in heats]


def compare(heats, base, pol, label):
    a, b = run(heats, base), run(heats, pol)
    m, se, t = R.paired(a, b)
    s = sorted(b)
    return b, f"   {label:<30} {dist(b)}   paired {m:>+7,.0f} (t {t:+.2f})"


def halves(heats, base, pol):
    out = []
    for part in (heats[: len(heats) // 2], heats[len(heats) // 2:]):
        m, _, t = R.paired(run(part, base), run(part, pol))
        out.append(f"{m:+,.0f} (t {t:+.2f})")
    return "  /  ".join(out)


def main():
    heats = R.load_heats()
    base = R.Policy("live")
    print(f"{len(heats)} full heats, v23 logic, paired against the live policy\n")
    print(f"   {'live':<30} {dist(run(heats, base))}")

    print("\nSWAP - dump inventory to take a blocked tender")
    for dc in (0.075, 0.10, 0.15):
        _, line = compare(heats, base, replace(base, swap=True, dump_cost=dc),
                          f"dump cost {dc:.3f}")
        print(line)

    print("\nHEDGE - held inventory hedged with BULL + BEAR")
    for bc in (0.02, 0.03, 0.05):
        for hf in (0.25, 0.5, 0.75, 1.0):
            _, line = compare(heats, base, replace(base, hedge_frac=hf, basket_cost=bc),
                              f"hedge {hf:.0%} @ {bc:.2f}/share/leg")
            print(line)

    print("\nREPLICATION - first half / second half of the heats, paired mean")
    for label, pol in (("swap, dump 0.10", replace(base, swap=True, dump_cost=0.10)),
                       ("hedge 100% @ 0.03", replace(base, hedge_frac=1.0, basket_cost=0.03)),
                       ("hedge 50% @ 0.03", replace(base, hedge_frac=0.5, basket_cost=0.03))):
        print(f"   {label:<20} {halves(heats, base, pol)}")


if __name__ == "__main__":
    main()
