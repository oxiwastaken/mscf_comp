"""
Where would this model have placed in last year's competition?

    python compete.py
    python compete.py --per-trader 150000

The case is scored on RANK, not dollars, so the only question that matters is
how our P&L compares to the field. Last year's RITCxCMU final results give the
actual distribution of team P&L across 66 teams and 10 volatility heats, which
is a far better calibration than any absolute target.

Team P&L is the sum of both members (confirmed from the RITCx rules), so if both
teammates run the bot the team number is twice the per-trader number. The two
runs are near-identical - same strategy, same heat - so the sum is 2x rather
than anything diversified.
"""

import argparse
import statistics
import sys

DEFAULT_RESULTS = r'C:\Users\hayde\Downloads\RITCxCMU2025-Final Results.xlsx'


def load_field(path):
    """[[team P&L, ...] per heat] for the volatility case."""
    import openpyxl
    ws = openpyxl.load_workbook(path, data_only=True)['Sheet1']
    rows = list(ws.iter_rows(values_only=True))
    header, data = rows[0], [r for r in rows[1:] if r[0]]
    columns = [i for i, h in enumerate(header)
               if h and str(h).startswith('NLV_Volatility')]
    return [sorted(float(r[c]) for r in data if r[c] is not None)
            for c in columns]


def rank_of(value, field):
    """Competition rank: 1 is best. `field` is ascending."""
    return sum(1 for v in field if v > value) + 1


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--results', default=DEFAULT_RESULTS)
    parser.add_argument('--per-trader', type=float, action='append',
                        help='per-trader P&L to place (repeatable)')
    args = parser.parse_args()

    heats = load_field(args.results)
    size = len(heats[0])
    print(f'\n  last year: {size} teams, {len(heats)} volatility heats\n')

    candidates = args.per_trader or [50_000, 100_000, 125_000, 150_000,
                                     200_000, 264_000]
    print(f'  {"per trader":>12} {"team P&L":>12} {"mean rank":>11} '
          f'{"best":>6} {"worst":>6} {"top-10 heats":>13}')
    for per in candidates:
        team = per * 2
        ranks = [rank_of(team, field) for field in heats]
        print(f'  ${per:>11,.0f} ${team:>11,.0f} {statistics.mean(ranks):>11.1f} '
              f'{min(ranks):>6} {max(ranks):>6} '
              f'{sum(1 for r in ranks if r <= 10):>10} / {len(heats)}')

    print(f'\n  what the field actually did, per heat (team dollars):')
    flat = sorted(v for field in heats for v in field)
    pick = lambda q: flat[int(q * (len(flat) - 1))]
    print(f'    median ${statistics.median(flat):>10,.0f}   '
          f'p75 ${pick(0.75):>10,.0f}   p90 ${pick(0.90):>10,.0f}   '
          f'p99 ${pick(0.99):>10,.0f}')
    print(f'    losing team-heats: '
          f'{sum(1 for v in flat if v < 0)} / {len(flat)} '
          f'({sum(1 for v in flat if v < 0) / len(flat):.0%})\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
