"""
Are the wings systematically underpriced?

    python smile.py

The maker quotes all ten options at ONE implied vol with no smile - verified
live, the high-vega strikes cluster within 0.18%. That is the correct price only
if the terminal distribution is lognormal with a known variance.

It is not. Three of the four weekly volatilities are unannounced at any given
moment and are genuinely random, so integrated variance is a random variable,
not a constant. Mixing lognormals over an uncertain variance produces fatter
tails than any single lognormal - which means the true value of an out-of-the-
money option is ABOVE the flat-IV Black-Scholes price, and the more OTM it is
the larger the gap.

If that holds here, the wings are cheap and the ATM strikes are rich, as a
matter of pricing rather than forecasting. The edge would not require knowing
where volatility is going - only that nobody knows, which is guaranteed.

This measures it the direct way: buy one contract of every strike at a fixed
tick, hold to cash settlement, and compare what each paid off against what it
cost, averaged over every recorded heat. No model, no fitted parameter - just
the realised payoff of each strike against the maker's price for it.
"""

import argparse
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

import config
import sweep
import vol_engine
from replay import load, spot_of


def measure_one(args):
    """Payoff minus cost for one contract of each strike, on one heat."""
    path, entry_tick = args
    snapshots = load(path)
    entry = None
    for snap in snapshots:
        if snap['case'].get('tick', 0) >= entry_tick:
            entry = snap
            break
    if entry is None:
        return None
    final_spot = None
    for snap in reversed(snapshots):
        final_spot = spot_of(snap['securities'])
        if final_spot:
            break
    spot = spot_of(entry['securities'])
    if not spot or not final_spot:
        return None

    out = {}
    for row in entry['securities']:
        ticker = row['ticker'].upper()
        parsed = vol_engine.parse_option_ticker(ticker)
        if not parsed:
            continue
        flag, strike = parsed
        bid, ask = row.get('bid'), row.get('ask')
        if not bid or not ask:
            continue
        cost = ask * config.CONTRACT_MULTIPLIER + 1.0        # buy at the touch
        payoff = (max(0.0, final_spot - strike) if flag == 'c'
                  else max(0.0, strike - final_spot)) * config.CONTRACT_MULTIPLIER
        # Moneyness in strikes away from spot, signed towards OTM.
        distance = (strike - spot) if flag == 'c' else (spot - strike)
        out[ticker] = (round(distance), payoff - cost, cost)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tick', type=int, default=20,
                        help='tick to buy at (default 20, near max vega)')
    args = parser.parse_args()

    paths = sweep.usable_heats()
    with ProcessPoolExecutor() as pool:
        results = [r for r in pool.map(measure_one,
                                       [(p, args.tick) for p in paths],
                                       chunksize=4) if r]

    # Group by how far out of the money the option was when bought.
    buckets = defaultdict(list)
    for heat in results:
        for _ticker, (distance, edge, cost) in heat.items():
            buckets[distance].append((edge, cost))

    print(f'\n  {len(results)} heats, bought at tick {args.tick}, '
          f'held to settlement\n')
    print(f'  {"moneyness":>12} {"n":>5} {"mean P&L":>11} {"mean cost":>11} '
          f'{"return":>9} {"t":>7}')
    for distance in sorted(buckets):
        rows = buckets[distance]
        edges = [r[0] for r in rows]
        costs = [r[1] for r in rows]
        mean = statistics.mean(edges)
        stderr = statistics.pstdev(edges) / (len(edges) ** 0.5)
        label = ('ATM' if distance == 0
                 else f'{distance} OTM' if distance > 0 else f'{-distance} ITM')
        print(f'  {label:>12} {len(rows):>5} ${mean:>10,.0f} '
              f'${statistics.mean(costs):>10,.0f} '
              f'{mean / statistics.mean(costs):>8.1%} '
              f'{mean / stderr if stderr else 0:>7.1f}')
    print('\n  A positive t on the OTM rows means the wings really are cheap:')
    print('  buying them paid more than the maker charged, repeatedly.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
