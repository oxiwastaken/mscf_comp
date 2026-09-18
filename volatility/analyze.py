"""
Decompose a heat's result: how much went to friction versus the actual bet.

    python analyze.py logs/auto_233406.log
    python analyze.py logs/auto_233406.log --nlv -15153

Reads an autotrader log and totals what was traded and what it cost. The point
is to separate two very different explanations for a losing heat:

  * FRICTION - commission and spread paid on churn. Fixable by trading less.
  * DIRECTION - the volatility view was simply wrong. Fixable only by a better
    forecast, which is what replay.py is for.

Costs are charged at the rates the live server reports ($1.00/contract,
$0.01/share), not the case PDF's doubled figures.
"""

import argparse
import re
import sys
from collections import Counter

import config

FEE_PER_CONTRACT = 1.00
FEE_PER_SHARE = 0.01
# Crossing from mid to the touch on a 2-cent market costs one cent, which is
# $1.00 on a 100-share contract and $0.01 on a share.
SPREAD_PER_CONTRACT = 1.00
SPREAD_PER_SHARE = 0.01

SENT_RE = re.compile(r'SENT\s+(BUY|SELL)\s+([\d,]+)\s+(\S+)')
TICK_RE = re.compile(r'tick\s+(\d+)/(\d+)')
STRIKE_RE = re.compile(r'\bK(\d+)\b')
GAP_RE = re.compile(r'gap\s+([+-][\d.]+)%')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('log')
    parser.add_argument('--nlv', type=float,
                        help='final NLV, to attribute the remainder')
    args = parser.parse_args()

    contracts = shares = 0
    opt_sends = rtm_sends = 0
    per_ticker = Counter()
    switches = 0
    last_strike = None
    gaps = []
    first_trade_tick = None
    last_tick = 0

    try:
        lines = open(args.log, encoding='utf-8', errors='ignore').readlines()
    except OSError as exc:
        print(f'cannot read {args.log}: {exc}')
        return 1

    for line in lines:
        tick_match = TICK_RE.search(line)
        if tick_match:
            last_tick = int(tick_match.group(1))
        gap_match = GAP_RE.search(line)
        if gap_match:
            gaps.append(float(gap_match.group(1)))
        strike_match = STRIKE_RE.search(line)
        if strike_match:
            if last_strike and strike_match.group(1) != last_strike:
                switches += 1
            last_strike = strike_match.group(1)

        sent = SENT_RE.search(line)
        if not sent:
            continue
        quantity = int(sent.group(2).replace(',', ''))
        ticker = sent.group(3)
        per_ticker[ticker] += quantity
        if first_trade_tick is None:
            first_trade_tick = last_tick
        if ticker.upper() == config.UNDERLYING.upper():
            shares += quantity
            rtm_sends += 1
        else:
            contracts += quantity
            opt_sends += 1

    opt_fee = contracts * FEE_PER_CONTRACT
    rtm_fee = shares * FEE_PER_SHARE
    opt_spread = contracts * SPREAD_PER_CONTRACT
    rtm_spread = shares * SPREAD_PER_SHARE
    friction = opt_fee + rtm_fee + opt_spread + rtm_spread

    print()
    print('VOLUME')
    print(f'  option contracts   {contracts:>10,}   ({opt_sends} sends)')
    print(f'  RTM shares         {shares:>10,}   ({rtm_sends} sends)')
    print(f'  strike switches    {switches:>10,}')
    if first_trade_tick is not None:
        print(f'  first trade at tick{first_trade_tick:>10,} of {last_tick:,}')

    print()
    print('FRICTION  (what trading cost, regardless of the view)')
    print(f'  option commission  {opt_fee:>10,.0f}')
    print(f'  option spread      {opt_spread:>10,.0f}')
    print(f'  RTM commission     {rtm_fee:>10,.0f}')
    print(f'  RTM spread         {rtm_spread:>10,.0f}')
    print(f'  {"TOTAL":<17}  {friction:>10,.0f}')

    if gaps:
        positive = sum(1 for g in gaps if g > 0)
        print()
        print('SIGNAL')
        print(f'  ticks with a reading {len(gaps):>8,}')
        print(f'  mean gap             {sum(gaps) / len(gaps):>8.2f}%')
        print(f'  min / max            {min(gaps):>8.2f}% / {max(gaps):.2f}%')
        print(f'  sign stability       {max(positive, len(gaps) - positive) / len(gaps) * 100:>7.0f}% '
              f'one-directional')

    if args.nlv is not None:
        directional = args.nlv + friction
        print()
        print('ATTRIBUTION')
        print(f'  final NLV          {args.nlv:>10,.0f}')
        print(f'  paid in friction   {-friction:>10,.0f}')
        print(f'  => the bet itself  {directional:>10,.0f}')
        print()
        if friction > abs(args.nlv) * 0.5:
            print('  Friction dominates. The view may be fine; it is being')
            print('  traded too often. Widen the deadband and the strike')
            print('  switch distance before touching the forecast.')
        elif directional < 0:
            print('  Friction is not the main story - the volatility view lost')
            print('  money on its own. That is a forecast problem, so settle the')
            print('  tail-week mode with replay.py before trading again.')
        else:
            print('  The bet made money and friction ate it.')

    print()
    if per_ticker:
        print('BY TICKER')
        for ticker, quantity in per_ticker.most_common():
            print(f'  {ticker:<10} {quantity:>8,}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
