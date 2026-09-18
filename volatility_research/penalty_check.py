"""
Does the bigger position ever run the RTM hedge out of room?

    python penalty_check.py

The structure carries up to 1,750 at-the-money contracts against the straddle's
1,000, so the delta to hedge grows with it while the share limit does not:
50,000 shares is 50,000 shares. If the hedge cannot keep up, it shows as delta
penalty - $0.10 per delta per second - and that is the one cost that can run
away faster than the edge it is protecting.
"""

import sys

import sweep

SHIPPING = {'structure.WING_CONTRACTS': 750,
            'structure.WING_MAX_VEGA_RATIO': 1.0,
            'structure.WING_MIN_EXTRA_VEGA': 1000,
            'structure.WING_SYMMETRIC': False,
            'STRIKE_SWITCH_DISTANCE': 1.5,
            'STAND_DOWN_CLOSES': True}


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings\n')
    off = dict(SHIPPING, **{'structure.WING_CONTRACTS': 0})
    for name, settings in (('straddle', off), ('structure', SHIPPING)):
        stats, rows = sweep.score(paths, settings, policy='ratchet+news')
        worst_penalty = max(r[3] for r in rows)
        print(f'  {name:<10} mean ${stats["mean"]:>10,.0f}   '
              f'penalty mean ${stats["penalty"]:>7,.0f}   '
              f'penalty worst ${worst_penalty:>9,.0f}   '
              f'friction ${stats["friction"]:>8,.0f}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
