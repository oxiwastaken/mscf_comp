"""
The true cost breakdown of recent heats: fees, spread, and FINES separately.

    python heat_costs.py 10

results.csv has a column called "friction" and it is NOT the CRO's fine. It is
commission plus crossed spread - contracts * $2.00 + shares * $0.02 - which the
runner estimates from the order log. The fine is a different thing entirely:
$0.10 per delta per second outside +/-7,000, and it is only visible in the
trader's own log, where the rate is printed each tick the book is outside.

Confusing the two is easy and expensive: it points the work at the wrong cost.
This prints them side by side. NLV is already net of both.
"""

import csv
import glob
import os
import statistics
import sys

import penalty_from_log

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    rows = list(csv.DictReader(open(os.path.join(HERE, 'results.csv'),
                                    encoding='utf-8')))
    rows = [r for r in rows if int(r['ticks'] or 0) >= 299
            and int(r['contracts'] or 0) > 1000][-limit:]

    print(f'\n  {"heat":>8} {"NLV":>11} {"fees+spread":>13} {"FINE":>10} '
          f'{"contracts":>10} {"shares":>9}')
    fines, nlvs = [], []
    for row in rows:
        log = os.path.join(HERE, 'logs', row['log'])
        fine = 0
        if os.path.exists(log):
            fine = sum(rate for _delta, rate in penalty_from_log.scan(log))
        fines.append(fine)
        nlvs.append(float(row['nlv']))
        print(f'  {row["tag"]:>8} ${float(row["nlv"]):>10,.0f} '
              f'${float(row["friction"]):>12,.0f} ${fine:>9,.0f} '
              f'{int(row["contracts"]):>10,} {int(row["shares"]):>9,}')

    print(f'\n  mean NLV ${statistics.mean(nlvs):>11,.0f}'
          f'    mean fine ${statistics.mean(fines):>8,.0f}'
          f'    fine as share of NLV {statistics.mean(fines) / statistics.mean(nlvs):>5.1%}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
