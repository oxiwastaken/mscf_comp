"""
Price the late-heat gamma problem the live run exposed.

    python gamma_check.py

Live, build da50ee6, heat at 17:47: the book sat completely still for ten ticks
- no option orders at all - while RTM moved from 51.62 to 52.35, and portfolio
delta swung from +12,053 to -28,710 on that move alone. The hedge acts once a
tick, so that second cost $2,171; 29 ticks in the heat were over the limit.

That is gamma, and the structure makes it 1.75x worse because it is 1.75x the
contracts. Three ways to spend less on it, measured against the shipped build:

  taper          scale the position by sqrt(remaining/total), holding gamma
                 roughly constant instead of contract count
  structure off  stand the gross-capacity structure down early, keeping the
                 straddle - the structure is the optional 75%
  tighter band   hedge back sooner, paying share fees to avoid penalty

The penalty column is the one that matters here; the mean is what decides it.
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
           'GAMMA_TAPER': 0.0,
           'STRUCTURE_OFF_AFTER': 1.0,
           'structure.WING_MAX_EXTRA_GAMMA': 0.0}

# The taper is dead: it saves $11k of penalty and costs $20k of vega. The time
# cutoff is the live lever - vega falls with sqrt(T) while gamma rises with
# 1/sqrt(T), so the structure's whole value decays monotonically through a heat
# while its cost grows. This finds where those cross.
CANDIDATES = [('shipped (da50ee6)', {}),
              ('time cutoff 0.60 (spiky)', {'STRUCTURE_OFF_AFTER': 0.60})]
for gate in (5_000, 8_000, 12_000, 18_000, 25_000, 40_000):
    CANDIDATES.append((f'extra-gamma gate {gate:,}',
                       {'structure.WING_MAX_EXTRA_GAMMA': gate}))


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')
    print(f'  {"config":<26} {"mean":>12} {"vs shipped":>12} {"t":>6} '
          f'{"penalty":>10} {"worst":>11}')
    reference = None
    for name, override in CANDIDATES:
        stats, rows = sweep.score(paths, dict(SHIPPED, **override),
                                  policy=POLICY)
        if reference is None:
            reference = rows
            delta = t = 0.0
        else:
            delta, _stderr, t = sweep.paired(reference, rows)
        print(f'  {name:<26} ${stats["mean"]:>11,.0f} {delta:>+12,.0f} {t:>6.1f} '
              f'${stats["penalty"]:>9,.0f} ${stats["worst"]:>10,.0f}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
