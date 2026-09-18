"""
How much of a heat the gross-capacity structure is actually on, and what it
buys while it is.

    python uptime_study.py

The structure was worth +$6.1k a heat in the first paired test - real, but a
long way under the +37% of vega the limits make available. Either it is not on
often enough, or what it buys while on is smaller than the arithmetic suggests.
This separates those two, which decides whether the gate should be loosened or
the whole idea capped.
"""

import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

import config
import news_parser
import structure
import sweep
import vol_engine
from replay import load


def job(path):
    snapshots = load(path)
    ticks_total = snapshots[0]['case'].get('ticks_per_period') or 300
    on = off = 0
    extras, ratios = [], []
    for snap in snapshots:
        tick = snap['case'].get('tick', 0)
        week_vols, _ = news_parser.parse_vol_news(snap.get('news') or [],
                                                  ticks_total)
        try:
            chain = vol_engine.build_chain(snap['securities'], tick,
                                           ticks_total, week_vols,
                                           mode=config.TAIL_VOL_MODE)
        except ValueError:
            continue
        if not chain['live'] or not chain['forecast']:
            continue
        usable = [r for r in chain['rows'] if r['tradeable']]
        if not usable:
            continue
        strike = min({r['strike'] for r in usable},
                     key=lambda k: abs(k - chain['spot']))
        legs = {r['flag']: r for r in chain['rows'] if r['strike'] == strike}
        if 'c' not in legs or legs['c'].get('vega') is None:
            continue
        atm_vega = abs(legs['c']['vega'])
        candidates = structure.wing_legs(chain['rows'], strike, chain['spot'],
                                         atm_vega, max_ratio=1.0)
        if not candidates:
            off += 1
            continue
        # What a full-size structure would buy here, at the legs it would use.
        room = structure.WING_CONTRACTS or 562
        per_leg = room // 2 if len(candidates) > 1 else room
        placed, shorted = 0, 0.0
        for row in candidates:
            lot = min(per_leg, room - placed)
            if lot <= 0:
                break
            placed += lot
            shorted += lot * abs(row['vega'])
        extra = (placed * atm_vega - shorted) * config.CONTRACT_MULTIPLIER
        extras.append(extra)
        ratios.append(shorted / (placed * atm_vega) if placed else 1.0)
        if abs(candidates[0]['vega']) / atm_vega <= 0.60:
            on += 1
        else:
            off += 1
    if not extras:
        return None
    return on, off, statistics.mean(extras), statistics.mean(ratios)


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings\n')
    with ProcessPoolExecutor() as pool:
        rows = [r for r in pool.map(job, paths, chunksize=4) if r]

    on = sum(r[0] for r in rows)
    off = sum(r[1] for r in rows)
    print(f'  ticks where a 0.60 ratio gate admits the structure   '
          f'{on / (on + off):>6.1%}')
    print(f'  mean extra vega a 562-contract structure buys        '
          f'${statistics.mean([r[2] for r in rows]):>8,.0f} per vol point')
    print(f'  mean wing/ATM vega ratio at the legs it would use     '
          f'{statistics.mean([r[3] for r in rows]):>7.2f}')
    print(f'\n  for scale: the straddle alone carries about $5,760 a vol point '
          f'early\n  in a heat, and the structure costs ~$2,250 to open and the '
          f'same to unwind.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
