"""
Is our volatility signal pointing the right way?

    python diagnose.py heats/live_233406.jsonl

Answers two questions that decide whether the strategy is sound at all, using a
recorded heat and nothing else.

1. IS THE VOLATILITY SCALE RIGHT?
   The news announces a number ("this week will be 20%"). We feed that into
   Black-Scholes as an annualised sigma. If that interpretation is wrong - wrong
   annualisation factor, wrong ticks-per-year, wrong time to expiry - then every
   forecast is off by a constant factor and the gap against market implied vol
   can point the wrong way. This measures the volatility RTM actually realised
   each week, annualised our way, and compares it to what was announced. They
   should match closely.

2. DOES THE SIGNAL PREDICT THE RIGHT DIRECTION?
   Our trade says: when forecast > market implied vol, buy volatility. That is
   only profitable if realised volatility also comes in above market implied.
   This scores, tick by tick, whether sign(forecast - market_iv) agrees with
   sign(realised_forward_vol - market_iv). Agreement well below 50% means the
   signal is inverted and flipping it would make money.
"""

import argparse
import math
import statistics
import sys

import config
import news_parser
import vol_engine
from replay import load, realised_forward_vol, spot_of

MODES = ('market', 'fixed', 'persist')


def weekly_realised(spots, ticks_total):
    """Annualised volatility RTM actually realised within each week."""
    per_week = ticks_total / config.WEEKS_IN_CASE
    ticks_per_year = ticks_total / config.CASE_YEARS
    out = {}
    for week in range(1, config.WEEKS_IN_CASE + 1):
        start, end = int((week - 1) * per_week), int(week * per_week)
        window = [s for s in spots[start:end + 1] if s]
        if len(window) < 10:
            continue
        returns = [b / a - 1.0 for a, b in zip(window, window[1:]) if a]
        if len(returns) < 10:
            continue
        out[week] = statistics.pstdev(returns) * math.sqrt(ticks_per_year)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('path')
    args = parser.parse_args()

    snapshots = load(args.path)
    if len(snapshots) < 50:
        print('recording too short')
        return 1
    ticks_total = snapshots[0]['case'].get('ticks_per_period') or 300
    spots = [spot_of(s['securities']) for s in snapshots]

    # ---------------------------------------------------------------- scale
    announced, _ = news_parser.parse_vol_news(snapshots[-1]['news'], ticks_total)
    realised = weekly_realised(spots, ticks_total)

    print(f'\n1. VOLATILITY SCALE  ({len(snapshots)} ticks, case {ticks_total})')
    print(f'   {"week":<6}{"announced":>11}{"realised":>11}{"ratio":>9}')
    ratios = []
    for week in sorted(set(announced) | set(realised)):
        a, r = announced.get(week), realised.get(week)
        if a and r:
            ratios.append(r / a)
            print(f'   {week:<6}{a * 100:>10.2f}%{r * 100:>10.2f}%{r / a:>9.2f}')
        else:
            print(f'   {week:<6}{(f"{a * 100:.2f}%" if a else "-"):>11}'
                  f'{(f"{r * 100:.2f}%" if r else "-"):>11}{"-":>9}')
    if ratios:
        mean_ratio = statistics.mean(ratios)
        print(f'\n   mean realised/announced ratio: {mean_ratio:.3f}')
        if 0.75 <= mean_ratio <= 1.33:
            print('   -> scale looks CORRECT. Announced numbers are annualised')
            print('      the way we assume; realised vol tracks them.')
        else:
            print(f'   -> SCALE IS WRONG by roughly {mean_ratio:.2f}x.')
            print('      Every forecast is mis-stated by this factor, which can')
            print('      flip the sign of the gap against market implied vol.')
            if 0.45 <= mean_ratio <= 0.55 or 1.8 <= mean_ratio <= 2.2:
                print('      A factor near 2 suggests a ticks-per-year or')
                print('      weeks-in-case error.')

    # -------------------------------------------------------------- direction
    print('\n2. SIGNAL DIRECTION')
    print(f'   {"mode":<9}{"n":>6}{"agree":>8}{"mean gap":>11}'
          f'{"mean truth":>12}{"corr":>8}')
    for mode in MODES:
        gaps, truths = [], []
        for i, snapshot in enumerate(snapshots):
            truth = realised_forward_vol(spots, i, ticks_total)
            if truth is None:
                continue
            tick = snapshot['case'].get('tick', 0)
            week_vols, _ = news_parser.parse_vol_news(snapshot['news'], ticks_total)
            try:
                chain = vol_engine.build_chain(snapshot['securities'], tick,
                                               ticks_total, week_vols, mode=mode)
            except ValueError:
                continue
            if not chain['forecast'] or not chain['market_avg_iv'] or not chain['live']:
                continue
            gaps.append(chain['forecast'] - chain['market_avg_iv'])
            truths.append(truth - chain['market_avg_iv'])

        if len(gaps) < 20:
            print(f'   {mode:<9}{len(gaps):>6}   not enough data')
            continue
        agree = sum(1 for g, t in zip(gaps, truths)
                    if (g > 0) == (t > 0)) / len(gaps)
        try:
            corr = statistics.correlation(gaps, truths)
        except Exception:
            corr = float('nan')
        print(f'   {mode:<9}{len(gaps):>6}{agree * 100:>7.0f}%'
              f'{statistics.mean(gaps) * 100:>10.2f}%'
              f'{statistics.mean(truths) * 100:>11.2f}%{corr:>8.2f}')

    print()
    print('   "agree" = how often our gap pointed the same way as the move that')
    print('   actually happened. Above 50% the signal has edge; near 50% it is')
    print('   noise; well below 50% it is INVERTED and should be flipped.')
    print('   "mean truth" is how far realised vol sat above (+) or below (-)')
    print('   what the market was quoting - the profit available to a buyer.')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
