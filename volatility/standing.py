"""The overnight standing: P&L, the SERVER's fine, and where the fine came from.

    python standing.py            # last 10 heats, plus the rolling average
    python standing.py 25

The goal is stated as a TEN-HEAT AVERAGE, so that is the headline here rather
than the last heat - single heats are worthless at a ~$60k per-heat spread, and
reacting to one is how a working model gets tuned into a broken one.

The fine is read from the recording's own `total_fines`, which is what the
server charged. Do not use the trader log for this: it prints a rate only on
the ticks it noticed it was outside the band, so it undercounts badly.
"""

import csv
import glob
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GOAL = 150_000
FINE_BUDGET = 10_000


def server_fine(tag):
    """(fine, worst second, ticks charged) the server billed for one heat."""
    path = os.path.join(HERE, 'heats', f'live_{tag}.jsonl')
    if not os.path.exists(path):
        return None, 0.0, 0
    fine = prev = 0.0
    worst = 0.0
    charged = 0
    with open(path, encoding='utf-8', errors='ignore') as handle:
        for line in handle:
            try:
                snap = json.loads(line)
            except ValueError:
                continue
            trader = snap.get('trader') or {}
            if trader.get('total_fines') is None:
                continue
            fine = float(trader['total_fines'] or 0.0)
            step = fine - prev
            prev = fine
            if step > 0:
                charged += 1
                worst = max(worst, step)
    return fine, worst, charged


def rows(limit):
    with open(os.path.join(HERE, 'results.csv'), encoding='utf-8') as handle:
        data = [r for r in csv.DictReader(handle)
                if int(r['ticks'] or 0) >= 299]
    return data[-limit:], data


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    recent, every = rows(limit)
    if not recent:
        print('  no completed heats yet')
        return 0

    print(f'\n  {"heat":>7} {"NLV":>10} {"FINE":>9} {"fees+spr":>9} '
          f'{"gross":>10} {"worst s":>8} {"secs":>5}')
    nlvs, fines = [], []
    for row in recent:
        fine, worst, charged = server_fine(row['tag'])
        nlv = int(row['nlv'])
        fric = int(row['friction'])
        nlvs.append(nlv)
        flag = ''
        if fine is None:
            fine, worst, charged = 0.0, 0.0, 0
            flag = ' (no recording)'
        fines.append(fine)
        alert = '  <-- FINE' if fine > FINE_BUDGET else ''
        print(f'  {row["tag"]:>7} {nlv:>10,} {fine:>9,.0f} {fric:>9,} '
              f'{nlv + fric + fine:>10,.0f} {worst:>8,.0f} {charged:>5}{alert}{flag}')

    mean = statistics.mean(nlvs)
    print(f'\n  {len(nlvs)}-heat mean NLV   ${mean:>10,.0f}   '
          f'{"ON TARGET" if mean >= GOAL else f"SHORT by ${GOAL - mean:,.0f}"}')
    print(f'  {len(fines)}-heat mean fine  ${statistics.mean(fines):>10,.0f}   '
          f'{"within budget" if statistics.mean(fines) <= FINE_BUDGET else "OVER BUDGET"}')
    if len(nlvs) > 1:
        se = statistics.pstdev(nlvs) / len(nlvs) ** 0.5
        print(f'  per-heat sd       ${statistics.pstdev(nlvs):>10,.0f}   '
              f'standard error ${se:,.0f}  (95% CI '
              f'${mean - 2 * se:,.0f} to ${mean + 2 * se:,.0f})')
    print(f'  losing heats       {sum(1 for v in nlvs if v < 0):>10}/{len(nlvs)}')

    # The whole run, so a change of build can be read against what came before.
    if len(every) > len(recent):
        allnlv = [int(r['nlv']) for r in every]
        print(f'\n  all {len(allnlv)} heats on record: mean ${statistics.mean(allnlv):,.0f}   '
              f'median ${statistics.median(allnlv):,.0f}   worst ${min(allnlv):,.0f}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
