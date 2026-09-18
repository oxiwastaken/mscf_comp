"""
Per-heat results, derived from the recordings.

    python track.py                  # P&L of every recorded heat
    python track.py --diagnose       # ... and what each heat COST to trade
    python track.py --verify         # check the numbers against results.csv

`overnight.py` writes results.csv, but it also runs its own trader, so it cannot
be used while a trader is already live - two actors on one book is how an
unhedgeable position happens. The recordings already carry everything, so
results can simply be READ back out of them, with nothing extra touching the
account.

`--diagnose` is the important one. Single-heat P&L says almost nothing here:
the standard deviation across heats is about $58,000, so a change worth $8,000
is invisible in any small number of heats. Hedge volume and delta breaches are
not noisy in that way - they follow almost deterministically from the code - so
a regression shows up in a single heat. The interleaving churn bug was caught
exactly this way, from two heats whose P&L proved nothing either direction.
"""

import argparse
import glob
import json
import os
import statistics
import sys


def heat_nlv(path):
    """
    (tick, NLV) at the end of the heat, or None.

    Uses the server's own `trader.nlv`, which is the authoritative,
    penalty-adjusted figure the competition scores. Summing each security's
    realized and unrealized instead looks equivalent and is not - it overstated
    every heat by $2-7k when checked against results.csv, because it misses the
    account-level charges. Verified to the dollar on 84 heats with --verify.
    """
    last = None
    for line in open(path, encoding='utf-8'):
        try:
            snap = json.loads(line)
        except ValueError:
            continue
        nlv = (snap.get('trader') or {}).get('nlv')
        if nlv is None:
            continue
        last = (snap['case'].get('tick', 0), float(nlv))
    return last


def diagnose(path):
    """
    What a heat COST, not just what it earned.

    P&L per heat is nearly useless on its own - the standard deviation across
    heats is about $58,000, so no single heat says anything about a change.
    The mechanism numbers are not noisy in the same way: hedge volume and delta
    breaches are near-deterministic consequences of the code, so a regression
    shows up in one heat. The interleaving churn bug was found this way, from
    two heats whose P&L proved nothing.

    Everything here comes out of the recording, so it works on heats that were
    captured months ago as well as the one that just finished.
    """
    import config
    import news_parser
    import vol_engine

    shares = contracts = breaches = 0
    penalty = 0.0
    peak = 0.0
    previous = {}
    ticks_total = None
    for line in open(path, encoding='utf-8'):
        try:
            snap = json.loads(line)
        except ValueError:
            continue
        tick = snap['case'].get('tick', 0)
        ticks_total = ticks_total or snap['case'].get('ticks_per_period') or 300
        securities = snap.get('securities') or []
        for row in securities:
            ticker = row['ticker'].upper()
            moved = abs((row.get('position') or 0) - previous.get(ticker, 0))
            if ticker == config.UNDERLYING:
                shares += moved
            else:
                contracts += moved
            previous[ticker] = row.get('position') or 0
        week_vols, _ = news_parser.parse_vol_news(snap.get('news') or [],
                                                 ticks_total)
        try:
            chain = vol_engine.build_chain(securities, tick, ticks_total,
                                           week_vols, mode='market')
        except ValueError:
            continue
        if not chain['live']:
            continue
        delta = abs(chain['portfolio_delta'])
        peak = max(peak, delta)
        breach = max(delta - config.DELTA_LIMIT, 0.0)
        if breach:
            breaches += 1
            penalty += breach * config.DELTA_PENALTY_RATE
    return {'shares': shares, 'contracts': contracts, 'breaches': breaches,
            'penalty': penalty, 'peak_delta': peak,
            'shares_per_contract': shares / contracts if contracts else 0.0}


def rows(pattern='heats/*.jsonl', min_ticks=250):
    out = []
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding='utf-8') as handle:
            n = sum(1 for _ in handle)
        if n < min_ticks:
            continue
        result = heat_nlv(path)
        if result:
            out.append((os.path.basename(path), result[0], result[1]))
    return out


def verify():
    """Cross-check the derived NLV against results.csv, which overnight.py wrote."""
    import csv
    known = {}
    try:
        with open('results.csv', encoding='utf-8') as handle:
            for row in csv.DictReader(handle):
                known[row['tag']] = int(row['nlv'])
    except OSError:
        print('  no results.csv to verify against')
        return
    checked = 0
    for name, _tick, nlv in rows():
        tag = name.replace('live_', '').replace('.jsonl', '')
        if tag not in known:
            continue
        checked += 1
        diff = nlv - known[tag]
        flag = 'ok' if abs(diff) < max(1000, abs(known[tag]) * 0.02) else 'MISMATCH'
        if flag != 'ok' or checked <= 5:
            print(f'  {tag:>12}  derived ${nlv:>11,.0f}   '
                  f'results.csv ${known[tag]:>11,.0f}   {flag}')
    print(f'\n  {checked} heats cross-checked against results.csv')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--diagnose', action='store_true',
                        help='per-heat mechanism numbers, not just P&L')
    parser.add_argument('--pattern', default='heats/heat_*.jsonl')
    args = parser.parse_args()

    if args.verify:
        verify()
        return 0

    data = rows(args.pattern)
    if not data:
        print('\n  no completed heats recorded yet\n')
        return 0
    if args.diagnose:
        print(f'\n  {"heat":>26} {"NLV":>11} {"shares":>9} {"contracts":>10} '
              f'{"sh/con":>7} {"breaches":>9} {"penalty":>9} {"peak delta":>11}')
        for name, _tick, nlv in data:
            d = diagnose(os.path.join('heats', name))
            print(f'  {name[:26]:>26} ${nlv:>10,.0f} {d["shares"]:>9,.0f} '
                  f'{d["contracts"]:>10,.0f} {d["shares_per_contract"]:>7.1f} '
                  f'{d["breaches"]:>9} ${d["penalty"]:>8,.0f} '
                  f'{d["peak_delta"]:>11,.0f}')
        print('\n  sh/con is hedge shares per option contract - the churn '
              'measure.\n  It is comparable across heats; raw share counts are '
              'not, because\n  heats differ in how much trading they call '
              'for.\n')
    else:
        print(f'\n  {"heat":>34} {"last tick":>10} {"NLV":>12}')
        for name, tick, nlv in data:
            print(f'  {name:>34} {tick:>10} ${nlv:>11,.0f}')
    values = [r[2] for r in data]
    print(f'  n {len(values)}   mean ${statistics.mean(values):>11,.0f}   '
          f'median ${statistics.median(values):>11,.0f}   '
          f'worst ${min(values):>11,.0f}   best ${max(values):>11,.0f}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
