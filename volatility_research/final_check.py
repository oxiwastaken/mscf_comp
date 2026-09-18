"""
The shipping candidate, end to end, against the model as it stood this morning.

    python final_check.py

One table, paired per heat, with the ten-heat distribution that the goal is
actually stated in: $150,000 average per trader across ten heats.
"""

import statistics
import sys

import sweep

POLICY = 'ratchet+news'

MORNING = {'structure.WING_CONTRACTS': 0, 'GAP_SMOOTHING': 0.15,
           'STRIKE_SWITCH_DISTANCE': 1.5, 'STAND_DOWN_CLOSES': True,
           'structure.WING_SYMMETRIC': False,
           'structure.WING_MIN_EXTRA_VEGA': 1000,
           'structure.WING_MAX_VEGA_RATIO': 1.0,
           'structure.WING_MIN_CONVICTION': 1.0}
STRUCTURE = dict(MORNING, **{'structure.WING_CONTRACTS': 750})
SLOWER = dict(STRUCTURE, **{'GAP_SMOOTHING': 0.10})
BOTH = dict(SLOWER, **{'FULL_SIZE_VOL_EDGE': 0.012})

CANDIDATES = (
    ('this morning', MORNING),
    ('+ gross capacity', STRUCTURE),
    ('+ slower smoothing', SLOWER),
    ('+ wider full-size ramp', BOTH),
)


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')
    print(f'  {"config":<24} {"mean":>12} {"vs start":>10} {"t":>6} '
          f'{"worst":>11} {"P(10 >= 150k)":>14}')
    reference = None
    for name, settings in CANDIDATES:
        stats, rows = sweep.score(paths, settings, policy=POLICY)
        if reference is None:
            reference = rows
            delta = t = 0.0
        else:
            delta, _stderr, t = sweep.paired(reference, rows)
        band = sweep.heats10(rows)
        print(f'  {name:<24} ${stats["mean"]:>11,.0f} {delta:>+10,.0f} {t:>6.1f} '
              f'${stats["worst"]:>10,.0f} {sweep.p_target(rows):>13.1%}')
        if name == CANDIDATES[-1][0]:
            print(f'\n  ten-heat average, shipping candidate: '
                  f'p05 ${band["p05"]:,.0f}   p50 ${band["p50"]:,.0f}   '
                  f'p95 ${band["p95"]:,.0f}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
