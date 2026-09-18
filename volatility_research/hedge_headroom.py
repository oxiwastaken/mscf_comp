"""
Does the share limit ever stop the hedge?

    python hedge_headroom.py

The structure carries up to 1,750 at-the-money contracts against the straddle's
1,000, and the RTM limit stays at 50,000 shares. If the hedge runs out of room
the delta penalty is unbounded, so this measures the thing directly rather than
discounting the position size against a worst case that may never happen:
peak share position, and how often a hedge had to be cut short.
"""

import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

import backtest
import config
import sweep
import vol_engine
from replay import load

SHIPPING = {'structure.WING_CONTRACTS': 750,
            'structure.WING_MAX_VEGA_RATIO': 1.0,
            'structure.WING_MIN_EXTRA_VEGA': 1000,
            'structure.WING_SYMMETRIC': False}


def job(args):
    path, settings = args
    sweep.apply(settings)
    snapshots = load(path)

    peak = {'shares': 0, 'capped': 0, 'ticks': 0}
    real_trade_shares = backtest.Book.trade_shares

    def watched(self, spot, change):
        real_trade_shares(self, spot, change)
        held = abs(self.positions.get(config.UNDERLYING, 0))
        peak['shares'] = max(peak['shares'], held)
        if held >= config.LIMIT_RTM_GROSS:
            peak['capped'] += 1

    backtest.Book.trade_shares = watched
    try:
        book, _ = backtest.run(snapshots, 'ratchet+news')
    finally:
        backtest.Book.trade_shares = real_trade_shares
    return peak['shares'], peak['capped'], book.penalty


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings\n')
    off = dict(SHIPPING, **{'structure.WING_CONTRACTS': 0})
    for name, settings in (('straddle', off), ('structure', SHIPPING)):
        jobs = [(p, settings) for p in paths]
        with ProcessPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(job, jobs, chunksize=4))
        shares = [r[0] for r in rows]
        print(f'  {name:<10} peak RTM: mean {statistics.mean(shares):>8,.0f}   '
              f'worst {max(shares):>8,.0f}   of {config.LIMIT_RTM_GROSS:,} allowed')
        print(f'  {"":<10} heats that ever hit the share limit: '
              f'{sum(1 for r in rows if r[1]):>3} / {len(rows)}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
