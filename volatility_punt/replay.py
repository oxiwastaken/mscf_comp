"""
Replay recorded heats and compare volatility models on real market data.

    python replay.py heats/heat_20260917_143000.jsonl
    python replay.py heats/*.jsonl              # aggregate across heats

Every mode is run over the identical recorded ticks, quotes and news, so any
difference between them is the model, not luck and not a guess about how the
market maker behaves. This is what makes it worth more than a simulation.

Two independent measures are reported.

1. FORECAST ERROR. For each tick, compare the mode's forecast against the
   volatility RTM actually went on to realise over the option's remaining life,
   computed from the recorded price path. That realised-forward vol is exactly
   the quantity a delta-hedged option position pays out on, so lower error here
   is a direct statement about model quality, independent of any trading rule.

2. REPLAYED P&L. Run the trading logic over the recorded bid/ask, paying the
   real spread and the real commissions, charging the delta penalty every tick,
   and cash-settling at the final price. The case guarantees unlimited depth and
   no price impact, so fills at the recorded touch are realistic.
"""

import argparse
import glob
import json
import math
import statistics
import sys

import config
import news_parser
import vol_engine

MODES = ('market', 'fixed', 'persist')
MIN_RETURNS_FOR_REALISED = 15


def load(path):
    snapshots = []
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if line:
                snapshots.append(json.loads(line))
    return snapshots


def spot_of(securities):
    for sec in securities:
        if sec['ticker'].upper() == config.UNDERLYING.upper():
            bid, ask = sec.get('bid'), sec.get('ask')
            if bid and ask:
                return (bid + ask) / 2.0
            return sec.get('last')
    return None


def realised_forward_vol(spots, index, ticks_total):
    """
    Annualised volatility RTM actually realised from `index` to the end of the
    recording, from the observed returns. This is the ground truth the option
    was priced against.
    """
    tail = spots[index:]
    if len(tail) < MIN_RETURNS_FOR_REALISED + 1:
        return None
    returns = []
    for prev, curr in zip(tail, tail[1:]):
        if prev and curr and prev > 0:
            returns.append(curr / prev - 1.0)
    if len(returns) < MIN_RETURNS_FOR_REALISED:
        return None
    try:
        per_tick = statistics.pstdev(returns)
    except statistics.StatisticsError:
        return None
    ticks_per_year = ticks_total / config.CASE_YEARS
    return per_tick * math.sqrt(ticks_per_year)


def forecast_error(snapshots, mode):
    """Mean absolute error and RMSE of the mode's forecast, in vol points."""
    ticks_total = snapshots[0]['case'].get('ticks_per_period') or 300
    spots = [spot_of(s['securities']) for s in snapshots]
    errors = []

    for i, snapshot in enumerate(snapshots):
        truth = realised_forward_vol(spots, i, ticks_total)
        if truth is None:
            continue
        tick = snapshot['case'].get('tick', 0)
        week_vols, _ = news_parser.parse_vol_news(snapshot['news'], ticks_total)
        try:
            chain = vol_engine.build_chain(
                snapshot['securities'], tick, ticks_total, week_vols, mode=mode)
        except ValueError:
            continue
        if chain['forecast'] is None:
            continue
        errors.append(chain['forecast'] - truth)

    if not errors:
        return None
    return {
        'n': len(errors),
        'mae': statistics.mean(abs(e) for e in errors),
        'rmse': math.sqrt(statistics.mean(e ** 2 for e in errors)),
        'bias': statistics.mean(errors),
    }


def replay_pnl(snapshots, mode, size=20):
    """Trade the recorded quotes with the given mode. Returns P&L breakdown."""
    ticks_total = snapshots[0]['case'].get('ticks_per_period') or 300
    positions = {}
    cash = 0.0
    fees = 0.0
    penalty = 0.0
    trades = 0
    last_spot = None

    for snapshot in snapshots:
        tick = snapshot['case'].get('tick', 0)
        securities = snapshot['securities']
        last_spot = spot_of(securities) or last_spot

        # Replay always starts flat; the positions recorded in the file belong
        # to whatever was done live and would otherwise contaminate the test.
        overridden = [dict(s, position=positions.get(s['ticker'].upper(), 0))
                      for s in securities]

        week_vols, _ = news_parser.parse_vol_news(snapshot['news'], ticks_total)
        try:
            chain = vol_engine.build_chain(
                overridden, tick, ticks_total, week_vols, mode=mode)
        except ValueError:
            continue
        if chain['forecast'] is None or chain['t_years'] <= 0:
            continue

        opt_gross = sum(abs(v) for k, v in positions.items()
                        if k != config.UNDERLYING.upper())
        opt_net = sum(v for k, v in positions.items()
                      if k != config.UNDERLYING.upper())

        for row in chain['rows']:
            ticker = row['ticker']
            buy, sell = row['buy_edge'], row['sell_edge']
            if buy is not None and buy >= config.MIN_EDGE_DOLLARS and row['ask']:
                if (opt_gross + size <= config.LIMIT_OPT_GROSS
                        and abs(opt_net + size) <= config.LIMIT_OPT_NET):
                    fee = config.FEE_PER_CONTRACT * size
                    cash -= row['ask'] * size * config.CONTRACT_MULTIPLIER + fee
                    fees += fee
                    positions[ticker] = positions.get(ticker, 0) + size
                    opt_gross += size
                    opt_net += size
                    trades += 1
            elif sell is not None and sell >= config.MIN_EDGE_DOLLARS and row['bid']:
                if (opt_gross + size <= config.LIMIT_OPT_GROSS
                        and abs(opt_net - size) <= config.LIMIT_OPT_NET):
                    fee = config.FEE_PER_CONTRACT * size
                    cash += row['bid'] * size * config.CONTRACT_MULTIPLIER - fee
                    fees += fee
                    positions[ticker] = positions.get(ticker, 0) - size
                    opt_gross += size
                    opt_net -= size
                    trades += 1

        # hedge, then charge the penalty on whatever delta remains
        rows = [dict(r, position=positions.get(r['ticker'], 0)) for r in chain['rows']]
        underlying = positions.get(config.UNDERLYING.upper(), 0)
        delta = vol_engine.portfolio_delta(rows, underlying)
        suggestion = vol_engine.hedge_suggestion(delta)
        if suggestion['action'] and suggestion['quantity'] > 0:
            quantity = suggestion['quantity']
            signed = quantity if suggestion['action'] == 'BUY' else -quantity
            if abs(underlying + signed) <= config.LIMIT_RTM_GROSS:
                price = (last_spot or 0) + (0.01 if signed > 0 else -0.01)
                fee = config.FEE_PER_SHARE * quantity
                cash -= signed * price + fee
                fees += fee
                positions[config.UNDERLYING.upper()] = underlying + signed

        rows = [dict(r, position=positions.get(r['ticker'], 0)) for r in chain['rows']]
        delta = vol_engine.portfolio_delta(
            rows, positions.get(config.UNDERLYING.upper(), 0))
        breach = max(abs(delta) - config.DELTA_LIMIT, 0.0)
        if breach:
            charge = breach * config.DELTA_PENALTY_RATE
            cash -= charge
            penalty += charge

    # settle
    for ticker, position in positions.items():
        if ticker == config.UNDERLYING.upper():
            cash += position * (last_spot or 0)
            continue
        parsed = vol_engine.parse_option_ticker(ticker)
        if not parsed or not position:
            continue
        flag, strike = parsed
        intrinsic = (max(0.0, (last_spot or 0) - strike) if flag == 'c'
                     else max(0.0, strike - (last_spot or 0)))
        cash += position * intrinsic * config.CONTRACT_MULTIPLIER

    return {'pnl': cash, 'fees': fees, 'penalty': penalty, 'trades': trades}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('paths', nargs='+', help='recorded .jsonl heat files')
    parser.add_argument('--size', type=int, default=20)
    args = parser.parse_args()

    files = []
    for pattern in args.paths:
        files.extend(sorted(glob.glob(pattern)) or [pattern])

    heats = []
    for path in files:
        try:
            snapshots = load(path)
        except OSError as exc:
            print(f'skipping {path}: {exc}')
            continue
        if len(snapshots) < 20:
            print(f'skipping {path}: only {len(snapshots)} snapshots')
            continue
        heats.append((path, snapshots))

    if not heats:
        print('no usable recordings. Record one first:\n  python record.py')
        return 1

    print(f'\n{len(heats)} heat(s), {sum(len(s) for _, s in heats)} ticks total\n')

    totals = {mode: {'pnl': [], 'rmse': [], 'mae': [], 'bias': []} for mode in MODES}

    for path, snapshots in heats:
        ticks_total = snapshots[0]['case'].get('ticks_per_period') or 300
        print(f'  {path}  ({len(snapshots)} ticks, case length {ticks_total})')
        for mode in MODES:
            err = forecast_error(snapshots, mode)
            pnl = replay_pnl(snapshots, mode, size=args.size)
            totals[mode]['pnl'].append(pnl['pnl'])
            if err:
                totals[mode]['rmse'].append(err['rmse'])
                totals[mode]['mae'].append(err['mae'])
                totals[mode]['bias'].append(err['bias'])
            err_text = (f"rmse {err['rmse'] * 100:5.2f}%  bias {err['bias'] * 100:+5.2f}%"
                        if err else 'rmse    n/a')
            print(f'    {mode:<8} P&L ${pnl["pnl"]:>12,.0f}   {err_text}   '
                  f'trades {pnl["trades"]:>4}  fees ${pnl["fees"]:>8,.0f}  '
                  f'penalty ${pnl["penalty"]:>8,.0f}')
        print()

    print('  ---- aggregate across heats ----')
    for mode in MODES:
        pnls = totals[mode]['pnl']
        rmses = totals[mode]['rmse']
        line = f'  {mode:<8} mean P&L ${statistics.mean(pnls):>12,.0f}'
        if len(pnls) > 1:
            line += f'   worst ${min(pnls):>12,.0f}'
        if rmses:
            line += f'   mean rmse {statistics.mean(rmses) * 100:5.2f}%'
        print(line)

    by_pnl = max(MODES, key=lambda m: statistics.mean(totals[m]['pnl']))
    scored = [m for m in MODES if totals[m]['rmse']]
    by_rmse = min(scored, key=lambda m: statistics.mean(totals[m]['rmse'])) if scored else None

    print(f'\n  best mean P&L:       {by_pnl}')
    if by_rmse:
        print(f'  best forecast error: {by_rmse}')
        if by_rmse == by_pnl:
            print(f'\n  Both measures agree. Set TAIL_VOL_MODE = {by_pnl!r} in config.py.')
        else:
            print(f'\n  The two measures disagree, which usually means the P&L gap is\n'
                  f'  noise from a small sample. Prefer {by_rmse!r} (the forecast measure\n'
                  f'  uses every tick, not just the few that triggered trades), or\n'
                  f'  record more heats.')
    if len(heats) < 3:
        print(f'\n  Only {len(heats)} heat(s) - record a few more before trusting this.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
