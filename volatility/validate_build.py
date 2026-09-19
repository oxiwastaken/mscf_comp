"""
Backtest against live, for ONE build.

    python validate_build.py 180124        # heats tagged at or after this

`sweep.py --validate` pools every heat in results.csv, which spans a day of
different builds and some heats the trader joined half-way through. That gave a
1.46x backtest-to-live ratio, and it is not a fair reading of the model that is
running now: it mixes in builds with known bugs and partial heats.

This restricts the comparison to heats traded by one build, which is the only
version of the question worth answering before a competition.
"""

import statistics
import sys

import sweep


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else '0'
    live = {tag: nlv for tag, nlv in sweep.live_results().items()
            if tag >= since}
    paths = [p for p in sweep.usable_heats() if sweep.tag_of(p) in live]
    if not paths:
        print(f'  no recordings match live heats at or after {since}')
        return 1

    print(f'\n  {len(paths)} heats traded at or after tag {since}\n')
    _stats, rows = sweep.score(paths, {}, policy='news')
    pairs = [(live[tag], sim) for tag, sim, _f, _p in rows if tag in live]
    actual = [p[0] for p in pairs]
    simulated = [p[1] for p in pairs]

    print(f'  heats matched         {len(pairs)}')
    print(f'  live mean             ${statistics.mean(actual):>11,.0f}')
    print(f'  backtest mean         ${statistics.mean(simulated):>11,.0f}')
    print(f'  ratio                  {statistics.mean(simulated) / statistics.mean(actual):>11.2f}x')
    if len(pairs) > 2:
        print(f'  correlation            {statistics.correlation(actual, simulated):>11.2f}')
    print(f'  live sd               ${statistics.pstdev(actual):>11,.0f}')
    print(f'  live worst            ${min(actual):>11,.0f}   '
          f'losing heats {sum(1 for a in actual if a < 0)}/{len(actual)}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
