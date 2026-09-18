"""
How long the trader takes to finish a resize, from its own log timestamps.

    python timing_from_log.py logs/auto_180124.log

Ticks are one second. Every order is a separate HTTP round trip, and the case
caps a single option order at 100 contracts - so a full-size resize of the
gross-capacity structure is at least 25 option orders plus the hedges between
them. If that takes longer than a tick, the later orders in the resize are
filling against a market that has already moved on, and the delta they create
goes unhedged across a tick boundary.

That would show up as live P&L below both the backtest and the order-accurate
replay, which is exactly the gap being chased: backtest $137,810, replay
$127,725, live ~$99,505 on the same kind of heats.

Reads only the trader's own log, so it measures the real thing rather than a
model of it.
"""

import collections
import glob
import os
import re
import sys
from datetime import datetime

STAMP = re.compile(r'^(\d\d:\d\d:\d\d)\s+(SENT|REJECTED|DRY)\s', re.M)
TICK = re.compile(r'^(\d\d:\d\d:\d\d)\s+tick\s+(\d+)/(\d+)', re.M)


def parse(path):
    """[(seconds, kind, tick)] for every logged line that matters."""
    events = []
    tick = None
    with open(path, encoding='utf-8', errors='ignore') as handle:
        for line in handle:
            match = TICK.match(line)
            if match:
                tick = int(match.group(2))
                events.append((match.group(1), 'tick', tick))
                continue
            match = STAMP.match(line)
            if match:
                events.append((match.group(1), match.group(2), tick))
    return events


def seconds(stamp):
    moment = datetime.strptime(stamp, '%H:%M:%S')
    return moment.hour * 3600 + moment.minute * 60 + moment.second


def main():
    paths = sys.argv[1:]
    if not paths:
        logs = sorted(glob.glob(os.path.join(os.path.dirname(__file__) or '.',
                                             'logs', 'auto_*.log')),
                      key=os.path.getmtime)
        paths = logs[-1:]

    for path in paths:
        events = parse(path)
        # Orders grouped by the tick they belong to, and how many WALL SECONDS
        # separate the first from the last.
        by_tick = collections.defaultdict(list)
        for stamp, kind, tick in events:
            if kind in ('SENT', 'REJECTED') and tick is not None:
                by_tick[tick].append(seconds(stamp))

        spans = [(tick, len(times), max(times) - min(times))
                 for tick, times in by_tick.items() if times]
        if not spans:
            print(f'  {os.path.basename(path):<22} no orders')
            continue

        overruns = [s for s in spans if s[2] >= 1]
        biggest = max(spans, key=lambda s: s[2])
        busiest = max(spans, key=lambda s: s[1])
        orders = sum(s[1] for s in spans)

        print(f'\n  {os.path.basename(path)}')
        print(f'    ticks that traded          {len(spans):>5}')
        print(f'    orders sent                {orders:>5}')
        print(f'    resizes spanning >= 1s     {len(overruns):>5}  '
              f'({len(overruns) / len(spans):.0%} of trading ticks)')
        print(f'    longest resize             {biggest[2]:>5}s  '
              f'({biggest[1]} orders at tick {biggest[0]})')
        print(f'    most orders in one tick    {busiest[1]:>5}  '
              f'(spanning {busiest[2]}s at tick {busiest[0]})')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
