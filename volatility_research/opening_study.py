"""
The opening: are we wrong early, or just not there?

    python opening_study.py

phases.py says ticks 25-50 capture 49% of what is available against 69% in the
best window, and the opening is the most expensive place in the heat to be idle
- vega scales with sqrt(T), so an option at tick 1 carries about four times the
vega it has at tick 280. A vol point captured early is worth several captured
late.

Two very different explanations, and they call for opposite fixes:

  ABSENT   we are flat, or small, while the opportunity passes. Fix by
           entering sooner or larger.
  WRONG    we are fully engaged and on the wrong side. Fix by waiting for a
           better signal, which means being MORE idle, not less.

This prints position size and sign accuracy tick by tick through the opening,
which separates them. The sign is scored against where the maker's quote
actually goes next, since that is what pays us.
"""

import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

import backtest
import news_parser
import oracle
import sweep
import vol_engine
from replay import load

POLICY = 'ratchet+news'
BUCKETS = ((0, 10), (10, 25), (25, 40), (40, 55), (55, 75), (75, 100),
           (100, 150), (150, 225), (225, 300))

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
    sizes = dict(book.sizes)

    # Where the maker's quote actually went, and what we thought at the time.
    ivs = oracle.iv_path(snapshots)
    truth = oracle.future_view(snapshots, 10)
    ticks_total = snapshots[0]['case'].get('ticks_per_period') or 300

    out = []
    for snap in snapshots:
        tick = snap['case'].get('tick', 0)
        if tick not in sizes or tick not in ivs or tick not in truth:
            continue
        week_vols, _ = news_parser.parse_vol_news(snap.get('news') or [],
                                                  ticks_total)
        try:
            chain = vol_engine.build_chain(snap['securities'], tick,
                                           ticks_total, week_vols,
                                           mode='market')
        except ValueError:
            continue
        if not chain['live'] or not chain['forecast']:
            continue
        held = sizes[tick]
        ours = chain['forecast'] - ivs[tick]
        moving = truth[tick] - ivs[tick]
        right = None
        if held and moving:
            right = (held > 0) == (moving > 0)
        out.append((tick, abs(held), right))
    return out


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')
    with ProcessPoolExecutor(max_workers=4) as pool:
        rows = [r for r in pool.map(job, paths, chunksize=4) if r]

    print(f'  {"ticks":>10} {"in a position":>14} {"mean size":>11} '
          f'{"sign right":>12}')
    for low, high in BUCKETS:
        sizes, rights = [], []
        for heat in rows:
            for tick, size, right in heat:
                if low <= tick < high:
                    sizes.append(size)
                    if right is not None:
                        rights.append(right)
        if not sizes:
            continue
        engaged = sum(1 for s in sizes if s) / len(sizes)
        accuracy = (sum(rights) / len(rights)) if rights else float('nan')
        print(f'  {low:>4}-{high:<5} {engaged:>13.0%} {statistics.mean(sizes):>11,.0f} '
              f'{accuracy:>11.1%}')
    print('\n  "in a position" is the fraction of ticks holding anything at '
          'all;\n  "mean size" is contracts per straddle leg, cap 500.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
