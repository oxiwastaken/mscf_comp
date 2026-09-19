"""
Does hedging on market-IV greeks help, measured where timing cannot interfere?

    python market_iv_check.py

The live attempt at this produced a $101,481 fine and was reverted. But the
delta MATHS was verified correct: scored against the server's own charges,
greeks at each option's own market implied vol reproduce the server's delta to
a median of 5, while our forecast greeks are off by 1,200-2,900
(`delta_method.py`). And the failing heat contained a 30-second stall inside a
single step() - API calls taking 3-8 seconds each - which no other heat before
or since shows, and which coincided with a CPU-heavy analysis running on the
same machine.

So the live evidence is contaminated and cannot settle it. The backtest can:
it charges the delta penalty off the same greeks it hedges with, and it has no
wall clock to stall against. If hedging the number the server actually measures
is right, the modelled penalty should fall.

If this shows a gain, the change is worth retrying live - with nothing else
running on the machine. If it shows a loss, the revert stands on its own merits
and the mystery is closed.
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
           'SIGNAL_HALFLIFE_TICKS': 0,
           'STRUCTURE_OFF_AFTER': 0.80,
           'config.HEDGE_AT_MARKET_IV': False}


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')
    print(f'  {"greeks priced at":<24} {"mean":>12} {"vs ref":>10} {"t":>6} '
          f'{"penalty":>10} {"friction":>10} {"worst":>11}')
    reference = None
    for name, flag in (('our forecast (shipped)', False),
                       ('market implied vol', True)):
        stats, rows = sweep.score(
            paths, dict(SHIPPED, **{'config.HEDGE_AT_MARKET_IV': flag}),
            policy=POLICY)
        if reference is None:
            reference = rows
            delta = t = 0.0
        else:
            delta, _stderr, t = sweep.paired(reference, rows)
        print(f'  {name:<24} ${stats["mean"]:>11,.0f} {delta:>+10,.0f} {t:>6.1f} '
              f'${stats["penalty"]:>9,.0f} ${stats["friction"]:>9,.0f} '
              f'${stats["worst"]:>10,.0f}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
