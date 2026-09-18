"""
Replay the ACTUAL straddle strategy over recorded heats, under different
rebalancing policies.

    python backtest.py heats/*.jsonl
    python backtest.py heats/*.jsonl --verbose

This exists to answer one question with data rather than opinion: does the
profit come from repositioning at each news release, with the continuous
tick-by-tick rebalancing in between merely bleeding commission?

Policies compared:

  continuous   what autotrader.py does today - re-evaluate the target every
               tick and trade whenever the proportional deadband is cleared.

  news         only resize the option position when a NEW news release lands
               (plus the forced close near expiry). Between releases the
               straddle is left completely alone. Delta is still hedged every
               tick, because the delta penalty accrues per second and is not
               optional.

  news+band    as 'news', and also hedge less often by widening the delta band
               toward the limit, to see how much of the residual friction is
               share hedging rather than option churn.

Fills are taken at the recorded bid/ask and charged the server's real fees
($1.00/contract, $0.01/share). The case cash-settles, so a position carried to
expiry pays no exit cost - which is why holding is cheaper than it looks.
"""

import argparse
import glob
import statistics
import sys

import autotrader as at
import config
import news_parser
import structure
import vol_engine
from replay import load, spot_of

# Two ways to cut the between-news churn, tested side by side:
#   raising the deadband so small target moves are ignored, versus
#   refusing to resize at all until new information arrives.
# Whether the trader hedges BETWEEN its option chunks (it does, since Change 1).
# Flipping this off prices the old send-everything-then-hedge behaviour, which
# is the only way to measure what interleaving was worth.
INTERLEAVED_HEDGE = True

POLICIES = (
    'continuous',        # today's behaviour, 25% deadband
    'deadband-40',
    'deadband-60',
    'deadband-80',
    'news',              # resize only on a news release
    'news+band',         # ... and hedge less often too
)


def policy_params(policy):
    """(rebalance fraction, news-gated?) for a policy name."""
    if policy.startswith('deadband-'):
        return int(policy.split('-')[1]) / 100.0, False
    if policy.startswith('news'):
        return at.REBALANCE_FRACTION, True
    return at.REBALANCE_FRACTION, False


def order_changes(chain, desired, positions, rebalance_fraction):
    """
    This tick's option trades as [(row, change)], closes first.

    Two rules, and the second one exists because the first can break the book.

    The deadband skips a resize too small to be worth its fees. But the legs
    are not independent: the wing short is what pays for the extra size at the
    money, so skipping the at-the-money reduction while taking the wing close
    leaves MORE net contracts than the limit allows. Seen at tick 75 of
    heat_20260918_112741 - the wing came off, a 94-lot trim was 54 contracts
    under the deadband, and the book sat at 1,186 net against a 1,000 limit,
    which the server would have rejected outright.

    So: the deadband is a fee optimisation and it is not allowed to produce an
    illegal book. If skipping makes the book breach, the whole desired book is
    traded - it is legal by construction.
    """
    def build(deadband):
        out = []
        for row in chain['rows']:
            held = positions.get(row['ticker'], 0)
            want = int(desired.get(row['ticker'], 0))
            change = want - held
            if change == 0:
                continue
            threshold = max(at.REBALANCE_MIN,
                            rebalance_fraction * max(abs(want), abs(held)))
            if deadband and want != 0 and abs(change) < threshold:
                continue
            out.append((row, change))
        # Anything that shrinks a position goes first: both limits are enforced
        # on submission, so room has to be freed before it can be used.
        out.sort(key=lambda rc: 0 if abs(rc[1] + positions.get(rc[0]['ticker'], 0))
                 < abs(positions.get(rc[0]['ticker'], 0)) else 1)
        return out

    changes = build(True)
    after = dict(positions)
    for row, change in changes:
        after[row['ticker']] = after.get(row['ticker'], 0) + change
    options = {t: q for t, q in after.items() if t != config.UNDERLYING}
    gross, net = structure.limits_used(options)
    if gross > config.LIMIT_OPT_GROSS or net > config.LIMIT_OPT_NET:
        return build(False)
    return changes


class Book:
    """Positions, cash and friction for one simulated run."""

    def __init__(self):
        self.positions = {}
        self.cash = 0.0
        self.fees = 0.0
        self.spread = 0.0
        self.penalty = 0.0
        self.contracts = 0
        self.shares = 0

    def trade_option(self, row, change):
        if change == 0:
            return
        price = row['ask'] if change > 0 else row['bid']
        if not price:
            return
        notional = price * abs(change) * config.CONTRACT_MULTIPLIER
        fee = abs(change) * 1.00
        self.cash += -notional - fee if change > 0 else notional - fee
        self.fees += fee
        # Cost of crossing from mid to the touch, tracked separately so the
        # friction split is legible. Already inside the fill price above.
        self.spread += abs(change) * 1.00
        self.positions[row['ticker']] = self.positions.get(row['ticker'], 0) + change
        self.contracts += abs(change)

    def trade_shares(self, spot, change):
        if change == 0:
            return
        price = spot + (0.01 if change > 0 else -0.01)
        fee = abs(change) * 0.01
        self.cash -= change * price + fee
        self.fees += fee
        self.spread += abs(change) * 0.01
        self.positions[config.UNDERLYING] = (
            self.positions.get(config.UNDERLYING, 0) + change)
        self.shares += abs(change)

    def settle(self, spot):
        for ticker, position in self.positions.items():
            if not position:
                continue
            if ticker == config.UNDERLYING:
                self.cash += position * spot
                continue
            parsed = vol_engine.parse_option_ticker(ticker)
            if not parsed:
                continue
            flag, strike = parsed
            intrinsic = (max(0.0, spot - strike) if flag == 'c'
                         else max(0.0, strike - spot))
            self.cash += position * intrinsic * config.CONTRACT_MULTIPLIER


def run(snapshots, policy, verbose=False):
    rebalance_fraction, news_gated = policy_params(policy)
    ticks_total = snapshots[0]['case'].get('ticks_per_period') or 300
    book = Book()
    gap_ema = None
    target_strike = None
    last_news = -1
    last_spot = None
    pending_resize = True          # always allowed to open the first position
    news_pnl_marks = []

    hedge_trigger = config.HEDGE_TRIGGER_DELTA
    hedge_target = config.HEDGE_TARGET_DELTA
    if policy == 'news+band':
        # Sit closer to the limit and hedge back to the centre, so hedges are
        # rarer but each one buys more room.
        hedge_trigger = config.DELTA_LIMIT - 500
        hedge_target = 0

    for snapshot in snapshots:
        tick = snapshot['case'].get('tick', 0)
        securities = snapshot['securities']
        spot = spot_of(securities) or last_spot
        last_spot = spot

        overridden = [dict(s, position=book.positions.get(s['ticker'].upper(), 0))
                      for s in securities]
        news = snapshot['news']
        week_vols, _ = news_parser.parse_vol_news(news, ticks_total)
        try:
            chain = vol_engine.build_chain(overridden, tick, ticks_total,
                                           week_vols, mode=config.TAIL_VOL_MODE)
        except ValueError:
            continue
        if not chain['live'] or not chain['forecast'] or not chain['market_avg_iv']:
            continue

        raw_gap = chain['forecast'] - chain['market_avg_iv']
        gap_ema = raw_gap if gap_ema is None else (
            at.GAP_SMOOTHING * raw_gap + (1 - at.GAP_SMOOTHING) * gap_ema)
        gap = gap_ema

        if len(news) != last_news:
            last_news = len(news)
            pending_resize = True
            news_pnl_marks.append((tick, book.cash))

        # ------------------------------------------------------ pick strike
        by_strike = {}
        for row in chain['rows']:
            by_strike.setdefault(row['strike'], {})[row['flag']] = row
        usable = sorted(k for k, legs in by_strike.items()
                        if 'c' in legs and 'p' in legs
                        and legs['c']['tradeable'] and legs['p']['tradeable'])
        if not usable:
            continue
        if target_strike not in usable or \
                abs(target_strike - spot) > at.STRIKE_SWITCH_DISTANCE:
            new_strike = min(usable, key=lambda k: abs(k - spot))
            if new_strike != target_strike:
                target_strike = new_strike
                pending_resize = True

        call, put = by_strike[target_strike]['c'], by_strike[target_strike]['p']

        # ------------------------------------------------------ sizing
        late = tick >= ticks_total * at.NO_NEW_AFTER
        headroom = config.LIMIT_RTM_GROSS - abs(
            book.positions.get(config.UNDERLYING, 0))
        cap = min(config.LIMIT_OPT_NET // 2,
                  max(int((max(headroom, 0) + config.DELTA_LIMIT)
                          / config.CONTRACT_MULTIPLIER), 0))
        if abs(gap) < at.EXIT_VOL_EDGE or late:
            target = 0
        elif abs(gap) < at.ENTRY_VOL_EDGE:
            target = None                        # hold whatever we have
        else:
            span = at.FULL_SIZE_VOL_EDGE - at.ENTRY_VOL_EDGE
            scale = min((abs(gap) - at.ENTRY_VOL_EDGE) / span, 1.0)
            target = int(scale * cap)
            if gap < 0:
                target = -target

        # The trader lets the gap set a floor on conviction rather than a
        # target, so the backtest must too or it measures a strategy we do not
        # run. Measured in NET contracts, because the wing structure inflates
        # the at-the-money leg with net room borrowed from its own shorts. See
        # autotrader.ratchet_target and structure.desired_book.
        if target is not None:
            net_held = sum(q for t, q in book.positions.items()
                           if t != config.UNDERLYING)
            target = at.ratchet_target(target, int(net_held / 2))

        # ------------------------------------------------- rebalance policy
        may_trade = (not news_gated) or pending_resize or target == 0
        if target is not None and may_trade:
            desired = structure.desired_book(
                chain, target_strike, target, held=book.positions,
                underlying=book.positions.get(config.UNDERLYING, 0))
            for row, change in order_changes(chain, desired, book.positions,
                                             rebalance_fraction):
                book.trade_option(row, change)
            pending_resize = False

            # The limits are hard - the server refuses anything past them - so
            # a backtest that quietly exceeds them is scoring a book that could
            # never have existed.
            options = {t: q for t, q in book.positions.items()
                       if t != config.UNDERLYING}
            gross, net = structure.limits_used(options)
            if gross > config.LIMIT_OPT_GROSS or net > config.LIMIT_OPT_NET:
                raise AssertionError(
                    f'tick {tick}: book breaches the option limits '
                    f'(gross {gross}, net {net}): {options}')

        # ------------------------------------------- transient delta penalty
        # This backtest hedges inside the same tick off the exact post-trade
        # delta, so for a long time it charged ZERO delta penalty on all 95
        # recordings while a live run paid $86,156. The penalty accrues per
        # SECOND, and a resize used to send all of its option legs before the
        # RTM hedge went out, so the book carried the un-hedged delta for about
        # a second each time.
        #
        # With interleaved hedging (autotrader.execute) the chunks are sized so
        # no single one can push delta past the band, so the transient is gone
        # by construction. Set INTERLEAVED_HEDGE False to price the old
        # behaviour and measure what the change is worth.
        if not INTERLEAVED_HEDGE:
            pre = [dict(r, position=book.positions.get(r['ticker'], 0))
                   for r in chain['rows']]
            breach = max(abs(vol_engine.portfolio_delta(
                pre, book.positions.get(config.UNDERLYING, 0)))
                - config.DELTA_LIMIT, 0.0)
            if breach:
                charge = breach * config.DELTA_PENALTY_RATE
                book.cash -= charge
                book.penalty += charge

        # ----------------------------------------------------------- hedge
        rows = [dict(r, position=book.positions.get(r['ticker'], 0))
                for r in chain['rows']]
        delta = vol_engine.portfolio_delta(
            rows, book.positions.get(config.UNDERLYING, 0))
        suggestion = vol_engine.hedge_suggestion(
            delta, trigger=hedge_trigger, target=hedge_target)
        if suggestion['action'] and suggestion['quantity'] > 0:
            signed = (suggestion['quantity'] if suggestion['action'] == 'BUY'
                      else -suggestion['quantity'])
            if abs(book.positions.get(config.UNDERLYING, 0) + signed) \
                    <= config.LIMIT_RTM_GROSS:
                book.trade_shares(spot, signed)

        rows = [dict(r, position=book.positions.get(r['ticker'], 0))
                for r in chain['rows']]
        delta = vol_engine.portfolio_delta(
            rows, book.positions.get(config.UNDERLYING, 0))
        breach = max(abs(delta) - config.DELTA_LIMIT, 0.0)
        if breach:
            charge = breach * config.DELTA_PENALTY_RATE
            book.cash -= charge
            book.penalty += charge

    book.settle(last_spot or 0)
    return book, news_pnl_marks


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('paths', nargs='+')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    files = []
    for pattern in args.paths:
        files.extend(sorted(glob.glob(pattern)) or [pattern])

    results = {p: [] for p in POLICIES}
    print()
    for path in files:
        snapshots = load(path)
        if len(snapshots) < 50:
            continue
        print(f'  {path}')
        for policy in POLICIES:
            book, marks = run(snapshots, policy, args.verbose)
            friction = book.fees + book.spread
            results[policy].append(book.cash)
            print(f'    {policy:<11} P&L ${book.cash:>11,.0f}   '
                  f'friction ${friction:>9,.0f}   '
                  f'contracts {book.contracts:>7,}   '
                  f'shares {book.shares:>8,}   '
                  f'penalty ${book.penalty:>7,.0f}')
        print()

    print('  ---- mean across heats ----')
    for policy in POLICIES:
        values = results[policy]
        if not values:
            continue
        line = f'  {policy:<11} mean ${statistics.mean(values):>11,.0f}'
        if len(values) > 1:
            line += f'   worst ${min(values):>11,.0f}'
        print(line)

    best = max(POLICIES, key=lambda p: statistics.mean(results[p])
               if results[p] else float('-inf'))
    print(f'\n  best mean P&L: {best}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
