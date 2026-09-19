"""
Get out before the pin.

    python pin_check.py

Live evidence, heat 21:49, ticks 243-256. Spot oscillating on the K52 strike
with a short 875/875 straddle, twelve ticks from stand-down:

    tick 244  delta -17,382  BUY  15,382
    tick 247  delta +19,290  SELL 17,290
    tick 248  delta -23,734  BUY  21,734   bought at 52.43
    tick 249  delta +14,106  SELL 12,106   sold at 52.12

Short gamma at the strike at expiry. Delta reverses every tick, the hedge
chases it, and we buy high and sell low all the way. That episode alone cost
$6,066 in fines, and the whipsaw on top is a straight P&L loss - being short a
straddle while realised vol arrives is exactly what that position risks.

NO_NEW_AFTER stands the book down at 0.90 of the heat, and that number was
fitted when the book was 1,000 contracts. The gross-capacity structure makes it
1,750, so the same pin now costs 1.75x. This asks whether the threshold should
move, and separately whether it should be the STRUCTURE that comes off first -
it is the optional 750 contracts and it carries no view of its own.
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
           'STRUCTURE_OFF_AFTER': 1.0,
           'NO_NEW_AFTER': 0.90}

LIVE_RATIO = 1 / 1.20

# Standing the whole book down early loses - the late window pays despite the
# pin. Dropping only the STRUCTURE is what wins, so this maps that curve
# finely: a real effect should be smooth, and 0.90 should read as zero because
# the whole book already stands down there.
CANDIDATES = [('shipped (structure to the end)', {})]
for after in (0.72, 0.76, 0.80, 0.84, 0.88, 0.90):
    CANDIDATES.append((f'structure off at {after:.2f}',
                       {'STRUCTURE_OFF_AFTER': after}))


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')
    print(f'  {"config":<30} {"mean":>11} {"vs ref":>10} {"t":>6} '
          f'{"penalty":>9} {"worst":>11} {"live-adj":>10}')
    reference = None
    for name, override in CANDIDATES:
        stats, rows = sweep.score(paths, dict(SHIPPED, **override),
                                  policy=POLICY)
        if reference is None:
            reference = rows
            delta = t = 0.0
        else:
            delta, _stderr, t = sweep.paired(reference, rows)
        print(f'  {name:<30} ${stats["mean"]:>10,.0f} {delta:>+10,.0f} {t:>6.1f} '
              f'${stats["penalty"]:>8,.0f} ${stats["worst"]:>10,.0f} '
              f'{delta * LIVE_RATIO:>+10,.0f}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
