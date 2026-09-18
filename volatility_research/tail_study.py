"""
What goes wrong in the worst heats?

    python tail_study.py

Per-heat standard deviation is about $60k and every configuration ever tested
lands within $5k of that, including maximum aggression and minimum engagement.
That is opportunity variance - how badly the maker misprices vol on the day -
and it is not ours to remove.

The LEFT TAIL is a different question. If the worst heats share a cause -
a penalty blow-up, a view that flipped repeatedly, a heat where we were simply
on the wrong side - then capping that cause cuts the downside without paying
for it in the mean. If they are just heats where the maker never mispriced,
there is nothing to fix and the number is the number.

This prints the bottom and top heats side by side with the diagnostics that
would separate those two stories.
"""

import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

import backtest
import sweep
from replay import load

POLICY = 'ratchet+news'

SHIPPED = {'structure.WING_CONTRACTS': 750,
           'structure.WING_MAX_VEGA_RATIO': 1.0,
           'structure.WING_MIN_EXTRA_VEGA': 1000,
           'structure.WING_SYMMETRIC': False,
           'structure.WING_MIN_CONVICTION': 1.0,
           'structure.WING_NET_BUFFER': 50,
           'structure.WING_STALE_RATIO': 0.90,
           'GAP_SMOOTHING': 0.10}


def job(path):
    sweep.apply(SHIPPED)
    snapshots = load(path)
    book, _marks = backtest.run(snapshots, POLICY)
    sizes = [size for _tick, size in book.sizes]
    flips = sum(1 for a, b in zip(sizes, sizes[1:])
                if a and b and (a > 0) != (b > 0))
    flat = sum(1 for s in sizes if not s) / max(len(sizes), 1)
    return {
        'tag': sweep.tag_of(path),
        'pnl': book.cash,
        'penalty': book.penalty,
        'friction': book.fees + book.spread,
        'contracts': book.contracts,
        'flips': flips,
        'flat': flat,
    }


def show(title, rows):
    print(f'\n  {title}')
    print(f'    {"heat":<18} {"P&L":>11} {"penalty":>9} {"friction":>9} '
          f'{"contracts":>10} {"flips":>6} {"flat":>6}')
    for row in rows:
        print(f'    {row["tag"]:<18} ${row["pnl"]:>10,.0f} '
              f'${row["penalty"]:>8,.0f} ${row["friction"]:>8,.0f} '
              f'{row["contracts"]:>10,} {row["flips"]:>6} {row["flat"]:>5.0%}')


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings, policy {POLICY}')
    with ProcessPoolExecutor(max_workers=4) as pool:
        rows = [r for r in pool.map(job, paths, chunksize=4) if r]
    rows.sort(key=lambda r: r['pnl'])

    n = max(len(rows) // 10, 5)
    worst, best = rows[:n], rows[-n:]
    show(f'the {n} worst heats', worst)
    show(f'the {n} best heats', best)

    print('\n  averages')
    for name, group in (('worst', worst), ('best', best), ('all', rows)):
        print(f'    {name:<6} P&L ${statistics.mean(r["pnl"] for r in group):>10,.0f}   '
              f'penalty ${statistics.mean(r["penalty"] for r in group):>8,.0f}   '
              f'friction ${statistics.mean(r["friction"] for r in group):>8,.0f}   '
              f'flips {statistics.mean(r["flips"] for r in group):>5.1f}   '
              f'flat {statistics.mean(r["flat"] for r in group):>4.0%}')
    values = [r['pnl'] for r in rows]
    print(f'\n  mean ${statistics.mean(values):,.0f}   '
          f'sd ${statistics.pstdev(values):,.0f}   '
          f'losing heats {sum(1 for v in values if v < 0)}/{len(values)}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
