"""
Score named configurations against the shipped one, paired, on every heat.

    python compare.py                 # the standing candidate list
    python compare.py --quick 40      # first 40 heats, for a fast read

Paired per heat, because heat-to-heat variance is $60k against a $132k mean and
an unpaired comparison of two means cannot resolve anything under about $9k.
Every config is scored on the SAME recordings, so the difference can be taken
heat by heat; `t` past ~2 is a real effect.

The candidates here are the two levers that survived the negative-results
archive in FINDINGS.md:

  gross capacity   the straddle is capped by the NET option limit while 1,500
                   contracts of GROSS sit idle; shorting the cheapest vega on
                   the board buys net room for more of the dearest. See
                   structure.py.

  cycling          the model turns over roughly twelve times its own position
                   per heat at $2 a contract, and a strike switch alone is a
                   2,000-contract round trip to move one strike closer to the
                   money - about 6% more vega. Both costs scale with position
                   size, so they matter MORE once gross capacity is in use.
"""

import argparse
import statistics
import sys

import sweep

POLICY = 'ratchet+news'

WINGS = {'structure.WING_CONTRACTS': 562, 'structure.WING_MAX_VEGA_RATIO': 0.60,
         'structure.WING_SYMMETRIC': False}
HOLD = {'STAND_DOWN_CLOSES': False}

BAND = {'config.HEDGE_TRIGGER_DELTA': 6900, 'config.HEDGE_TARGET_DELTA': 6500}

CANDIDATES = {
    'wings 562': dict(WINGS),
    'wings + settle': dict(WINGS, **HOLD),
    'wings + free band': dict(WINGS, **BAND),
    'wings + conviction 0.8': dict(WINGS,
                                   **{'structure.WING_MIN_CONVICTION': 0.8}),
    'wings + vega gate 800': dict(WINGS,
                                  **{'structure.WING_MAX_VEGA_RATIO': 1.0,
                                     'structure.WING_MIN_EXTRA_VEGA': 800}),
    'wings + vega gate 1200': dict(WINGS,
                                   **{'structure.WING_MAX_VEGA_RATIO': 1.0,
                                      'structure.WING_MIN_EXTRA_VEGA': 1200}),
    'wings 750 + vega gate 1200': dict(WINGS,
                                       **{'structure.WING_CONTRACTS': 750,
                                          'structure.WING_MAX_VEGA_RATIO': 1.0,
                                          'structure.WING_MIN_EXTRA_VEGA': 1200}),
    'all of it': dict(WINGS, **HOLD, **BAND,
                      **{'structure.WING_MAX_VEGA_RATIO': 1.0,
                         'structure.WING_MIN_EXTRA_VEGA': 1200}),
}

# Everything is scored against the shipped model, with the structure off.
BASE = {'structure.WING_CONTRACTS': 0, 'STRIKE_SWITCH_DISTANCE': 1.5,
        'structure.WING_SYMMETRIC': False, 'STAND_DOWN_CLOSES': True}


def show(name, stats, rows, ref_rows=None):
    line = (f'  {name:<22} ${stats["mean"]:>11,.0f} ')
    if ref_rows is not None:
        delta, _stderr, t = sweep.paired(ref_rows, rows)
        line += f'{delta:>+10,.0f} {t:>6.1f} '
    else:
        line += f'{"":>10} {"":>6} '
    line += (f'${stats["worst"]:>10,.0f} ${stats["friction"]:>9,.0f} '
             f'{sweep.p_target(rows):>10.1%}')
    print(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--quick', type=int, help='use only the first N heats')
    args = parser.parse_args()

    paths = sweep.usable_heats()
    if args.quick:
        paths = paths[:args.quick]
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')
    print(f'  {"config":<22} {"mean":>12} {"vs ref":>10} {"t":>6} '
          f'{"worst":>11} {"friction":>10} {"P(10>=150k)":>11}')

    stats, ref_rows = sweep.score(paths, BASE, policy=POLICY)
    show('shipped (reference)', stats, ref_rows)

    for name, settings in CANDIDATES.items():
        stats, rows = sweep.score(paths, dict(BASE, **settings), policy=POLICY)
        show(name, stats, rows, ref_rows)
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
