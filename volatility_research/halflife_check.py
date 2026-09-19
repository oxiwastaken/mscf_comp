"""
Does decaying conviction with the age of the signal pay?

    python halflife_check.py

The case says the maker "learns about the underlying volatility - at least
until a new shock occurs". So our edge is not a property of the gap, it is a
property of the gap's AGE, and the model currently treats a sixty-tick-old
disagreement exactly like one that landed on this tick.

What that costs is measurable (opening_study.py, 160 recordings):

    ticks 0-10    94.1% right      ticks 40-55   84.8%
    ticks 10-25   91.3%            ticks 55-75   63.8%   <- the vacuum
    ticks 25-40   97.6%            ticks 75-100  91.2%   <- week 2 lands

Ticks 55-75 are an information vacuum - week one's vol was announced at tick 1,
the maker has had fifty ticks to converge, week one's remaining variance is
nearly nil, and week two is not announced until tick 75. phases.py scores that
window at -$679 of AVAILABLE P&L, and the model is in a position for 68% of it
at 310 contracts a leg.

A halflife of N ticks halves the effective gap every N ticks since the last
release, so the position fades out of a stale view on its own and the exit
threshold eventually closes it. Short halflives are aggressive; a long one is
almost the current behaviour.
"""

import sys

import sweep

POLICY = 'ratchet+news'

SHIPPED = {'structure.WING_CONTRACTS': 750,
           'structure.WING_MAX_VEGA_RATIO': 1.0,
           'structure.WING_MIN_EXTRA_VEGA': 1000,
           'structure.WING_SYMMETRIC': False,
           'structure.WING_MIN_CONVICTION': 1.0,
           'structure.WING_NET_BUFFER': 50,
           'structure.WING_STALE_RATIO': 0.90,
           'STRIKE_SWITCH_DISTANCE': 1.5,
           'STAND_DOWN_CLOSES': True,
           'GAP_SMOOTHING': 0.10,
           'SIGNAL_HALFLIFE_TICKS': 0}

# Live is about 0.83x the backtest on this model (validate_build.py: 1.20x,
# correlation 0.98), so a backtest delta is worth roughly this much in money.
LIVE_RATIO = 1 / 1.20


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')
    print(f'  {"halflife":>10} {"mean":>12} {"vs shipped":>12} {"t":>6} '
          f'{"worst":>11} {"friction":>10} {"live-adj":>10}')
    stats, reference = sweep.score(paths, SHIPPED, policy=POLICY)
    print(f'  {"off":>10} ${stats["mean"]:>11,.0f} {0:>+12,.0f} {0.0:>6.1f} '
          f'${stats["worst"]:>10,.0f} ${stats["friction"]:>9,.0f} {0:>+10,.0f}')

    for halflife in (15, 25, 40, 60, 100, 160):
        stats, rows = sweep.score(
            paths, dict(SHIPPED, **{'SIGNAL_HALFLIFE_TICKS': halflife}),
            policy=POLICY)
        delta, _stderr, t = sweep.paired(reference, rows)
        print(f'  {halflife:>10} ${stats["mean"]:>11,.0f} {delta:>+12,.0f} '
              f'{t:>6.1f} ${stats["worst"]:>10,.0f} ${stats["friction"]:>9,.0f} '
              f'{delta * LIVE_RATIO:>+10,.0f}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
