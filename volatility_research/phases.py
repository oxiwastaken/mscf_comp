"""
Where in a heat is the money actually made - and where is it available?

    python phases.py

Two curves, both averaged over every recording.

EARNED comes straight from the recordings: `trader.nlv` is logged every tick, so
the P&L of any stretch of the heat is just the difference across it. No model,
no simulation.

AVAILABLE comes from the quote oracle in ceiling.py - a trader who knows where
the maker's quote is heading, trading the same straddle through the same fill
model. The gap between the two curves is the opportunity we are not taking, and
WHEN it occurs says what to do about it.

The reason to look at the opening specifically: vega scales with the square root
of time to expiry, so at tick 1 (T = 1/12) an option carries about four times
the vega it has at tick 280. A volatility point captured early is worth several
captured late. If the early ticks are underused, that is the most expensive
place in the heat to be idle.
"""

import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

import backtest
import oracle
import sweep
from replay import load

BUCKETS = ((0, 25), (25, 50), (50, 75), (75, 100), (100, 150),
           (150, 200), (200, 250), (250, 300))


def earned(path):
    """{bucket: NLV change} for one heat, from the recorded trader.nlv."""
    marks = {}
    for line in open(path, encoding='utf-8'):
        try:
            import json
            snap = json.loads(line)
        except ValueError:
            continue
        nlv = (snap.get('trader') or {}).get('nlv')
        if nlv is not None:
            marks[snap['case'].get('tick', 0)] = float(nlv)
    if not marks:
        return None

    def at(tick):
        keys = [k for k in marks if k <= tick]
        return marks[max(keys)] if keys else 0.0

    return {b: at(b[1]) - at(b[0]) for b in BUCKETS}


def available(path):
    """{bucket: NLV change} for the quote oracle on the same heat."""
    sweep.apply(sweep.RAMP)
    snapshots = load(path)
    book, _marks = backtest.run(snapshots, 'continuous',
                                forecast_path=oracle.future_view(snapshots, 10))
    marks = dict(book.curve)
    if not marks:
        return None

    def at(tick):
        keys = [k for k in marks if k <= tick]
        return marks[max(keys)] if keys else 0.0

    return {b: at(b[1]) - at(b[0]) for b in BUCKETS}


def job(path):
    return earned(path), available(path)


def main():
    paths = sweep.usable_heats()
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(job, paths, chunksize=8))
    curves = [r[0] for r in results if r[0]]
    ceiling = [r[1] for r in results if r[1] is not None]

    print(f'\n  {len(curves)} heats\n')
    totals, best = {}, {}
    for bucket in BUCKETS:
        got = [c[bucket] for c in curves if bucket in c]
        could = [c[bucket] for c in ceiling if c and bucket in c]
        totals[bucket] = statistics.mean(got) if got else 0.0
        best[bucket] = statistics.mean(could) if could else 0.0
    grand = sum(totals.values()) or 1.0
    grand_best = sum(best.values()) or 1.0

    print(f'  {"ticks":>11} {"earned":>11} {"share":>7} | '
          f'{"available":>11} {"share":>7} | {"missed":>11} {"capture":>8}')
    for bucket in BUCKETS:
        missed = best[bucket] - totals[bucket]
        capture = totals[bucket] / best[bucket] if best[bucket] > 0 else 0.0
        print(f'  {bucket[0]:>4}-{bucket[1]:<6} ${totals[bucket]:>10,.0f} '
              f'{totals[bucket] / grand:>6.0%} | ${best[bucket]:>10,.0f} '
              f'{best[bucket] / grand_best:>6.0%} | ${missed:>10,.0f} '
              f'{capture:>7.0%}')
    print(f'  {"TOTAL":>11} ${grand:>10,.0f}        | ${grand_best:>10,.0f}'
          f'        | ${grand_best - grand:>10,.0f} {grand / grand_best:>7.0%}')

    opening = sum(totals[b] for b in BUCKETS[:3])
    opening_best = sum(best[b] for b in BUCKETS[:3])
    print(f'\n  week 1 (ticks 0-75): earned {opening / grand:.0%} of our P&L, '
          f'but {opening_best / grand_best:.0%} of what was there')
    print(f'  we capture {opening / opening_best:.0%} of the opening against '
          f'{grand / grand_best:.0%} over the whole heat\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
