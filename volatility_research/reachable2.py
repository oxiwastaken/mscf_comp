"""
Signal quality against P&L, with the gross-capacity structure ON.

    python reachable2.py

`reachable.py` asked this of the plain straddle and got a flat answer: perfect
ten-tick foresight of the maker's quote was worth about nothing, because the
position was capped by the net limit long before the view ran out. That is what
sent the work to the structure in the first place.

The question has to be asked again now that the cap is 39% higher, because the
two levers are not independent: a bigger position converts a better view into
more money, so signal quality can be worthless at one size and valuable at
another. If the curve is flat again, forecast work is finished for good and
everything left is structural.
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

WINGS = {'structure.WING_CONTRACTS': 562, 'structure.WING_MAX_VEGA_RATIO': 0.60,
         'structure.WING_SYMMETRIC': False}
OFF = {'structure.WING_CONTRACTS': 0, 'structure.WING_SYMMETRIC': False}


def our_forecast(snapshots):
    ticks_total = snapshots[0]['case'].get('ticks_per_period') or 300
    out = {}
    for snap in snapshots:
        tick = snap['case'].get('tick', 0)
        week_vols, _ = news_parser.parse_vol_news(snap.get('news') or [],
                                                  ticks_total)
        try:
            chain = vol_engine.build_chain(snap['securities'], tick,
                                           ticks_total, week_vols,
                                           mode='market')
        except ValueError:
            continue
        if chain['live'] and chain['forecast']:
            out[tick] = chain['forecast']
    return out


def job(args):
    path, blend, settings = args
    sweep.apply(settings)
    snapshots = load(path)
    ours = our_forecast(snapshots)
    truth = oracle.future_view(snapshots, 10)
    mixed = {t: (1 - blend) * ours[t] + blend * truth[t]
             for t in ours if t in truth}
    if not mixed:
        return None
    book, _ = backtest.run(snapshots, 'ratchet+news', forecast_path=mixed)
    return book.cash


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} heats, policy ratchet+news\n')
    print(f'  {"blend":>7} {"straddle":>14} {"with structure":>16} {"gain":>12}')
    for blend in (0.0, 0.25, 0.50, 0.75, 1.0):
        line = [f'  {blend:>7.2f}']
        means = []
        for settings in (OFF, WINGS):
            jobs = [(p, blend, settings) for p in paths]
            with ProcessPoolExecutor(max_workers=4) as pool:
                values = [v for v in pool.map(job, jobs, chunksize=4)
                          if v is not None]
            means.append(statistics.mean(values))
            line.append(f'${means[-1]:>13,.0f}' if len(means) == 1
                        else f'${means[-1]:>15,.0f}')
        line.append(f'{means[1] - means[0]:>+12,.0f}')
        print(''.join(line))
    print('\n  A flat column means the view is not what is limiting the P&L.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
