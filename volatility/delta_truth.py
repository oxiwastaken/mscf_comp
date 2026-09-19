"""
What delta did the SERVER see, against what the trader believed?

    python delta_truth.py heats/live_224008.jsonl

The server charges (|delta| - 7,000) * $0.10 for every second outside the band,
and record.py captures `total_fines` every tick. So the increment in fines from
one tick to the next inverts straight back to the delta the server saw:

    server |delta| = 7,000 + (fine increment) / 0.10

That is ground truth, and it is not something the trader can see while it runs.
Against it we put our own number, computed the way autotrader computes it -
greeks at OUR FORECAST vol, because that is what vol_engine prices with.

If the two disagree systematically, the trader is hedging to the wrong target:
it believes it is inside the band while the server is charging it. When our
forecast sits five vol points away from the market's - which is exactly when we
have a position on - the greeks differ, and the difference is largest in the
wings and near expiry.
"""

import json
import os
import statistics
import sys

import config
import news_parser
import vol_engine


def load(path):
    for line in open(path, encoding='utf-8', errors='ignore'):
        try:
            yield json.loads(line)
        except ValueError:
            continue


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print('usage: delta_truth.py <recording>')
        return 1

    ticks_total = None
    previous_fine = 0.0
    rows = []
    for snap in load(path):
        case = snap.get('case') or {}
        ticks_total = case.get('ticks_per_period') or ticks_total or 300
        tick = case.get('tick', 0)
        fine = float((snap.get('trader') or {}).get('total_fines') or 0.0)
        charged = max(fine - previous_fine, 0.0)
        previous_fine = fine

        week_vols, _ = news_parser.parse_vol_news(snap.get('news') or [],
                                                  ticks_total)
        try:
            ours = vol_engine.build_chain(snap['securities'], tick, ticks_total,
                                          week_vols, mode=config.TAIL_VOL_MODE)
        except (ValueError, KeyError):
            continue
        if not ours['live']:
            continue

        # Same book, but priced at the MARKET's implied vol rather than ours.
        market = None
        if ours['market_avg_iv']:
            market = vol_engine.build_chain(
                snap['securities'], tick, ticks_total,
                {w: ours['market_avg_iv'] for w in range(1, 5)}, mode='fixed')

        implied = 7_000 + charged / config.DELTA_PENALTY_RATE if charged else None
        rows.append((tick, ours['portfolio_delta'],
                     market['portfolio_delta'] if market else None,
                     charged, implied))

    charged_rows = [r for r in rows if r[3] > 0]
    print(f'\n  {os.path.basename(path)}: {len(rows)} ticks, '
          f'{len(charged_rows)} charged, ${sum(r[3] for r in rows):,.0f} total fine\n')
    print(f'  {"tick":>5} {"our delta":>11} {"at mkt IV":>11} '
          f'{"server |d|":>11} {"fine/s":>9}  {"our |d| over?":>14}')
    for tick, ours_d, mkt_d, charged, implied in charged_rows[:28]:
        flag = 'yes' if abs(ours_d) > config.DELTA_LIMIT else 'NO - we thought we were flat'
        print(f'  {tick:>5} {ours_d:>+11,.0f} '
              f'{(f"{mkt_d:+,.0f}" if mkt_d is not None else "n/a"):>11} '
              f'{implied:>11,.0f} ${charged:>8,.0f}  {flag:>14}')

    blind = [r for r in charged_rows if abs(r[1]) <= config.DELTA_LIMIT]
    if charged_rows:
        print(f'\n  charged while WE believed we were inside the band: '
              f'{len(blind)}/{len(charged_rows)} ticks, '
              f'${sum(r[3] for r in blind):,.0f} of the fine')
        gaps = [abs(r[4]) - abs(r[1]) for r in charged_rows if r[4]]
        if gaps:
            print(f'  median gap between server |delta| and ours: '
                  f'{statistics.median(gaps):+,.0f}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
