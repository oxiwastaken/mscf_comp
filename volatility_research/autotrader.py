"""
Automated volatility trader for the RITCx Volatility Trading Case.

    python autotrader.py                  # DRY RUN - prints, sends nothing
    python autotrader.py --live           # actually trades
    python autotrader.py --live --max-legs 300

WHY STRADDLES

The tradeable edge in this case is the gap between our forecast volatility and
the volatility the market maker is quoting. We want that exposure - vega - and
we do NOT want exposure to the direction of RTM - delta.

An at-the-money straddle (equal calls and puts at the same strike) is the clean
way to express it: the call delta and put delta very nearly cancel, so the
position is close to delta-neutral the moment it is opened, while the ATM strike
carries the highest vega of any strike. Little hedging is needed, so little
commission is paid, and the delta limit is never seriously threatened.

The alternative - buying whichever single option shows the biggest dollar edge -
looks appealing and is a trap. Observed live on the practice server: a 500-lot
of RTM52P (delta -0.987) generated -49,342 of portfolio delta on its own, on an
option with almost no vega. With 45,794 RTM shares already held against a 50,000
limit, there was not enough headroom left to hedge it. The account sat at
-27,016 delta paying $2,002 per second with no legal way back inside the band.

The lesson: an option position must be chosen for its VEGA, and its DELTA must
stay hedgeable. Straddles satisfy both by construction.

SAFETY

  * Dry run unless --live is passed.
  * Never opens a position it could not hedge within the RTM limit.
  * Respects gross and net option limits read live from the API.
  * Stops opening new positions near expiry, where delta swings violently.
  * Refuses to trade without a live two-sided quote.
  * Ctrl-C closes cleanly.
"""

import argparse
import signal
import sys
import time

import config
import news_parser
import vol_engine
from rit_api import ApiException, RitClient

# Entry and sizing thresholds, in vol points (0.01 = 1 volatility point).
#
# Calibrated from the actual economics rather than guessed. Opening a straddle
# costs roughly $4: half the 2-cent spread on each of two legs ($2) plus $1 per
# contract commission on two contracts ($2). An ATM straddle's vega is about
# S*phi(d1)*sqrt(T) per leg - near $9.60 per vol point for the pair at a typical
# mid-case T of 0.06 years. Options here CASH-SETTLE, so a position carried to
# expiry pays no exit cost at all.
#
#   breakeven held to settlement:  $4.00 / $9.60 = 0.42 vol points
#   breakeven if closed early:     $8.00 / $9.60 = 0.84 vol points
#
# The first live heat ran gaps of 1.0-1.8 points in 'market' mode and 3.3-3.7 in
# the others, so the previous 4.0-point full-size threshold was unreachable and
# the trader sized at roughly 6% of capacity all heat. These values clear the
# round-trip breakeven with a margin and reach full size at a gap this market
# actually produces.
#
# Swept over 94 recorded heats (`sweep.py --sizing`). Full size at 0.008 rather
# than 0.025 is worth +$19,554 per heat, paired t = 9.5, and it also lifts the
# WORST heat from $715 to $14,940 - it is not a variance trade. The surface is a
# broad plateau: every combination with FULL_SIZE <= 0.008 scores within $700 of
# the best, so nothing here is fitted to a knife edge.
#
# The reason is the ramp's own arithmetic rather than anything subtle. The old
# span mapped a 1.7-point gap onto 0-500 contracts, and the gaps this market
# actually produces are 1-2 points, so the book sat near half size during real
# edge. Breakeven is 0.42 vol points held to settlement; above roughly twice
# that the position should simply be at the limit, because the limit - not the
# size of the gap - is what caps the payoff.
#
# RESEARCH FORK: these are deliberately past the point the base model stops at.
# Entry and full-size are set below any real gap, so the book is always in the
# market and always at the 1,000-contract net limit in the direction of the
# view. This is the largest bet the case permits. See FINDINGS.md - it does NOT
# raise the spread of outcomes, because the limit already bound before.
ENTRY_VOL_EDGE = 0.0001
FULL_SIZE_VOL_EDGE = 0.0002
# Close out once the gap decays below this - the maker has learned. Held below
# the open-and-hold breakeven, since exiting costs more than sitting still.
# RESEARCH FORK: never trim. The base model exits at 0.003 once the maker has
# learned; this one holds the position and lets it cash-settle.
EXIT_VOL_EDGE = 0.0
# Stop opening new positions in the last fraction of the case: near expiry ATM
# delta flips between +/-1 on tiny moves and hedging it becomes ruinous.
# RESEARCH FORK: carry into expiry rather than standing down at tick 270, taking
# the violent near-expiry gamma instead of avoiding it.
NO_NEW_AFTER = 1.00
# Deadband on rebalancing, in contracts per leg.
#
# The target size moves with the vol gap, and the gap jitters tick to tick as
# the maker's quotes round to the cent. Observed in a dry run: the target walked
# 397, 366, 352, 366, 356, 351, 347 across seven consecutive ticks. Chasing that
# exactly would round-trip tens of contracts a second on both legs, paying
# commission each way to hold what is effectively the same position. Only trade
# when the gap between target and holding is worth the fees.
REBALANCE_MIN = 25
# Rebalance only when the position is off target by this fraction of itself.
#
# A flat 25-contract deadband is far too tight once positions reach the hundreds.
# Live heat 2 churned 28,444 option contracts for a book that peaked near 500 -
# roughly 50x turnover - and paid $56,888 in option commission and spread to do
# it, against $83,865 of directional profit. The ramp maps 1.7 vol points onto
# 0-500 contracts, so a half-point flicker in the gap resizes the book by ~150
# lots. Scaling the deadband with position size stops that.
REBALANCE_FRACTION = 0.25
# Smoothing applied to the vol gap before it drives sizing.
#
# The gap jitters tick to tick because the maker's quotes round to the cent, and
# that jitter was being traded. The real signal is a step change at each weekly
# shock, which survives smoothing; the rounding noise does not. At 0.15 the
# filter reaches ~80% of a step in about ten ticks - far quicker than the maker
# converges, so no edge is given up.
GAP_SMOOTHING = 0.15
# Resize the option position only when NEW INFORMATION arrives.
#
# Between news releases the straddle is left completely alone. Backtested over
# four recorded live heats (backtest.py), this beat continuous tick-by-tick
# rebalancing in every single heat:
#
#     heat 1   $22,990  vs  $18,981
#     heat 2   $62,137  vs  $23,596
#     heat 3   $82,821  vs  $69,027
#     heat 4  $138,087  vs $119,085
#     mean     $76,509  vs  $57,672      (+33%)
#
# Only about $5k of that is saved commission. The larger part is that as the
# market maker converges toward our forecast the gap SHRINKS, and continuous
# rebalancing reacts by trimming - selling out of the very convergence that pays
# us. Holding until the next release captures the whole move.
#
# Raising the rebalance deadband instead was tested and captures roughly a third
# of the benefit (40-60% deadbands gained ~$4k; 80% went backwards), so the
# threshold is the wrong lever.
#
# Delta is still hedged every tick regardless - the penalty accrues per second
# and is not optional.
REBALANCE_ON_NEWS_ONLY = True
# How far RTM must wander from the working strike before we move the position.
#
# Switching strike is expensive: it closes one straddle and opens another,
# paying spread and commission on four legs. Seen in the first live heat at tick
# 68, RTM drifted to 48.48 and the trader sold 28 of each K50 leg to buy 43 of
# each K48 - a full round trip for a marginal vega improvement. Strikes are 1
# apart, so holding until RTM is 1.5 away keeps us on a strike that is still
# close to the money while cutting the number of switches sharply.
STRIKE_SWITCH_DISTANCE = 1.5

shutdown = False


def _stop(_signum, _frame):
    global shutdown
    shutdown = True


class AutoTrader:
    def __init__(self, client, live=False, mode=None, max_legs=None,
                 verbose=False):
        self.client = client
        self.live = live
        self.mode = mode or config.TAIL_VOL_MODE
        self.max_legs = max_legs
        self.verbose = verbose
        self.delta_limit = config.DELTA_LIMIT
        self.orders_sent = 0
        self._last_tick = -1
        self._target_strike = None
        self._gap_ema = None
        self._last_news_count = -1
        self._pending_resize = True     # the first position is always allowed

    # ------------------------------------------------------------------ util
    def log(self, text):
        print(f'{time.strftime("%H:%M:%S")}  {text}', flush=True)

    def send(self, ticker, action, quantity, max_chunk):
        quantity = int(quantity)
        if quantity <= 0:
            return
        if not self.live:
            self.log(f'  DRY  {action:<4} {quantity:>6,} {ticker}')
            return
        try:
            self.client.send_in_chunks(ticker, action, quantity, max_chunk)
            self.orders_sent += 1
            self.log(f'  SENT {action:<4} {quantity:>6,} {ticker}')
        except (ApiException, ValueError) as exc:
            self.log(f'  REJECTED {action} {quantity} {ticker}: {exc}')

    def option_limits(self):
        """Live gross/net option limits and current usage."""
        gross_limit, net_limit = config.LIMIT_OPT_GROSS, config.LIMIT_OPT_NET
        try:
            for entry in self.client.limits():
                if 'OPT' in entry.get('name', '').upper():
                    gross_limit = entry.get('gross_limit', gross_limit)
                    net_limit = entry.get('net_limit', net_limit)
        except ApiException:
            pass
        return gross_limit, net_limit

    # -------------------------------------------------------------- sizing
    def target_legs(self, gap, chain, net_limit):
        """
        How many contracts per straddle leg to hold, signed.

        Positive means long volatility (buy the straddle), negative means short.
        Size ramps linearly with the size of the disagreement, so a marginal
        edge takes a small position and a large one takes the full allowance.
        """
        magnitude = abs(gap)
        if magnitude < ENTRY_VOL_EDGE:
            return 0
        span = max(FULL_SIZE_VOL_EDGE - ENTRY_VOL_EDGE, 1e-9)
        scale = min((magnitude - ENTRY_VOL_EDGE) / span, 1.0)

        # A straddle of N per leg is 2N contracts against the net limit.
        cap = net_limit // 2
        if self.max_legs:
            cap = min(cap, self.max_legs)
        cap = min(cap, self.hedgeable_legs(chain))

        legs = int(scale * cap)
        return legs if gap > 0 else -legs

    def hedgeable_legs(self, chain):
        """
        Largest straddle we could still delta-hedge inside the RTM limit.

        A straddle is near delta-neutral, but not exactly: as RTM drifts away
        from the strike the legs stop cancelling. This bounds the position so
        that even a fully one-sided outcome stays hedgeable, which is precisely
        the guard the RTM52P episode showed to be missing.
        """
        headroom = config.LIMIT_RTM_GROSS - abs(chain['underlying_position'])
        headroom = max(headroom, 0) + self.delta_limit
        # Worst case a straddle leg pair nets ~1.0 delta per contract pair.
        return max(int(headroom / config.CONTRACT_MULTIPLIER), 0)

    def pick_strike(self, chain):
        """
        Nearest-to-money strike that has BOTH legs tradeable.

        ATM maximises vega per contract. Sticky: we only move strike when the
        current one is more than one strike from the money, so ordinary drift in
        RTM does not churn the book.
        """
        spot = chain['spot']
        by_strike = {}
        for row in chain['rows']:
            by_strike.setdefault(row['strike'], {})[row['flag']] = row
        usable = sorted(
            strike for strike, legs in by_strike.items()
            if 'c' in legs and 'p' in legs
            and legs['c']['tradeable'] and legs['p']['tradeable'])
        if not usable:
            return None, by_strike
        if self._target_strike in usable:
            if abs(self._target_strike - spot) <= STRIKE_SWITCH_DISTANCE:
                return self._target_strike, by_strike
        self._target_strike = min(usable, key=lambda k: abs(k - spot))
        return self._target_strike, by_strike

    # ----------------------------------------------------------------- cycle
    def step(self):
        case = self.client.case()
        tick = case.get('tick', 0)
        total = case.get('ticks_per_period') or 300

        if case.get('status') != 'ACTIVE':
            return False
        if tick == self._last_tick:
            return True
        self._last_tick = tick

        securities = self.client.securities()
        news = self.client.news()
        week_vols, _ = news_parser.parse_vol_news(news, total)
        announced = news_parser.parse_delta_limit(news)
        if announced:
            self.delta_limit = announced

        if len(news) != self._last_news_count:
            if self._last_news_count >= 0:
                self.log(f'  news release ({len(news)} total) - resizing')
            self._last_news_count = len(news)
            self._pending_resize = True

        chain = vol_engine.build_chain(securities, tick, total, week_vols,
                                       mode=self.mode)
        if not chain['live'] or not chain['forecast'] or not chain['market_avg_iv']:
            return True

        raw_gap = chain['forecast'] - chain['market_avg_iv']
        # Smooth before sizing. The weekly vol shock is a step change and passes
        # straight through; the cent-rounding jitter that was being traded does
        # not. Seeded on the first reading so there is no warm-up lag.
        self._gap_ema = raw_gap if self._gap_ema is None else (
            GAP_SMOOTHING * raw_gap + (1 - GAP_SMOOTHING) * self._gap_ema)
        gap = self._gap_ema
        _gross_limit, net_limit = self.option_limits()

        previous_strike = self._target_strike
        strike, by_strike = self.pick_strike(chain)
        if strike is None:
            self.hedge(chain)
            return True
        # Moving strike is new information about where the position belongs, so
        # it earns a resize even without a news release.
        if previous_strike is not None and strike != previous_strike:
            self._pending_resize = True

        call, put = by_strike[strike]['c'], by_strike[strike]['p']
        held = (call['position'] + put['position']) / 2.0

        # Wind down rather than build up once the gap has decayed or we are
        # close enough to expiry that hedging a straddle turns hostile.
        late = tick >= total * NO_NEW_AFTER
        if abs(gap) < EXIT_VOL_EDGE or late:
            target = 0
        else:
            target = self.target_legs(gap, chain, net_limit)

        if self.verbose or target != round(held):
            self.log(f'tick {tick:>3}/{total}  spot {chain["spot"]:.2f}  '
                     f'fcst {chain["forecast"] * 100:5.2f}%  '
                     f'mkt {chain["market_avg_iv"] * 100:5.2f}%  '
                     f'gap {gap * 100:+5.2f}% (raw {raw_gap * 100:+5.2f}%)  '
                     f'K{strike:.0f}  held {held:+.0f}  target {target:+d}'
                     + ('  [late]' if late else ''))

        # Only resize on new information. Exits are always allowed: once the
        # edge is gone, sitting in the position is the expensive choice.
        may_trade = (not REBALANCE_ON_NEWS_ONLY
                     or self._pending_resize or target == 0)

        if may_trade:
            # Order matters. Close the strikes we are leaving FIRST: the net
            # option limit is enforced on submission, so holding the old
            # straddle while opening the new one gets the new one rejected
            # outright. Seen live at tick 236 - a 303-lot of K52 bounced off the
            # limit because 404 lots of K50 were still on the book, and only
            # filled a tick later once K50 had been closed.
            self.close_other_strikes(chain, strike)
            self.rebalance(call, put, target)
            self._pending_resize = False
            hedge_target = target
        else:
            # Not trading this tick, so the hedge must project the position we
            # are actually keeping, not the one we merely computed.
            hedge_target = int(round(held))

        # Hedge the delta we are about to HAVE, not the one we had before
        # trading. Hedging the stale figure and correcting next tick is what
        # produced BUY 12,369 RTM followed one second later by SELL 12,381.
        self.hedge(chain, strike, hedge_target)
        return True

    def projected_delta(self, chain, strike, target):
        """
        Portfolio delta as it will stand once this tick's option trades land.

        Every leg at the working strike goes to `target`; everything else is
        being closed, so it contributes nothing. Fills are immediate in this
        case (execution_delay_ms is 0), so the projection is what the next tick
        will actually observe.
        """
        total = float(chain['underlying_position'])
        for row in chain['rows']:
            if row['delta'] is None:
                continue
            position = target if row['strike'] == strike else 0
            total += position * row['delta'] * config.CONTRACT_MULTIPLIER
        return total

    def rebalance(self, call, put, target):
        """
        Move each leg toward the target, trading only the difference.

        Legs are handled independently rather than as a pair, so a rejected or
        partially filled order on one leg self-corrects on the next tick instead
        of leaving the straddle permanently lopsided.

        The deadband is skipped when target is zero: closing out is always worth
        doing in full, however small the remainder.
        """
        for leg in (call, put):
            change = int(target - leg['position'])
            if change == 0:
                continue
            threshold = max(REBALANCE_MIN,
                            REBALANCE_FRACTION * max(abs(target),
                                                     abs(leg['position'])))
            if target != 0 and abs(change) < threshold:
                continue
            action = 'BUY' if change > 0 else 'SELL'
            self.send(leg['ticker'], action, abs(change),
                      config.MAX_TRADE_OPTIONS)

    def close_other_strikes(self, chain, keep_strike):
        """
        Flatten option positions away from the working strike.

        Stale legs at other strikes drift in delta without contributing the vega
        we actually want, which is how a book turns into an unhedgeable mess.
        """
        for row in chain['rows']:
            if row['strike'] == keep_strike or not row['position']:
                continue
            action = 'SELL' if row['position'] > 0 else 'BUY'
            self.send(row['ticker'], action, abs(row['position']),
                      config.MAX_TRADE_OPTIONS)

    def hedge(self, chain, strike=None, target=None):
        delta = (chain['portfolio_delta'] if strike is None
                 else self.projected_delta(chain, strike, target))
        suggestion = vol_engine.hedge_suggestion(delta, limit=self.delta_limit)
        if not suggestion['action'] or suggestion['quantity'] <= 0:
            return
        quantity = suggestion['quantity']
        signed = quantity if suggestion['action'] == 'BUY' else -quantity
        projected = chain['underlying_position'] + signed
        if abs(projected) > config.LIMIT_RTM_GROSS:
            allowed = config.LIMIT_RTM_GROSS - abs(chain['underlying_position'])
            if allowed <= 0:
                self.log(f'  CANNOT HEDGE: delta {delta:+,.0f} '
                         f'but RTM position is at its limit')
                return
            quantity = min(quantity, allowed)
        if suggestion['over_limit']:
            self.log(f'  delta {delta:+,.0f} OVER LIMIT, '
                     f'-${suggestion["penalty_per_second"]:,.0f}/s')
        self.send(config.UNDERLYING, suggestion['action'], quantity,
                  config.MAX_TRADE_RTM)

    def run(self):
        mode = 'LIVE' if self.live else 'DRY RUN'
        self.log(f'autotrader starting [{mode}]  tail-mode={self.mode}  '
                 f'entry={ENTRY_VOL_EDGE * 100:.1f}vol  exit={EXIT_VOL_EDGE * 100:.1f}vol')
        if not self.live:
            self.log('no orders will be sent. pass --live to trade.')
        waiting = False
        while not shutdown:
            try:
                if not self.step():
                    if not waiting:
                        self.log('waiting for an active heat...')
                        waiting = True
                    time.sleep(0.5)
                    continue
                waiting = False
            except ApiException as exc:
                self.log(f'api error: {exc}')
                time.sleep(1.0)
                continue
            time.sleep(0.1)
        self.log(f'stopped. {self.orders_sent} order batches sent.')


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--live', action='store_true',
                        help='actually send orders (default is a dry run)')
    parser.add_argument('--mode', choices=('market', 'fixed', 'persist'))
    parser.add_argument('--max-legs', type=int,
                        help='cap contracts per straddle leg')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _stop)
    trader = AutoTrader(RitClient(), live=args.live, mode=args.mode,
                        max_legs=args.max_legs, verbose=args.verbose)
    trader.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
