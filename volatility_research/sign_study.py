"""
WHERE is the sign wrong?

    python sign_study.py

Size is not the constraint - we are pinned at the 1,000-contract limit on 86% of
the ticks we hold anything - so the only thing left that can improve P&L is the
SIGN of the view, which is right about 84% of the time.

That 16% is not necessarily fixable, but it is certainly not uniform. This buckets
sign accuracy by how large the signal is and by how far into the heat we are, to
find out whether the errors cluster anywhere we could act on: if accuracy is poor
only where the gap is small, a threshold helps; if it is poor everywhere, nothing
does.

Weighted by the size of the move, because being wrong on a 4-point move costs far
more than being wrong on a 0.2-point one, and a raw hit rate hides that entirely.
"""

import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

import oracle
import signal_study
import sweep


def job(path):
    return signal_study.features(path)


def report(rows, label, key, edges):
    print(f'\n  by {label}')
    print(f'  {"bucket":>14} {"n":>8} {"sign right":>11} {"move-weighted":>14} '
          f'{"mean |move|":>12}')
    for lo, hi in zip(edges, edges[1:]):
        sub = [r for r in rows if lo <= key(r) < hi]
        if len(sub) < 100:
            continue
        right = [r for r in sub if (r['ema'] > 0) == (r['move'] > 0)]
        won = sum(abs(r['move']) for r in right)
        total = sum(abs(r['move']) for r in sub)
        print(f'  {lo:>6.3f}-{hi:<7.3f} {len(sub):>8,} '
              f'{len(right) / len(sub):>10.1%} {won / total if total else 0:>13.1%} '
              f'{statistics.mean([abs(r["move"]) for r in sub]) * 100:>11.2f}%')


def main():
    paths = sweep.usable_heats()
    with ProcessPoolExecutor(max_workers=3) as pool:
        chunks = list(pool.map(job, paths, chunksize=8))
    rows = [r for c in chunks for r in c if r.get('move') is not None]
    print(f'\n  {len(rows):,} tick observations')

    overall = [r for r in rows if (r['ema'] > 0) == (r['move'] > 0)]
    won = sum(abs(r['move']) for r in overall)
    total = sum(abs(r['move']) for r in rows)
    print(f'  overall sign right {len(overall) / len(rows):.1%}, '
          f'move-weighted {won / total:.1%}')

    report(rows, 'signal size |ema gap|', lambda r: abs(r['ema']),
           (0.0, 0.002, 0.005, 0.010, 0.020, 0.040, 9.0))
    report(rows, 'ticks since the last release', lambda r: r['since_news'],
           (0, 5, 15, 40, 80, 999))
    report(rows, 'tick in the heat', lambda r: r['tick'] / 300.0,
           (0.0, 0.25, 0.5, 0.75, 0.9, 1.01))
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
