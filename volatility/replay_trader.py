"""
Drive the REAL autotrader through recorded heats and check it never sends an
order the server would refuse.

    python replay_trader.py                     # every usable recording
    python replay_trader.py heats/live_*.jsonl  # a chosen set
    python replay_trader.py --verbose

The offline suite checks `chunks()` and `option_orders()` on synthetic
fixtures. This runs the whole loop - `step()`, the sizing ramp, the ratchet,
the structure, the chunking and the hedge - against real recorded market data,
with a fake client that fills every order immediately, and asserts the
invariants that matter live after EVERY SINGLE ORDER:

    gross options  <= 2,500 contracts
    net options    <= 1,000 contracts
    |RTM|          <= 50,000 shares
    every option order <= 100 contracts, every share order <= 10,000

The limits are enforced on submission, not at the end of the tick, so an
intermediate breach is a rejected order and a book left lopsided - which is how
the delta penalty starts. A backtest cannot catch that: it moves the whole
position at once and never sees the states in between.
"""

import argparse
import glob
import os
import sys

import autotrader
import config
import structure
import vol_engine
from replay import load


class ReplayClient:
    """Feeds recorded ticks to the trader and fills its orders immediately."""

    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.index = 0
        self.positions = {}
        self.orders = []
        self.breaches = []

    # ------------------------------------------------------------- the feed
    def case(self):
        snap = self.snapshots[self.index]
        return dict(snap['case'], status='ACTIVE')

    def securities(self):
        """The recorded market, with OUR simulated positions written in."""
        snap = self.snapshots[self.index]
        return [dict(sec, position=self.positions.get(sec['ticker'].upper(), 0))
                for sec in snap['securities']]

    def news(self):
        return self.snapshots[self.index]['news']

    def limits(self):
        return [{'name': 'Limit-OPT', 'gross_limit': config.LIMIT_OPT_GROSS,
                 'net_limit': config.LIMIT_OPT_NET}]

    # ---------------------------------------------------------- the fills
    def send_in_chunks(self, ticker, action, quantity, max_chunk):
        """
        Fill the order the way the real client sends it: split into legal
        chunks, one at a time, checking the limits after every chunk.

        Splitting here rather than filling the whole quantity at once is the
        point - `rit_api.send_in_chunks` does the same, and the limits are
        enforced per submitted order, so the intermediate states are real.
        """
        ticker = ticker.upper()
        cap = (config.MAX_TRADE_RTM if ticker == config.UNDERLYING
               else config.MAX_TRADE_OPTIONS)
        step = min(int(max_chunk), cap)
        remaining = int(quantity)
        while remaining > 0:
            lot = min(step, remaining)
            remaining -= lot
            signed = lot if action == 'BUY' else -lot
            self.positions[ticker] = self.positions.get(ticker, 0) + signed
            self.orders.append((ticker, action, lot))
            self.check()
        return []

    def check(self):
        """Both option limits and the share limit, after every single order."""
        options = {t: q for t, q in self.positions.items()
                   if t != config.UNDERLYING}
        gross, net = structure.limits_used(options)
        tick = self.snapshots[self.index]['case'].get('tick', 0)
        if gross > config.LIMIT_OPT_GROSS:
            self.breaches.append(f'tick {tick}: gross {gross} > '
                                 f'{config.LIMIT_OPT_GROSS}  {options}')
        if net > config.LIMIT_OPT_NET:
            self.breaches.append(f'tick {tick}: net {net} > '
                                 f'{config.LIMIT_OPT_NET}  {options}')
        shares = abs(self.positions.get(config.UNDERLYING, 0))
        if shares > config.LIMIT_RTM_GROSS:
            self.breaches.append(f'tick {tick}: RTM {shares} > '
                                 f'{config.LIMIT_RTM_GROSS}')


def replay(path, verbose=False):
    """Run one heat. Returns (breaches, orders, peak delta, peak shares)."""
    snapshots = load(path)
    client = ReplayClient(snapshots)
    trader = autotrader.AutoTrader(client, live=True, verbose=verbose)
    if not verbose:
        trader.log = lambda _text: None

    peak_delta = peak_shares = 0.0
    for index in range(len(snapshots)):
        client.index = index
        trader.step()
        snap = snapshots[index]
        tick = snap['case'].get('tick', 0)
        total = snap['case'].get('ticks_per_period') or 300
        try:
            chain = vol_engine.build_chain(client.securities(), tick, total,
                                           {}, mode=config.TAIL_VOL_MODE)
        except ValueError:
            continue
        peak_delta = max(peak_delta, abs(chain['portfolio_delta']))
        peak_shares = max(peak_shares,
                          abs(client.positions.get(config.UNDERLYING, 0)))
    return client.breaches, client.orders, peak_delta, peak_shares


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('paths', nargs='*')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--limit', type=int, help='only the first N heats')
    parser.add_argument('--no-structure', action='store_true',
                        help='plain straddle, to attribute what the structure costs')
    args = parser.parse_args()
    if args.no_structure:
        structure.WING_CONTRACTS = 0

    files = []
    for pattern in args.paths or ['heats/*.jsonl']:
        files.extend(sorted(glob.glob(pattern)))
    files = [f for f in files if os.path.getsize(f) > 100_000]
    if args.limit:
        files = files[:args.limit]
    if not files:
        print('no recordings found')
        return 1

    print(f'\n  replaying the live trader through {len(files)} heats\n')
    failed = 0
    for path in files:
        breaches, orders, peak_delta, peak_shares = replay(path, args.verbose)
        name = os.path.basename(path)
        status = 'OK  ' if not breaches else 'FAIL'
        print(f'  {status} {name:<34} {len(orders):>5,} orders   '
              f'peak delta {peak_delta:>8,.0f}   peak RTM {peak_shares:>7,.0f}')
        for breach in breaches[:3]:
            print(f'         {breach}')
        failed += bool(breaches)

    print(f'\n  {len(files) - failed}/{len(files)} heats clean\n')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
