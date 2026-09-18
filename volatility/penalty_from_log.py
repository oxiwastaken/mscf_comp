"""
What a live heat actually paid in delta penalty, read off the trader's log.

    python penalty_from_log.py logs/auto_180124.log
    python penalty_from_log.py            # the most recent log

The backtest charges one second of the pre-hedge delta at every resize, which
is a model, not a measurement - and it is a model of behaviour the interleaved
hedge was built to avoid. The trader logs the rate whenever it is outside the
band, once a tick, and ticks are a second, so summing those rates is the
closest thing to the real number available without the server's own accounting.
"""

import glob
import os
import re
import sys

LINE = re.compile(r'delta ([+-][\d,]+) OVER LIMIT, -\$([\d,]+)/s')


def scan(path):
    rates = []
    with open(path, encoding='utf-8', errors='ignore') as handle:
        for line in handle:
            match = LINE.search(line)
            if match:
                rates.append((int(match.group(1).replace(',', '')),
                              int(match.group(2).replace(',', ''))))
    return rates


def main():
    paths = sys.argv[1:]
    if not paths:
        logs = sorted(glob.glob(os.path.join(os.path.dirname(__file__) or '.',
                                             'logs', 'auto_*.log')),
                      key=os.path.getmtime)
        paths = logs[-1:]
    for path in paths:
        rates = scan(path)
        name = os.path.basename(path)
        if not rates:
            print(f'  {name:<22} never outside the delta band')
            continue
        worst = max(rates, key=lambda r: r[1])
        print(f'  {name:<22} {len(rates):>3} ticks outside the band   '
              f'worst delta {worst[0]:+,} at ${worst[1]:,}/s   '
              f'total ~${sum(r[1] for r in rates):,}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
