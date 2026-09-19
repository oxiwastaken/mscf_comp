"""
Anatomy of a heat's fines: when, how long, and what the book was doing.

    python penalty_anatomy.py logs/auto_214940.log

`penalty_from_log.py` gives the total. This gives the shape, which is what
decides whether anything can be done about it:

  SPIKY     a few short episodes, each a gamma event on a position that was
            sitting still. Fixable only by carrying less gamma at those
            moments - and every blunt version of that has been measured and
            costs more than it saves.

  CHRONIC   many ticks just over the line. That is a band problem, and the
            band is a parameter.

  RESIZE    breaches that coincide with the tick's own option orders, i.e. the
            delta a resize carries before its hedge lands. That is an
            execution problem and execution is ours to fix.

The distinction matters because the three have completely different answers and
the totals look identical.
"""

import re
import sys

OVER = re.compile(r'^(\d\d:\d\d:\d\d)\s+delta ([+-][\d,]+) OVER LIMIT, -\$([\d,]+)/s')
TICK = re.compile(r'^(\d\d:\d\d:\d\d)\s+tick\s+(\d+)/(\d+).*?held ([+-]?\d+)\s+target ([+-]?\d+)')
SENT = re.compile(r'^(\d\d:\d\d:\d\d)\s+(?:SENT|REJECTED)\s+(?:BUY|SELL)\s+([\d,]+)\s+(\S+)')


def number(text):
    return int(text.replace(',', ''))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print('usage: penalty_anatomy.py <trader log>')
        return 1

    events = []          # (tick, kind, payload)
    tick = None
    traded_at = set()    # ticks that sent option orders
    for line in open(path, encoding='utf-8', errors='ignore'):
        match = TICK.match(line)
        if match:
            tick = int(match.group(2))
            events.append((tick, 'tick', (int(match.group(4)),
                                          int(match.group(5)))))
            continue
        match = SENT.match(line)
        if match and tick is not None:
            if match.group(3).upper() != 'RTM':
                traded_at.add(tick)
            continue
        match = OVER.match(line)
        if match and tick is not None:
            events.append((tick, 'over', (number(match.group(2)),
                                          number(match.group(3)))))

    breaches = [(t, payload) for t, kind, payload in events if kind == 'over']
    if not breaches:
        print('  never outside the band')
        return 0

    # Group breaches into episodes: consecutive-ish ticks belong together.
    episodes = []
    for tick_no, (delta, rate) in breaches:
        if episodes and tick_no - episodes[-1][-1][0] <= 3:
            episodes[-1].append((tick_no, delta, rate))
        else:
            episodes.append([(tick_no, delta, rate)])

    total = sum(rate for _t, (_d, rate) in breaches)
    print(f'\n  {len(breaches)} ticks outside the band in {len(episodes)} '
          f'episodes, ~${total:,} total\n')
    print(f'  {"ticks":>12} {"len":>4} {"worst delta":>13} {"cost":>10} '
          f'{"resizing?":>10}')
    for episode in sorted(episodes, key=lambda e: -sum(r for _t, _d, r in e)):
        first, last = episode[0][0], episode[-1][0]
        worst = max(episode, key=lambda e: abs(e[1]))
        cost = sum(rate for _t, _d, rate in episode)
        resizing = any(t in traded_at for t, _d, _r in episode)
        print(f'  {first:>5}-{last:<6} {len(episode):>4} {worst[1]:>+13,} '
              f'${cost:>9,} {"yes" if resizing else "no":>10}')

    spiky = sum(sum(r for _t, _d, r in e) for e in episodes if len(e) <= 3)
    resize_cost = sum(sum(r for _t, _d, r in e) for e in episodes
                      if any(t in traded_at for t, _d, _r in e))
    print(f'\n  short episodes (<=3 ticks): ${spiky:,} '
          f'({spiky / total:.0%} of the fine)')
    print(f'  episodes that were resizing: ${resize_cost:,} '
          f'({resize_cost / total:.0%} of the fine)\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
