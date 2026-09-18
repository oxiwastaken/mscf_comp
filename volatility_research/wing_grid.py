"""
Tune the gross-capacity structure: size, vega gate, and leg symmetry.

    python wing_grid.py
    python wing_grid.py --quick 40

Scored paired against the shipped straddle on identical heats. The reference
line is the model as it trades today, so every number in the `vs ref` column is
what shipping that row would be worth per heat.
"""

import argparse
import sys

import sweep

POLICY = 'ratchet+news'
BASE = {'structure.WING_CONTRACTS': 0, 'structure.WING_SYMMETRIC': True}


def variants():
    """
    Size against the gate that decides when the structure is worth its fees.

    The gate is in dollars of extra vega per vol point, because that is what
    the structure buys; the ratio gate it replaces could not tell a good early
    trade from a bad late one. Opening costs about $2 a contract on 2W
    contracts and unwinding costs the same, so the break-even convergence is
    (4 * W) / gate vol points - roughly 2.8 points at W=562, gate 800.
    """
    for contracts in (468, 562, 656, 750):
        for gate in (600, 800, 1000, 1200):
            yield (f'{contracts:>3} contracts, gate ${gate:,}',
                   {'structure.WING_CONTRACTS': contracts,
                    'structure.WING_MAX_VEGA_RATIO': 1.0,
                    'structure.WING_MIN_EXTRA_VEGA': gate})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quick', type=int)
    args = parser.parse_args()

    paths = sweep.usable_heats()
    if args.quick:
        paths = paths[:args.quick]
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')

    stats, ref_rows = sweep.score(paths, BASE, policy=POLICY)
    print(f'  reference (straddle only)  ${stats["mean"]:,.0f}   '
          f'worst ${stats["worst"]:,.0f}   friction ${stats["friction"]:,.0f}\n')
    print(f'  {"variant":<26} {"mean":>12} {"vs ref":>10} {"t":>6} '
          f'{"worst":>11} {"friction":>10} {"P(10>=150k)":>11}')

    best = None
    for name, settings in variants():
        stats, rows = sweep.score(paths, dict(BASE, **settings), policy=POLICY)
        delta, _stderr, t = sweep.paired(ref_rows, rows)
        print(f'  {name:<26} ${stats["mean"]:>11,.0f} {delta:>+10,.0f} {t:>6.1f} '
              f'${stats["worst"]:>10,.0f} ${stats["friction"]:>9,.0f} '
              f'{sweep.p_target(rows):>10.1%}')
        if best is None or stats['mean'] > best[0]:
            best = (stats['mean'], name)
    print(f'\n  -> best {best[1]} at ${best[0]:,.0f}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
