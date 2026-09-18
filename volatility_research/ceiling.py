"""
The true structural ceiling: perfect signal AND unrestricted execution.

    python ceiling.py

`oracle.py` gave a perfect forecast to the shipped strategy, which is still
news-gated and still ramps its size. That measures the ceiling of the current
CONFIGURATION, not of the structure. This removes the execution handicaps too,
so what is left is the most a 1,000-contract straddle book can earn on these
heats given a perfect view of where the maker's quote is going.

If that number is below the target, the target needs a different structure
rather than a better signal - and it is worth knowing which before spending a
night on either.
"""

import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

import backtest
import oracle
import sweep
from replay import load

HORIZONS = (10, 25, 40, 75, 150)
POLICIES = ('news', 'continuous')


def job(args):
    """Module-level so Windows spawn can pickle it."""
    path, horizon, policy = args
    sweep.apply(sweep.RAMP)
    snapshots = load(path)
    book, _ = backtest.run(snapshots, policy,
                           forecast_path=oracle.future_view(snapshots, horizon))
    return book.cash


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} heats, perfect foresight of the maker\'s quote\n')
    print(f'  {"policy":<12} {"horizon":>8} {"mean":>12} {"median":>12} {"best":>12}')
    best = None
    for policy in POLICIES:
        for horizon in HORIZONS:
            jobs = [(p, horizon, policy) for p in paths]
            with ProcessPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(job, jobs, chunksize=8))
            mean = statistics.mean(values)
            print(f'  {policy:<12} {horizon:>8} ${mean:>11,.0f} '
                  f'${statistics.median(values):>11,.0f} ${max(values):>11,.0f}')
            if best is None or mean > best[0]:
                best = (mean, policy, horizon)
    print(f'\n  ceiling: ${best[0]:,.0f}  ({best[1]}, horizon {best[2]})')
    print(f'  shipped model is $132,377, i.e. '
          f'{132377 / best[0]:.0%} of it\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
