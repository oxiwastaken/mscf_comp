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
# Scales every transaction cost this simulation charges: the commission and
# the half-spread paid to cross. 1.0 is the real market. 0.0 fills at mid for
# free, which is not a strategy - it is a measuring stick for how much of the
# model's P&L the friction is currently eating.
FRICTION = 1.0

POLICIES = (
    'continuous',        # re-evaluate the target every tick, 25% deadband
    'deadband-40',
    'deadband-60',
    'deadband-80',
    'news',              # resize only on a news release - shipped behaviour
    'news+band',         # ... and hedge less often too
    'ratchet',           # add on conviction, never trim into convergence
    'ratchet+news',      # ratchet, but only act when news lands
)


def policy_params(policy):
    """(rebalance fraction, news-gated?, ratchet?) for a policy name."""
    if policy.startswith('deadband-'):
        return int(policy.split('-')[1]) / 100.0, False, False
    if policy == 'ratchet':
        return at.REBALANCE_FRACTION, False, True
    if policy == 'ratchet+news':
        return at.REBALANCE_FRACTION, True, True
    if policy.startswith('news'):
        return at.REBALANCE_FRACTION, True, False
    return at.REBALANCE_FRACTION, False, False


def ratchet_target(target, held):
    """
    Never trim a winning position just because the gap has closed.

    Size is proportional to |gap|, and between news releases the gap shrinks for
    one reason only: the maker is converging toward our forecast. That is the
    trade WORKING. Re-sizing on it sells out of the convergence that pays us,
    which is why plain continuous rebalancing loses to news-gating even though
    it reacts faster.

    So the gap sets a FLOOR on conviction, not a target. Add when it widens,
    hold when it narrows, and leave only when the edge is actually gone - the
    gap through the exit threshold, or reversed, both of which arrive as
    target 0 or a target of the opposite sign.
    """
    if target == 0 or held == 0:
        return target
    if (target > 0) != (held > 0):
        return target                      # view flipped - act on it
    return target if abs(target) > abs(held) else int(held)


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


def trade_reason(row, change, target, strike, late, positions):
    """
    Why this option trade is happening, for the friction breakdown.

    Turnover is charged at $2 a contract - $1 fee, $1 of crossed spread - and
    the model trades roughly twelve times its own position over a heat. Knowing
    which of these reasons dominates is the difference between "reduce churn"
    as a slogan and as a change.
    """
    held = positions.get(row['ticker'], 0)
    want = held + change
    if row['strike'] != strike:
        return 'strike switch' if want == 0 else 'wing'
    if want == 0:
        return 'stand down (late)' if late else 'exit (edge gone)'
    if held == 0:
        return 'open'
    if (want > 0) != (held > 0):
        return 'flip'
    return 'add' if abs(want) > abs(held) else 'trim'


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
        # Mark-to-market NLV per tick, as [(tick, nlv)]. Cash alone is not
        # comparable to the live recordings mid-heat, because the value of an
        # open position is not in it - and the whole point of a per-phase
        # breakdown is to compare the two.
        self.curve = []
        # Straddle size held each tick, so studies can ask what we were holding
        # when the maker moved rather than only what we ended up with.
        self.sizes = []
        # Contracts and dollars of friction attributed to WHY the trade
        # happened. Turnover is the largest controllable cost in the model -
        # about $24k a heat against a $132k mean - and "reduce churn" is not
        # actionable until it is known which churn.
        self.by_reason = {}
        self.cost_by_reason = {}

    def mark(self, tick, rows, spot):
        """Record cash plus the marked value of everything currently open."""
        value = self.positions.get(config.UNDERLYING, 0) * (spot or 0.0)
        for row in rows:
            held = self.positions.get(row['ticker'], 0)
            if held and row.get('mid'):
                value += held * row['mid'] * config.CONTRACT_MULTIPLIER
        self.curve.append((tick, self.cash + value))
        net = sum(q for t, q in self.positions.items() if t != config.UNDERLYING)
        self.sizes.append((tick, net / 2.0))

    def trade_option(self, row, change, reason=None):
        if change == 0:
            return
        price = row['ask'] if change > 0 else row['bid']
        if not price:
            return
        if reason:
            self.by_reason[reason] = self.by_reason.get(reason, 0) + abs(change)
            self.cost_by_reason[reason] = (self.cost_by_reason.get(reason, 0.0)
                                           + abs(change) * 2.00)
        mid = (row['bid'] + row['ask']) / 2.0 if row['bid'] and row['ask'] else price
        price = mid + FRICTION * (price - mid)
        notional = price * abs(change) * config.CONTRACT_MULTIPLIER
        fee = abs(change) * 1.00 * FRICTION
        self.cash += -notional - fee if change > 0 else notional - fee
        self.fees += fee
        # Cost of crossing from mid to the touch, tracked separately so the
        # friction split is legible. Already inside the fill price above.
        self.spread += abs(change) * 1.00 * FRICTION
        self.positions[row['ticker']] = self.positions.get(row['ticker'], 0) + change
        self.contracts += abs(change)

    def trade_shares(self, spot, change):
        if change == 0:
            return
        price = spot + FRICTION * (0.01 if change > 0 else -0.01)
        fee = abs(change) * 0.01 * FRICTION
        self.cash -= change * price + fee
        self.fees += fee
        self.spread += abs(change) * 0.01 * FRICTION
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


def run(snapshots, policy, verbose=False, forecast_news=None,
        forecast_path=None):
    """
    Replay one heat. `forecast_news` substitutes the feed the FORECAST reads
    while leaving the feed that triggers resizing alone - the two are different
    jobs, and conflating them is how `oracle.py` first measured a perfect
    forecast as being worse than no forecast at all. News-gating fires on the
    feed's length changing, so freezing the feed froze the position too.
    """
    rebalance_fraction, news_gated, ratchet = policy_params(policy)
    ticks_total = snapshots[0]['case'].get('ticks_per_period') or 300
    book = Book()
    gap_ema = None
    iv_history = []
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
        week_vols, _ = news_parser.parse_vol_news(forecast_news or news,
                                                  ticks_total)
        try:
            chain = vol_engine.build_chain(overridden, tick, ticks_total,
                                           week_vols, mode=config.TAIL_VOL_MODE)
        except ValueError:
            continue
        if not chain['live'] or not chain['forecast'] or not chain['market_avg_iv']:
            continue

        # `forecast_path` replaces our view with a known-future one, to measure
        # the ceiling of this strategy rather than of this forecast.
        forecast = chain['forecast']
        if forecast_path is not None:
            forecast = forecast_path.get(tick)
            if forecast is None:
                continue
        raw_gap = forecast - chain['market_avg_iv']
        gap_ema = raw_gap if gap_ema is None else (
            at.GAP_SMOOTHING * raw_gap + (1 - at.GAP_SMOOTHING) * gap_ema)
        # The maker's own recent drift, added to the signal. It converges
        # slowly and in a straight line, so how fast it is moving predicts
        # where it will be - information the gap alone does not carry.
        iv_history.append(chain['market_avg_iv'])
        momentum = 0.0
        if at.MOMENTUM_WEIGHT and len(iv_history) > at.MOMENTUM_TICKS:
            momentum = iv_history[-1] - iv_history[-at.MOMENTUM_TICKS - 1]
        gap = gap_ema + at.MOMENTUM_WEIGHT * momentum

        if len(news) != last_news:
            last_news = len(news)
            pending_resize = True
            # A release is a real step, not the rounding noise the EMA exists
            # to filter, so let the signal jump toward the new reading instead
            # of easing into it over the next ten ticks.
            if at.NEWS_JUMP and gap_ema is not None:
                gap_ema += at.NEWS_JUMP * (raw_gap - gap_ema)
                gap = gap_ema + at.MOMENTUM_WEIGHT * momentum
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
        if late and not at.STAND_DOWN_CLOSES:
            # Stop trading, but let what is held cash-settle rather than paying
            # to close it. See STAND_DOWN_CLOSES.
            target = None
        elif abs(gap) < at.EXIT_VOL_EDGE or late:
            target = 0
        elif abs(gap) < at.ENTRY_VOL_EDGE:
            target = None                        # hold whatever we have
        else:
            span = at.FULL_SIZE_VOL_EDGE - at.ENTRY_VOL_EDGE
            scale = min((abs(gap) - at.ENTRY_VOL_EDGE) / span, 1.0)
            target = int(scale * cap)
            if gap < 0:
                target = -target

        # ------------------------------------------------- rebalance policy
        if ratchet and target is not None:
            # The gap sets a floor on conviction, not a target. See
            # ratchet_target: trimming into a narrowing gap is selling out of
            # the convergence that pays us.
            #
            # Measured in NET contracts, not in the size of the at-the-money
            # leg. The wing structure inflates that leg with net room borrowed
            # from the shorts, and a ratchet reading the inflated figure treats
            # its own structure as conviction and ratchets again on top of it -
            # which breaches both limits within two ticks. Net over two is the
            # straddle the view actually asked for, and with the structure
            # switched off it is exactly the leg count.
            net_held = sum(q for t, q in book.positions.items()
                           if t != config.UNDERLYING)
            target = ratchet_target(target, int(net_held / 2))
        may_trade = (not news_gated) or pending_resize or target == 0
        if target is not None and may_trade:
            # What the limits allow us to hold for this view. With the wing
            # structure switched off this is exactly the straddle, so the
            # comparison against the shipped model stays like-for-like.
            desired = structure.desired_book(chain, target_strike, target,
                                             held=book.positions)
            changes = order_changes(chain, desired, book.positions,
                                    rebalance_fraction)
            for row, change in changes:
                book.trade_option(row, change,
                                  reason=trade_reason(row, change, target,
                                                      target_strike, late,
                                                      book.positions))
            pending_resize = False

            # The limits are hard: the server refuses anything past them, so a
            # backtest that quietly exceeds them is scoring a book that could
            # never have existed.
            options = {t: q for t, q in book.positions.items()
                       if t != config.UNDERLYING}
            gross, net = structure.limits_used(options)
            if gross > config.LIMIT_OPT_GROSS or net > config.LIMIT_OPT_NET:
                raise AssertionError(
                    f'tick {tick}: book breaches the option limits '
                    f'(gross {gross}, net {net}): {options}')

        # ------------------------------------------- transient delta penalty
        # The hedge below lands in the same tick, which is why this backtest
        # used to charge zero delta penalty on every heat while a live run paid
        # $86,156. It is not free: the penalty accrues per SECOND, and a resize
        # sends its option legs in 100-lot chunks before the RTM hedge can go
        # out, so the book carries the un-hedged delta for roughly one second.
        #
        # Observed live at tick 225 - selling 500 RTM52C and 500 RTM52P with
        # spot at 51.23 put the book at +47,713 delta for one second, because
        # the ITM put carries ~0.7 delta and the straddle is only neutral at the
        # money. That single resize cost $4,071.
        #
        # Charging one second of the pre-hedge breach is the honest minimum. It
        # matters for sizing decisions specifically: a steeper ramp makes every
        # resize larger, so the transient scales with it.
        pre = [dict(r, position=book.positions.get(r['ticker'], 0))
               for r in chain['rows']]
        pre_delta = vol_engine.portfolio_delta(
            pre, book.positions.get(config.UNDERLYING, 0))
        transient = max(abs(pre_delta) - config.DELTA_LIMIT, 0.0)
        if transient:
            charge = transient * config.DELTA_PENALTY_RATE
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

        book.mark(tick, chain['rows'], spot)

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
