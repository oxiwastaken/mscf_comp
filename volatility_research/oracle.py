"""
How much money is actually in a heat?

    python oracle.py

Answers the ceiling question directly instead of arguing about it. An ORACLE
trader is given the whole news feed from tick 1 - every weekly volatility,
including the three not yet announced - and otherwise trades exactly the same
straddle strategy through exactly the same fill model. Its P&L is the most this
strategy could earn on that heat with a perfect forecast and no other change.

That number bounds what better forecasting can buy. If the oracle earns barely
more than we do, the forecast is not the problem and the limits are. If it earns
far more, the forecast is the whole game.

No new strategy code is needed. The forecast reads the news feed, so replacing
each tick's feed with the FINAL one makes the trader omniscient about
volatility while leaving fills, fees, hedging and limits untouched.
"""

import argparse
import statistics
import sys

import autotrader as at
import backtest
import sweep
from replay import load


def iv_path(snapshots):
    """The market's own average implied vol at every tick of the heat."""
    import news_parser
    import vol_engine
    ticks_total = snapshots[0]['case'].get('ticks_per_period') or 300
    path = {}
    for snap in snapshots:
        tick = snap['case'].get('tick', 0)
        week_vols, _ = news_parser.parse_vol_news(snap['news'], ticks_total)
        try:
            chain = vol_engine.build_chain(snap['securities'], tick, ticks_total,
                                           week_vols, mode='market')
        except ValueError:
            continue
        if chain['live'] and chain['market_avg_iv']:
            path[tick] = chain['market_avg_iv']
    return path


def future_view(snapshots, horizon):
    """
    Forecast = the market's OWN implied vol `horizon` ticks from now.

    This is the strongest signal the strategy can act on: not what volatility
    truly is, but where the maker's quote is about to go. The gap it produces is
    exactly the move that is about to happen, so its P&L is the ceiling of this
    straddle strategy under the position limits.
    """
    path = iv_path(snapshots)
    ticks = sorted(path)
    if not ticks:
        return {}
    last = ticks[-1]
    return {t: path.get(min(t + horizon, last), path[t]) for t in ticks}


def score_one(args):
    """One heat, honest vs each oracle. Module-level so it can be pickled."""
    path, settings = args
    sweep.apply(settings)
    snapshots = load(path)
    honest, _ = backtest.run(snapshots, 'news')
    # Oracle 1: knows every weekly sigma from tick 1, but still resizes on the
    # real release schedule, so foresight is the only thing that differs.
    seer, _ = backtest.run(snapshots, 'news',
                           forecast_news=snapshots[-1]['news'])
    # Oracle 2: knows where the maker's quote will be shortly. The ceiling.
    quotes, _ = backtest.run(snapshots, 'news',
                             forecast_path=future_view(snapshots, 40))
    return sweep.tag_of(path), honest.cash, seer.cash, quotes.cash


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()

    paths = sweep.usable_heats()
    if args.limit:
        paths = paths[:args.limit]

    from concurrent.futures import ProcessPoolExecutor
    jobs = [(p, sweep.RAMP) for p in paths]
    with ProcessPoolExecutor() as pool:
        rows = list(pool.map(score_one, jobs, chunksize=4))

    honest = [r[1] for r in rows]
    seer = [r[2] for r in rows]
    quotes = [r[3] for r in rows]
    print(f'\n  {len(rows)} heats\n')
    for label, values in (('tuned model', honest),
                          ('knows every weekly sigma', seer),
                          ('knows the future QUOTE', quotes)):
        print(f'  {label:<26} mean ${statistics.mean(values):>11,.0f}   '
              f'best ${max(values):>11,.0f}   '
              f'>=$500k {sum(1 for v in values if v >= 500_000):>2}   '
              f'>=$1m {sum(1 for v in values if v >= 1_000_000):>2}')
    print(f'\n  capture vs the quote oracle: '
          f'{statistics.mean(honest) / statistics.mean(quotes):.0%}\n')

    best = sorted(rows, key=lambda r: -r[3])[:8]
    print(f'  {"heat":>10} {"tuned":>12} {"sigma seer":>12} {"quote seer":>12}')
    for tag, h, s, q in best:
        print(f'  {tag:>10} ${h:>11,.0f} ${s:>11,.0f} ${q:>11,.0f}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
