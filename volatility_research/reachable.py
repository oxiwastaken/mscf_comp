"""
How good would the signal have to be to hit $150k a heat?

    python reachable.py

Everything else is now ruled out. Size is not the constraint - 86% of the ticks
we hold anything, we are already pinned at the 1,000-contract limit. Time spent
flat is not the constraint either; staying in longer loses money in every
variant tested. What is left is the SIGN of the view, which is right 84.2% of
the time against the oracle's 100%.

So this blends our own forecast toward the oracle's - a signal that is right a
controllable fraction of the time - and maps P&L against it. That turns "is
$150k reachable" from an argument into a reading off a curve: either some
attainable level of signal quality gets there, or none does.

Blend 0.0 is exactly what we trade today; 1.0 is perfect foresight of the
maker's next move.
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


def our_forecast(snapshots):
    """Our own forecast at every tick, as the trader computes it."""
    ticks_total = snapshots[0]['case'].get('ticks_per_period') or 300
    out = {}
    for snap in snapshots:
        tick = snap['case'].get('tick', 0)
        week_vols, _ = news_parser.parse_vol_news(snap.get('news') or [],
                                                  ticks_total)
        try:
            chain = vol_engine.build_chain(snap['securities'], tick, ticks_total,
                                           week_vols, mode='market')
        except ValueError:
            continue
        if chain['live'] and chain['forecast']:
            out[tick] = chain['forecast']
    return out


def job(args):
    path, blend = args
    sweep.apply(sweep.RAMP)
    snapshots = load(path)
    ours = our_forecast(snapshots)
    truth = oracle.future_view(snapshots, 10)
    mixed = {t: (1 - blend) * ours[t] + blend * truth[t]
             for t in ours if t in truth}
    if not mixed:
        return None
    book, _ = backtest.run(snapshots, 'ratchet+news', forecast_path=mixed)

    # How often the blended signal points the right way, for calibration.
    ivs = oracle.iv_path(snapshots)
    scored = [(mixed[t] - ivs[t], truth[t] - ivs[t])
              for t in mixed if t in ivs and truth[t] != ivs[t]]
    right = sum(1 for a, b in scored if (a > 0) == (b > 0))
    return book.cash, (right / len(scored) if scored else 0.0)


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} heats, policy ratchet+news\n')
    print(f'  {"blend":>7} {"sign right":>12} {"mean P&L":>12} {"median":>12}')
    for blend in (0.0, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0):
        jobs = [(p, blend) for p in paths]
        with ProcessPoolExecutor(max_workers=4) as pool:
            rows = [r for r in pool.map(job, jobs, chunksize=8) if r]
        values = [r[0] for r in rows]
        accuracy = statistics.mean([r[1] for r in rows])
        print(f'  {blend:>7.2f} {accuracy:>11.1%} ${statistics.mean(values):>11,.0f} '
              f'${statistics.median(values):>11,.0f}')
    print('\n  $150,000 a heat is the target. Read across for the signal '
          'accuracy\n  that would be needed to get there.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
