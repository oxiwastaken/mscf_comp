"""
Price each safety guard separately.

    python guard_check.py

Three changes went in together after the live replay found a book the server
would have rejected, and together they turned +$7,830 a heat into -$3,672. At
least one of them is not paying for itself. They are:

  keep-or-stand-down   never swap a wing from one leg to another, because at
                       full size that has no legal order sequence. This one is
                       a correctness fix and is not optional.

  share-usage breaker  stand the structure down once the hedge has spent 80%
                       of the share limit.

  shrink ungate        allow a risk-reducing trade without waiting for news,
                       so a stand-down can actually happen when it is decided.

The breaker and the ungate interact: the breaker has no hysteresis, so with the
ungate the book can stand down at 40,001 shares and rebuild at 39,999, paying
about $3,000 each way.
"""

import sys

import sweep

POLICY = 'ratchet+news'

BASE = {'structure.WING_CONTRACTS': 750,
        'structure.WING_MAX_VEGA_RATIO': 1.0,
        'structure.WING_MIN_EXTRA_VEGA': 1000,
        'structure.WING_SYMMETRIC': False,
        'structure.WING_MIN_CONVICTION': 1.0,
        'STRIKE_SWITCH_DISTANCE': 1.5,
        'STAND_DOWN_CLOSES': True,
        'GAP_SMOOTHING': 0.10}

OFF = dict(BASE, **{'structure.WING_CONTRACTS': 0})

CANDIDATES = [('straddle only', dict(OFF))]
for buffer in (0, 50, 100):
    for stale in (0.90, 1.01):
        CANDIDATES.append(
            (f'buffer {buffer}, stale {stale:.2f}',
             dict(BASE, **{'structure.WING_NET_BUFFER': buffer,
                           'structure.WING_STALE_RATIO': stale,
                           'structure.WING_KEEP_FRACTION': 0.34})))


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')
    print(f'  {"config":<24} {"mean":>12} {"vs straddle":>12} {"t":>6} '
          f'{"worst":>11} {"friction":>10}')
    reference = None
    for name, settings in CANDIDATES:
        stats, rows = sweep.score(paths, settings, policy=POLICY)
        if reference is None:
            reference = rows
            delta = t = 0.0
        else:
            delta, _stderr, t = sweep.paired(reference, rows)
        print(f'  {name:<24} ${stats["mean"]:>11,.0f} {delta:>+12,.0f} {t:>6.1f} '
              f'${stats["worst"]:>10,.0f} ${stats["friction"]:>9,.0f}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
