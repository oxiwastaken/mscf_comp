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
import datetime
import json
import os
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GOAL = 150_000
FINE_BUDGET = 10_000

# The six modules the trader actually loads. A commit touching any of them
# starts a new build, and heats either side of it are not the same experiment.
LIVE_PATH = ('autotrader.py', 'structure.py', 'vol_engine.py',
             'news_parser.py', 'config.py', 'rit_api.py')


def builds():
    """[(local datetime, short sha, subject)] for each live-path commit."""
    try:
        out = subprocess.check_output(
            ('git', 'log', '--format=%h%x09%cI%x09%s', '--', *LIVE_PATH),
            cwd=HERE, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        sha, when, subject = line.split('\t', 2)
        stamp = datetime.datetime.fromisoformat(when).astimezone()
        rows.append((stamp.replace(tzinfo=None), sha, subject))
    return rows


def started_at(row):
    """
    When a heat BEGAN, which is the only time that decides what it traded.

    `overnight.py` spawns a fresh trader at the start of each heat, so a heat
    runs whatever was on disk then - not what is on disk when it ends, six and
    a half minutes later. Attributing by finish time credits a commit with a
    heat that had already been trading for minutes before it landed.

    The tag IS the start time, as HHMMSS. It carries no date, so take the date
    from `finished` and step back a day when the heat ran through midnight.
    """
    finished = datetime.datetime.fromisoformat(row['finished'])
    tag = row['tag']
    try:
        clock = datetime.time(int(tag[0:2]), int(tag[2:4]), int(tag[4:6]))
    except (ValueError, IndexError):
        return finished
    start = datetime.datetime.combine(finished.date(), clock)
    if start > finished:
        start -= datetime.timedelta(days=1)
    return start


def build_of(row, history):
    """Which build was on disk when this heat started."""
    when = started_at(row)
    for stamp, sha, subject in history:      # newest first
        if when >= stamp:
            return sha, subject
    return '(pre-git)', ''


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

    # Per build. Heats either side of a live-path commit are different
    # experiments and averaging across them measures neither.
    history = builds()
    if history:
        groups = {}
        for row in every:
            sha, subject = build_of(row, history)
            groups.setdefault((sha, subject), []).append(int(row['nlv']))
        print('\n  by build (a live-path commit starts a new one):')
        for (sha, subject), values in list(groups.items())[-6:]:
            note = '' if len(values) >= 10 else '   (too few to read)'
            print(f'    {sha:<10} n={len(values):<4} mean ${statistics.mean(values):>9,.0f}'
                  f'   {subject[:52]}{note}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
