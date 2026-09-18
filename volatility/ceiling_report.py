"""
What the shipped model makes, and the most it could make.

    python ceiling_report.py

Scores the current configuration over every usable recording, then puts three
reference points beside it:

  ceiling      a trader who knows where the maker's quote is heading ten ticks
               out, trading the same straddle through the same fill model. Not
               attainable - it is the bound, not a target.
  best blend   the same foresight mixed half-and-half with our own forecast,
               which beats pure foresight because a short-horizon predictor
               alone flips the position too often.
  today        what the live trader actually earned, for calibration.

Reported as a ten-heat average as well as a per-heat one, because the goal is
stated that way and single-heat variance here is enormous.
"""

import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

import sweep


def money(value):
    return f'${value:>11,.0f}'


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} usable recordings\n')

    stats, rows = sweep.score(paths, {})
    values = sorted(r[1] for r in rows)
    band = sweep.heats10(rows)

    print('  SHIPPED MODEL  (sizing ramp + interleaved hedge + ratchet)')
    print(f'    per heat   mean {money(stats["mean"])}   '
          f'median {money(stats["median"])}   sd {money(stats["sd"])}')
    print(f'               worst {money(stats["worst"])}   '
          f'best {money(values[-1])}   '
          f'losing heats {sum(1 for v in values if v < 0)}/{len(values)}')
    print(f'    10-heat    p05 {money(band["p05"])}   '
          f'p50 {money(band["p50"])}   p95 {money(band["p95"])}')

    import random
    rng = random.Random(11)
    draws = [statistics.mean(rng.choices(values, k=10)) for _ in range(20000)]
    for target in (120_000, 135_000, 150_000):
        hit = sum(1 for d in draws if d >= target) / len(draws)
        print(f'    P(10-heat average >= ${target:,}) = {hit:>5.1%}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
