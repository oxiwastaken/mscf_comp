"""
Is the binding constraint our SIGN or our SIZE?

    python sizing_study.py

The ceiling work says a perfect view of the maker's next move earns $187k a heat
against our $132k. Two very different things could explain that gap:

  SIZE  - we are right about direction but too small when it matters, or
  SIGN  - we are the wrong way round often enough to bleed the difference.

They call for opposite work. If we are already pinned at the contract limit
whenever we hold anything, then the magnitude of the signal is irrelevant and
only its sign can be improved - and every "better signal" that merely rescales
the gap is wasted effort. That is worth knowing before building one.

Compares our signal against the realised 10-tick move, tick by tick, over every
recording.
"""

import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

import autotrader as at
import backtest
import config
import oracle
import sweep
from replay import load


def run_study():
    paths = sweep.usable_heats()
    with ProcessPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(measure, paths, chunksize=8))
    rows = [r for r in rows if r]
    print(f'\n  {len(rows)} heats\n')
    for key, label in (('at_cap', 'ticks at the full contract limit'),
                       ('in_market', 'ticks holding anything'),
                       ('sign_right', 'ticks the sign was right'),
                       ('sign_right_big', 'sign right when the move was big')):
        values = [r[key] for r in rows]
        print(f'  {label:<38} {statistics.mean(values):>8.1%}')
    print()
    return 0


def measure(path):
    """Fraction of in-market ticks at the cap, and how often the sign is right."""
    sweep.apply(sweep.RAMP)
    snapshots = load(path)
    truth = oracle.future_view(snapshots, 10)
    ivs = oracle.iv_path(snapshots)
    book, _ = backtest.run(snapshots, 'news')
    sizes = getattr(book, 'sizes', None)
    if not sizes:
        return None
    cap = config.LIMIT_OPT_NET // 2
    in_market = [(t, s) for t, s in sizes if s]
    if not in_market:
        return None
    moves = {t: truth[t] - ivs[t] for t in ivs if t in truth}
    scored = [(t, s) for t, s in in_market if t in moves]
    if not scored:
        return None
    big = sorted(scored, key=lambda p: -abs(moves[p[0]]))[:max(1, len(scored) // 4)]
    return {
        'at_cap': sum(1 for _t, s in in_market if abs(s) >= cap) / len(in_market),
        'in_market': len(in_market) / max(len(sizes), 1),
        'sign_right': sum(1 for t, s in scored
                          if (s > 0) == (moves[t] > 0)) / len(scored),
        'sign_right_big': sum(1 for t, s in big
                              if (s > 0) == (moves[t] > 0)) / len(big),
    }


if __name__ == '__main__':
    sys.exit(run_study())
