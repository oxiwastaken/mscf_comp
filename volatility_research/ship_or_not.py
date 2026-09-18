"""
Does the gross-capacity structure earn its place?

    python ship_or_not.py

It was worth +$7,830 a heat when first measured. Since then three things have
changed that argument, all in the same direction:

  * constrained to order sequences the server will actually accept, most of
    that gain came from wing rotations that needed a net buffer to be legal,
  * the backtest is measured at 1.46x live (91 matched heats, correlation
    0.89), so every backtest delta translates to about 0.68x in real money,
  * it raises per-heat standard deviation from about $60k to $74k.

The competition scores the MEAN OF TEN HEATS, so that is what this reports -
not the per-heat mean, which is not what anyone is paid on. A change that adds
mean and adds variance can still be the right bet, but only if the ten-heat
distribution says so, and this prints that distribution directly rather than
arguing about it.
"""

import statistics
import sys

import sweep

POLICY = 'ratchet+news'

# What the backtest scores has been 1.46x what the live server paid, over 91
# matched heats. Deltas are reported in both currencies so the decision is made
# on the one that matters.
LIVE_RATIO = 1 / 1.46

BASE = {'structure.WING_MAX_VEGA_RATIO': 1.0,
        'structure.WING_MIN_EXTRA_VEGA': 1000,
        'structure.WING_SYMMETRIC': False,
        'structure.WING_MIN_CONVICTION': 1.0,
        'structure.WING_NET_BUFFER': 50,
        'structure.WING_STALE_RATIO': 0.90,
        'STRIKE_SWITCH_DISTANCE': 1.5,
        'STAND_DOWN_CLOSES': True,
        'GAP_SMOOTHING': 0.10}

CANDIDATES = (
    ('straddle only', {'structure.WING_CONTRACTS': 0}),
    ('structure 750', {'structure.WING_CONTRACTS': 750}),
    ('structure 375', {'structure.WING_CONTRACTS': 375}),
)


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')
    print(f'  {"config":<16} {"mean":>11} {"sd":>10} {"worst":>10} '
          f'{"losers":>7} {"10-heat p05":>12} {"p50":>10} '
          f'{"P(>=120k)":>10} {"P(>=150k)":>10}')
    reference = None
    for name, settings in CANDIDATES:
        stats, rows = sweep.score(paths, dict(BASE, **settings), policy=POLICY)
        band = sweep.heats10(rows)
        values = [value for _tag, value, _f, _p in rows]
        losers = sum(1 for v in values if v < 0)
        print(f'  {name:<16} ${stats["mean"]:>10,.0f} ${stats["sd"]:>9,.0f} '
              f'${stats["worst"]:>9,.0f} {losers:>4}/{len(values):<3} '
              f'${band["p05"]:>11,.0f} ${band["p50"]:>9,.0f} '
              f'{sweep.p_target(rows, 120_000):>9.1%} '
              f'{sweep.p_target(rows, 150_000):>9.1%}')
        if reference is None:
            reference = rows
        else:
            delta, _stderr, t = sweep.paired(reference, rows)
            print(f'  {"":<16} vs straddle {delta:>+9,.0f} backtest '
                  f'(t {t:.1f})  ~{delta * LIVE_RATIO:>+8,.0f} live-adjusted')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
