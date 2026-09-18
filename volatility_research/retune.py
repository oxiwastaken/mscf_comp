"""
Re-tune the sizing knobs now that the position is 1.75x bigger.

    python retune.py

Every threshold in the model was fitted against a 1,000-contract straddle. The
structure carries up to 2,500 gross, and the costs that those thresholds trade
off do not all scale the same way: a strike switch is now a 4,400-contract
round trip rather than 2,000, while the edge per vol point rose by about 29%.
An optimum fitted at the old size is not the optimum at this one.

Paired per heat against the shipped structure config, so `vs ref` is what
changing that one number would be worth.
"""

import sys

import sweep

POLICY = 'ratchet+news'

SHIPPING = {'structure.WING_CONTRACTS': 750,
            'structure.WING_MAX_VEGA_RATIO': 1.0,
            'structure.WING_MIN_EXTRA_VEGA': 1000,
            'structure.WING_SYMMETRIC': False,
            'structure.WING_MIN_CONVICTION': 1.0,
            'STRIKE_SWITCH_DISTANCE': 1.5,
            'STAND_DOWN_CLOSES': True}

AXES = {
    'STRIKE_SWITCH_DISTANCE': (1.0, 1.5, 2.0, 2.5),
    'EXIT_VOL_EDGE': (0.002, 0.003, 0.005, 0.007),
    'FULL_SIZE_VOL_EDGE': (0.006, 0.008, 0.012, 0.016),
    'REBALANCE_FRACTION': (0.15, 0.25, 0.40),
    'GAP_SMOOTHING': (0.10, 0.15, 0.25),
}


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')
    stats, ref_rows = sweep.score(paths, SHIPPING, policy=POLICY)
    print(f'  reference (shipped structure)  ${stats["mean"]:,.0f}   '
          f'worst ${stats["worst"]:,.0f}   friction ${stats["friction"]:,.0f}\n')

    for name, values in AXES.items():
        print(f'  {name}')
        for value in values:
            settings = dict(SHIPPING, **{name: value})
            stats, rows = sweep.score(paths, settings, policy=POLICY)
            delta, _stderr, t = sweep.paired(ref_rows, rows)
            flag = '  <- shipped' if value == SHIPPING.get(name) else ''
            print(f'    {str(value):>8}  ${stats["mean"]:>10,.0f}  '
                  f'{delta:>+9,.0f}  t {t:>5.1f}  '
                  f'worst ${stats["worst"]:>9,.0f}  '
                  f'friction ${stats["friction"]:>8,.0f}{flag}')
        print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
