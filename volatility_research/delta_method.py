"""
Which way of pricing delta matches the SERVER's?

    python delta_method.py ../volatility/heats/live_224008.jsonl

The server charges (|delta| - 7,000) * $0.10 a second and record.py captures
`total_fines` every tick, so the fine increment inverts to the MAGNITUDE of the
delta the server saw. That is ground truth, and it can referee this directly.

Three candidates:

  forecast     greeks at our blended forecast vol. What ships today. Known to
               disagree with the server - delta_truth.py measured a median gap
               of 19,621 on one heat.

  own IV       greeks at each row's OWN solved market implied vol. Tried live
               and it was far worse: $101,481 of fines against $46,379. The
               suspicion is that a low-vega wing's IV is not determined by its
               two-cent quote - MAX_IV_SPREAD exists for exactly that reason -
               so its delta comes out as noise.

  chain IV     greeks at the chain's vega-weighted average market IV, which is
               the number the filter already exists to make trustworthy.

Only ticks where a fine was actually charged can be scored, since only those
reveal the server's number. The winner is whichever method's |delta| sits
closest to it.
"""

import json
import os
import statistics
import sys

import config
import news_parser
import vol_engine
from py_vollib.black_scholes.greeks import analytical


def priced_delta(snap, tick, ticks_total, vol_for):
    """Portfolio delta with every option's greek priced at vol_for(row)."""
    try:
        chain = vol_engine.build_chain(snap['securities'], tick, ticks_total,
                                       {}, mode='market')
    except (ValueError, KeyError):
        return None
    if not chain['live']:
        return None
    spot = chain['spot']
    t_years = chain['t_years']
    if t_years <= 0:
        return None
    total = float(chain['underlying_position'])
    for row in chain['rows']:
        if not row['position']:
            continue
        vol = vol_for(row, chain)
        if not vol:
            return None
        try:
            delta = analytical.delta(row['flag'], spot, row['strike'], t_years,
                                     config.RISK_FREE_RATE, vol)
        except Exception:
            return None
        total += row['position'] * delta * config.CONTRACT_MULTIPLIER
    return total


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print('usage: delta_method.py <recording>')
        return 1

    methods = {
        'forecast': lambda row, chain: chain['forecast'],
        'own IV': lambda row, chain: row['market_iv'] or chain['market_avg_iv'],
        'chain IV': lambda row, chain: chain['market_avg_iv'],
    }
    errors = {name: [] for name in methods}

    previous = 0.0
    scored = 0
    for line in open(path, encoding='utf-8', errors='ignore'):
        try:
            snap = json.loads(line)
        except ValueError:
            continue
        case = snap.get('case') or {}
        ticks_total = case.get('ticks_per_period') or 300
        tick = case.get('tick', 0)
        fine = float((snap.get('trader') or {}).get('total_fines') or 0.0)
        charged = max(fine - previous, 0.0)
        previous = fine
        if charged <= 0:
            continue
        server = 7_000 + charged / config.DELTA_PENALTY_RATE
        scored += 1
        for name, vol_for in methods.items():
            value = priced_delta(snap, tick, ticks_total, vol_for)
            if value is not None:
                errors[name].append(abs(abs(value) - server))

    print(f'\n  {os.path.basename(path)}: {scored} charged ticks scored '
          f'against the server\n')
    print(f'  {"method":<12} {"median error":>14} {"mean error":>12} {"n":>5}')
    for name in methods:
        values = errors[name]
        if not values:
            print(f'  {name:<12} {"no data":>14}')
            continue
        print(f'  {name:<12} {statistics.median(values):>14,.0f} '
              f'{statistics.mean(values):>12,.0f} {len(values):>5}')
    print('\n  error is |our |delta| - server |delta||, in delta. Lower is '
          'closer to\n  what the CRO is actually measuring.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
