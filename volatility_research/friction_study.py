"""
Where the turnover goes, and what it costs.

    python friction_study.py

The model trades roughly twelve times its own position over a heat and pays
$2 a contract to do it - $1 commission, $1 of crossed spread - which is about
$24k against a $132k mean. That is the second largest number in the P&L after
the edge itself, and unlike the edge it is entirely ours to control.

"Reduce churn" is not a change until it is known WHICH churn, so every option
trade is attributed to the reason it happened, and the whole thing is scored
again at zero transaction cost to bound what perfect efficiency would be worth.
"""

import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

import backtest
import sweep
from replay import load

POLICY = 'ratchet+news'


def job(args):
    path, friction = args
    backtest.FRICTION = friction
    sweep.apply({'structure.WING_CONTRACTS': 0})
    book, _ = backtest.run(load(path), POLICY)
    return book.cash, dict(book.by_reason), dict(book.cost_by_reason), book.shares


def run(paths, friction):
    with ProcessPoolExecutor() as pool:
        return [r for r in pool.map(job, [(p, friction) for p in paths],
                                    chunksize=4)]


def main():
    paths = sweep.usable_heats()
    print(f'\n  {len(paths)} recordings, policy {POLICY}\n')

    real = run(paths, 1.0)
    free = run(paths, 0.0)

    paid = statistics.mean(r[0] for r in real)
    ideal = statistics.mean(r[0] for r in free)
    print(f'  P&L as traded            ${paid:>11,.0f}')
    print(f'  P&L at zero friction     ${ideal:>11,.0f}')
    print(f'  friction costs           ${ideal - paid:>11,.0f} a heat '
          f'({(ideal - paid) / ideal:.0%} of the gross)\n')

    reasons, costs = {}, {}
    for _cash, by_reason, cost, _shares in real:
        for name, contracts in by_reason.items():
            reasons[name] = reasons.get(name, 0) + contracts
            costs[name] = costs.get(name, 0.0) + cost.get(name, 0.0)
    n = len(real)
    shares = statistics.mean(r[3] for r in real)

    print(f'  {"reason":<20} {"contracts/heat":>15} {"$/heat":>10}')
    for name in sorted(reasons, key=lambda k: -costs[k]):
        print(f'  {name:<20} {reasons[name] / n:>15,.0f} '
              f'${costs[name] / n:>9,.0f}')
    print(f'  {"OPTIONS TOTAL":<20} {sum(reasons.values()) / n:>15,.0f} '
          f'${sum(costs.values()) / n:>9,.0f}')
    print(f'  {"RTM hedging":<20} {shares:>15,.0f} ${shares * 0.02:>9,.0f}'
          '   (shares, at $0.01 fee + $0.01 crossed)\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
