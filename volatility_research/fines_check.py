"""
Cut the fines, without giving back more than they cost.

    python fines_check.py

Profit is P&L minus the CRO's fine, and the fine is $0.10 per delta per second
outside +/-7,000. Live, the model pays about $6,600 a heat (penalty_from_log.py
over the real trader logs); the backtest models $14,839, so anything measured
here is roughly twice its live size and has to be discounted before believing
it.

The lever under test is where to hedge back TO once already outside the limit.
The standing buffer of 2,000 exists so ordinary drift does not drag a hedge
after it at $0.02 a share. Past the limit that reasoning inverts: clearing an
extra 2,000 of delta costs $40, and carrying it costs $200 a second.

Reported with penalty and share cost split out, because a change that only
moves cost from one column to the other is not a change.
"""

import sys

import sweep

POLICY = 'ratchet+news'

# Live penalty is ~$6,600 against the backtest's ~$14,839, so backtest penalty
# savings are worth roughly this much in real money.
PENALTY_REALISM = 6_600 / 14_839

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
           'config.HEDGE_URGENT_TARGET': None}

CANDIDATES = (
    ('shipped (buffer 2,000)', {}),
    ('flatten fully when over', {'config.HEDGE_URGENT_TARGET': 0}),
    ('to 1,000 when over', {'config.HEDGE_URGENT_TARGET': 1_000}),
    ('to -2,000 (overshoot)', {'config.HEDGE_URGENT_TARGET': -2_000}),
    ('trigger 4,000, flatten', {'config.HEDGE_TRIGGER_DELTA': 4_000,
                                'config.HEDGE_URGENT_TARGET': 0}),
    ('trigger 6,000, flatten', {'config.HEDGE_TRIGGER_DELTA': 6_000,
                                'config.HEDGE_URGENT_TARGET': 0}),
)


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')
    print(f'  {"config":<26} {"mean":>11} {"vs ref":>9} {"t":>6} '
          f'{"penalty":>9} {"friction":>10} {"live-adj":>10}')
    reference = None
    ref_penalty = None
    for name, override in CANDIDATES:
        stats, rows = sweep.score(paths, dict(SHIPPED, **override),
                                  policy=POLICY)
        if reference is None:
            reference = rows
            ref_penalty = stats['penalty']
            delta = t = 0.0
            adjusted = 0.0
        else:
            delta, _stderr, t = sweep.paired(reference, rows)
            # The part of the gain that comes from penalty is overstated by the
            # backtest's penalty model; the rest (share cost) is not.
            saved = ref_penalty - stats['penalty']
            adjusted = (delta - saved) + saved * PENALTY_REALISM
        print(f'  {name:<26} ${stats["mean"]:>10,.0f} {delta:>+9,.0f} {t:>6.1f} '
              f'${stats["penalty"]:>8,.0f} ${stats["friction"]:>9,.0f} '
              f'{adjusted:>+10,.0f}')
    print('\n  live-adj discounts the penalty component to its real size.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
