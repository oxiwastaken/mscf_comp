"""
The FINE the server actually charged, per heat, from the recordings.

    python true_fines.py 12

The trader endpoint reports `total_fines` and record.py has been capturing it
every tick, so the authoritative number has been sitting in the recordings all
along. Everything before this was estimated by scraping the trader's own log
for the rate it printed when it noticed it was outside the band - which counts
ONE second per logged tick and therefore undercounts badly, because the fine
accrues every second the book is outside, including the seconds inside a
multi-second resize and the ticks where no hedge fired at all.

Reported next to NLV so the fine can be read as what it is: a direct deduction
from the score.
"""

import glob
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def fine_of(path):
    """(final fine, peak |delta|, ticks outside the band) for one recording."""
    fine = 0.0
    nlv = None
    ticks_out = 0
    peak = 0.0
    with open(path, encoding='utf-8', errors='ignore') as handle:
        for line in handle:
            try:
                snap = json.loads(line)
            except ValueError:
                continue
            trader = snap.get('trader') or {}
            if 'total_fines' in trader:
                fine = max(fine, float(trader['total_fines'] or 0.0))
            if trader.get('nlv') is not None:
                nlv = float(trader['nlv'])
    return fine, nlv, ticks_out, peak


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    paths = sorted(glob.glob(os.path.join(HERE, 'heats', 'live_*.jsonl')),
                   key=os.path.getmtime)[-limit:]
    print(f'\n  {"recording":<22} {"final NLV":>12} {"SERVER FINE":>13} '
          f'{"fine as % of NLV":>18}')
    fines, nlvs = [], []
    for path in paths:
        fine, nlv, _out, _peak = fine_of(path)
        if nlv is None:
            continue
        fines.append(fine)
        nlvs.append(nlv)
        share = f'{fine / nlv:.0%}' if nlv else 'n/a'
        print(f'  {os.path.basename(path):<22} ${nlv:>11,.0f} ${fine:>12,.0f} '
              f'{share:>18}')
    if fines:
        print(f'\n  mean fine ${statistics.mean(fines):,.0f}   '
              f'worst ${max(fines):,.0f}   '
              f'mean NLV ${statistics.mean(nlvs):,.0f}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
