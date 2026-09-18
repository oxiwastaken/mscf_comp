"""
What actually predicts the maker's next move?

    python signal_study.py

The ceiling work established that the money is in the SIGNAL, not in when we act
on it: a perfect view of where the maker's quote is heading earns $187k a heat
against our $132k, while changing the rebalancing policy moves things by one or
two thousand. So this measures our signal against the thing it is trying to
predict, and tests candidate replacements on the same data.

The target is deliberately the maker's QUOTE, not true volatility. A position's
P&L comes from the quote moving, and an earlier oracle showed that knowing the
true weekly sigmas is actually WORSE than knowing where the quote is going - the
maker never converges to true integrated variance, because it does not know the
future weeks either.

Every candidate is scored by correlation with the realised 10-tick move in
market implied vol, pooled across every recorded heat.
"""

import json
import math
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

import autotrader as at
import news_parser
import sweep
import vol_engine

HORIZON = 10


def features(path):
    """One row per tick: candidate signals, and the move they must predict."""
    rows = []
    ticks_total = None
    ema = None
    history = []
    last_news = -1
    since_news = 999
    for line in open(path, encoding='utf-8'):
        try:
            snap = json.loads(line)
        except ValueError:
            continue
        tick = snap['case'].get('tick', 0)
        ticks_total = ticks_total or snap['case'].get('ticks_per_period') or 300
        news = snap.get('news') or []
        if len(news) != last_news:
            last_news = len(news)
            since_news = 0
        else:
            since_news += 1
        week_vols, _ = news_parser.parse_vol_news(news, ticks_total)
        try:
            chain = vol_engine.build_chain(snap['securities'], tick, ticks_total,
                                           week_vols, mode='market')
        except ValueError:
            continue
        if not chain['live'] or not chain['forecast'] or not chain['market_avg_iv']:
            continue
        iv = chain['market_avg_iv']
        raw = chain['forecast'] - iv
        ema = raw if ema is None else at.GAP_SMOOTHING * raw + (1 - at.GAP_SMOOTHING) * ema
        history.append(iv)
        # Momentum: how far the quote has already moved recently. If the maker
        # converges gradually, its own recent drift predicts its next drift.
        momentum = iv - history[-HORIZON - 1] if len(history) > HORIZON else 0.0
        rows.append({'tick': tick, 'iv': iv, 'raw': raw, 'ema': ema,
                     'momentum': momentum, 'since_news': since_news})

    # Attach the move each row had to predict.
    out = []
    for i, row in enumerate(rows):
        j = i + HORIZON
        if j >= len(rows):
            break
        row['move'] = rows[j]['iv'] - row['iv']
        out.append(row)
    return out


def correlation(xs, ys):
    if len(xs) < 3:
        return 0.0
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return 0.0


def main():
    paths = sweep.usable_heats()
    with ProcessPoolExecutor(max_workers=4) as pool:
        chunks = list(pool.map(features, paths, chunksize=8))
    data = [r for chunk in chunks for r in chunk]
    print(f'\n  {len(data):,} tick observations from {len(chunks)} heats\n')

    moves = [r['move'] for r in data]
    print(f'  {"signal":<34} {"corr with the next 10-tick move":>32}')
    for name, get in (
            ('raw gap (forecast - market IV)', lambda r: r['raw']),
            ('EMA gap 0.15 - WHAT WE TRADE', lambda r: r['ema']),
            ('quote momentum (last 10 ticks)', lambda r: r['momentum']),
            ('raw gap + momentum', lambda r: r['raw'] + r['momentum']),
            ('raw gap - momentum (overshoot)', lambda r: r['raw'] - r['momentum']),
    ):
        print(f'  {name:<34} {correlation([get(r) for r in data], moves):>32.3f}')

    # The EMA is a lag. If the raw gap predicts better, smoothing is costing us.
    print(f'\n  by time since the last news release:')
    print(f'  {"bucket":>12} {"n":>8} {"raw":>8} {"ema":>8} {"momentum":>10} '
          f'{"mean |move|":>12}')
    for lo, hi in ((0, 5), (5, 15), (15, 40), (40, 80), (80, 999)):
        sub = [r for r in data if lo <= r['since_news'] < hi]
        if len(sub) < 50:
            continue
        m = [r['move'] for r in sub]
        print(f'  {lo:>5}-{hi:<6} {len(sub):>8,} '
              f'{correlation([r["raw"] for r in sub], m):>8.3f} '
              f'{correlation([r["ema"] for r in sub], m):>8.3f} '
              f'{correlation([r["momentum"] for r in sub], m):>10.3f} '
              f'{statistics.mean([abs(x) for x in m]) * 100:>11.2f}%')

    # How much of the gap does the maker actually close in 10 ticks? This is the
    # slope we should be sizing against, not an assumed one.
    usable = [r for r in data if abs(r['raw']) > 0.002]
    if usable:
        slope = (sum(r['raw'] * r['move'] for r in usable)
                 / sum(r['raw'] ** 2 for r in usable))
        print(f'\n  regression of move on raw gap: slope {slope:.3f}')
        print(f'  i.e. the maker closes {slope:.0%} of the gap every '
              f'{HORIZON} ticks\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
