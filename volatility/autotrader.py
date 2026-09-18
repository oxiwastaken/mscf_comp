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
import structure
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
ENTRY_VOL_EDGE = 0.002
FULL_SIZE_VOL_EDGE = 0.008
# Close out once the gap decays below this - the maker has learned. Held below
# the open-and-hold breakeven, since exiting costs more than sitting still.
EXIT_VOL_EDGE = 0.003
# Stop opening new positions in the last fraction of the case: near expiry ATM
# delta flips between +/-1 on tiny moves and hedging it becomes ruinous.
NO_NEW_AFTER = 0.90
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
# Let the gap set a FLOOR on conviction rather than a target.
#
# Position size is proportional to the vol gap, and between news releases the
# gap shrinks for exactly one reason: the maker is converging toward our
# forecast. That is the trade working. Re-sizing on it sells out of the
# convergence that pays us - which is why plain continuous rebalancing loses to
# news-gating even though it reacts faster.
#
# So: add when the gap widens, hold when it narrows, and leave only when the
# edge is genuinely gone - the gap through the exit threshold or reversed, both
# of which arrive here as a target of zero or of the opposite sign.
#
# Measured over 95 recordings against the shipped news-gated policy:
# +$1,724 a heat, paired t = 3.0, with the worst heat unchanged.
RATCHET_SIZING = True

shutdown = False


def ratchet_target(target, held):
    """Never trim a winning position just because the gap has closed."""
    if not RATCHET_SIZING or target == 0 or held == 0:
        return target
    if (target > 0) != (held > 0):
        return target                      # view flipped - act on it
    return target if abs(target) > abs(held) else int(held)


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
        """
        Send an order. Returns True if the book actually moved.

        The return value is load-bearing, not decoration. Callers track delta
        locally between API reads, and a rejected order moves no delta: a chunk
        that bounces off the net option limit - the rejection option_orders()
        exists to avoid - would otherwise be counted as filled and hedged
        against exposure the book never acquired. A dry run reports True so the
        simulated book follows the same path the live one would.
        """
        quantity = int(quantity)
        if quantity <= 0:
            return False
        if not self.live:
            self.log(f'  DRY  {action:<4} {quantity:>6,} {ticker}')
            return True
        try:
            self.client.send_in_chunks(ticker, action, quantity, max_chunk)
            self.orders_sent += 1
            self.log(f'  SENT {action:<4} {quantity:>6,} {ticker}')
            return True
        except (ApiException, ValueError) as exc:
            self.log(f'  REJECTED {action} {quantity} {ticker}: {exc}')
            return False

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

        The wing structure raises the at-the-money position by W/2 a leg while
        the share limit stays at 50,000, so the same straddle target now asks
        more of the hedge. Discounting the cap by that inflation keeps the
        guard honest: measured over 124 recordings, the structure already more
        than doubles the delta penalty ($8,786 to $19,911 a heat), and running
        out of shares to hedge with is how that becomes unbounded.
        """
        headroom = config.LIMIT_RTM_GROSS - abs(chain['underlying_position'])
        headroom = max(headroom, 0) + self.delta_limit
        # Worst case a straddle leg pair nets ~1.0 delta per contract pair.
        return max(int(headroom / config.CONTRACT_MULTIPLIER), 0)

    @staticmethod
    def structure_inflation():
        """How much bigger the option book is than the straddle alone."""
        if not structure.WING_CONTRACTS or not config.LIMIT_OPT_NET:
            return 1.0
        return 1.0 + structure.WING_CONTRACTS / config.LIMIT_OPT_NET

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
            # No view to trade on - but an un-hedged delta still costs money
            # every second, so hedge what the API just reported before bailing.
            # `market_avg_iv` is None whenever no row survives the vega / IV
            # spread filter, which is exactly what happens late in a heat as the
            # options decay: the tick we would skip is the tick where a short
            # straddle's gamma is most violent. Guarded on `live` so nothing is
            # fired against the dead two-sided-zero quotes seen between heats.
            if chain['live']:
                self.hedge_running(float(chain['portfolio_delta']),
                                   float(chain['underlying_position']))
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
            # Nothing tradeable to work with; hedge and wait. One hedging entry
            # point, so this path cannot drift out of step with the backstop.
            self.hedge_running(float(chain['portfolio_delta']),
                               float(chain['underlying_position']))
            return True
        # Moving strike is new information about where the position belongs, so
        # it earns a resize even without a news release.
        if previous_strike is not None and strike != previous_strike:
            self._pending_resize = True

        call, put = by_strike[strike]['c'], by_strike[strike]['p']
        # Conviction is measured in NET contracts, not in the size of the
        # at-the-money leg. The wing structure inflates that leg with net room
        # borrowed from its own shorts, so a ratchet reading the leg would
        # treat the structure as conviction and ratchet on top of it - which
        # breaches both limits within two ticks. With the structure switched
        # off this is exactly the leg count.
        held = self.net_contracts(chain) / 2.0

        # Wind down rather than build up once the gap has decayed or we are
        # close enough to expiry that hedging a straddle turns hostile.
        late = tick >= total * NO_NEW_AFTER
        if abs(gap) < EXIT_VOL_EDGE or late:
            target = 0
        else:
            target = ratchet_target(self.target_legs(gap, chain, net_limit),
                                    int(round(held)))

        # What the two limits together allow us to hold for this view: the
        # straddle, plus the wing shorts that buy net room for more of it.
        desired = structure.desired_book(
            chain, strike, target,
            held={row['ticker']: row['position'] for row in chain['rows']},
            net_limit=net_limit, gross_limit=_gross_limit,
            underlying=float(chain['underlying_position']))

        if self.verbose or target != round(held):
            wings = int(sum(abs(q) for t, q in desired.items()
                            if t not in (call['ticker'], put['ticker'])))
            self.log(f'tick {tick:>3}/{total}  spot {chain["spot"]:.2f}  '
                     f'fcst {chain["forecast"] * 100:5.2f}%  '
                     f'mkt {chain["market_avg_iv"] * 100:5.2f}%  '
                     f'gap {gap * 100:+5.2f}% (raw {raw_gap * 100:+5.2f}%)  '
                     f'K{strike:.0f}  held {held:+.0f}  target {target:+d}'
                     + (f'  wings {wings}' if wings else '')
                     + ('  [late]' if late else ''))

        # Only resize on new information. Exits are always allowed: once the
        # edge is gone, sitting in the position is the expensive choice.
        may_trade = (not REBALANCE_ON_NEWS_ONLY
                     or self._pending_resize or target == 0)

        if may_trade:
            # execute() hedges as it goes and returns the state it left behind,
            # anchored on this tick's true portfolio delta.
            delta, underlying = self.execute(chain, desired)
            self._pending_resize = False
        else:
            # Not trading, so the book is exactly what the API just reported.
            delta = float(chain['portfolio_delta'])
            underlying = float(chain['underlying_position'])

        # THE BACKSTOP. This runs every tick, in both branches, no exceptions.
        #
        # Change 1 removed the unconditional hedge that used to end every tick,
        # on the grounds that execute() already hedges as it goes. That was
        # wrong in a way no test caught: execute() hedges *between option
        # chunks*, so when there are no chunks to send it does nothing at all -
        # including no hedging. Ratchet sizing then made `held == target` the
        # normal state rather than a rare one, so the common case became a tick
        # that hedged nothing.
        #
        # Live cost, heat 143052: short 500 straddles at K49 while spot rose to
        # 50.68. RTM sat unchanged at +2,708 for two hundred consecutive ticks
        # while delta drifted to -44,763. 162 ticks over the limit, $467,262 of
        # penalty, and a -$372,520 heat out of a position whose volatility view
        # was making roughly +$95,000.
        #
        # Delta must be checked every tick whatever else happens, because the
        # penalty accrues every second whatever else happens.
        self.hedge_running(delta, underlying)
        return True

    def net_contracts(self, chain):
        """Signed sum of every option position - what the net limit counts."""
        return sum(int(row['position']) for row in chain['rows'])

    def option_orders(self, chain, desired):
        """
        This tick's option orders as [(row, signed change)].

        The deadband skips a resize too small to be worth its fees, and that is
        all it is for. It is NOT allowed to produce a book the limits forbid:
        the legs are not independent, since the wing short is what pays for the
        extra size at the money, so taking the wing close while skipping the
        at-the-money trim leaves more net contracts than the server permits.
        Seen in the backtest at tick 75 of heat_20260918_112741 - a 94-lot trim
        sat 54 contracts under the deadband and left the book at 1,186 net
        against a 1,000 limit, which is an order the server simply rejects.

        So if skipping would breach, the whole desired book is traded. It is
        legal by construction.
        """
        def build(deadband):
            orders = []
            for row in chain['rows']:
                position = int(row['position'])
                want = int(desired.get(row['ticker'], 0))
                change = want - position
                if change == 0:
                    continue
                threshold = max(REBALANCE_MIN,
                                REBALANCE_FRACTION * max(abs(want),
                                                         abs(position)))
                # The deadband never blocks a close: going flat is always worth
                # doing in full, however small the remainder.
                if deadband and want != 0 and abs(change) < threshold:
                    continue
                orders.append((row, change))
            return orders

        orders = build(True)
        after = {row['ticker']: int(row['position']) for row in chain['rows']}
        for row, change in orders:
            after[row['ticker']] += change
        gross, net = structure.limits_used(after)
        if gross > config.LIMIT_OPT_GROSS or net > config.LIMIT_OPT_NET:
            return build(False)
        return orders

    def execute(self, chain, desired):
        """
        Send the option orders in chunks, hedging BETWEEN them.

        The delta penalty accrues per second, not per tick. Sending every option
        order and only then hedging leaves the book carrying the whole un-hedged
        delta for about a second - ten chunked option orders go out before the
        RTM hedge does. Measured live across ten heats: $86,156 of penalty, peak
        |delta| 47,713.

        A straddle is only delta-neutral AT the money, so the spike is worst
        exactly when a resize matters most. Seen at tick 225: selling 500
        RTM52C and 500 RTM52P with spot at 51.23 put the book at +47,713 delta,
        because the in-the-money put carries about 0.7 delta. That one resize
        cost $4,071.

        Interleaving costs nothing. Fees here are charged per SHARE, not per
        order, so hedging in ten pieces costs exactly what hedging in one does -
        and the rate limit is 50 orders a second against the handful we send.
        """
        delta = float(chain['portfolio_delta'])
        underlying = float(chain['underlying_position'])
        for row, lot in self.chunks(chain, desired, delta):
            moves = lot * (row['delta'] or 0.0) * config.CONTRACT_MULTIPLIER
            # Make room BEFORE sending, not after. Hedging only afterwards left
            # the chunk's own delta un-covered for the moment that matters: the
            # band is 5,000 and a chunk carries up to 4,000, so the book could
            # sit at 4,999 un-hedged, take a chunk, and stand at 8,999 - past
            # the 7,000 limit, which is exactly the residual that survived the
            # first version of this (peak 21,025 live).
            delta, underlying = self.hedge_running(
                delta, underlying,
                trigger=max(config.HEDGE_TARGET_DELTA,
                            self.delta_limit * 0.9 - abs(moves)))
            action = 'BUY' if lot > 0 else 'SELL'
            if self.send(row['ticker'], action, abs(lot),
                         config.MAX_TRADE_OPTIONS):
                delta += moves
            delta, underlying = self.hedge_running(delta, underlying)
        return delta, underlying

    def chunks(self, chain, desired, delta):
        """
        The tick's orders split into chunks and ORDERED under two constraints.

        LIMITS FIRST. Both option limits are enforced on submission, so every
        intermediate state has to be legal, not just the destination. The old
        rule - "close other strikes first" - was a special case of this that the
        wing structure breaks: going from a long straddle to a short one has to
        buy back the wing shorts and sell the at-the-money legs, and doing
        either side in full first puts the book past a limit mid-resize. Seen
        live before the rule existed: a 303-lot of K52 bounced off the net limit
        at tick 236 because 404 lots of K50 were still on the book.

        DELTA SECOND. Among the chunks that are legal to send now, take the one
        leaving |delta| smallest. Sending every call chunk and then every put
        chunk walks delta out and back, dragging hedges both ways - measured
        live at +74% RTM volume (244,598 to 425,994 shares a heat), which gave
        back half of what interleaving saved. A straddle's legs have
        opposite-signed deltas that nearly cancel, so alternating them keeps the
        running delta near zero and almost no hedging is needed mid-resize.

        If nothing is legal - which needs a book that is already outside a limit
        - take whichever chunk reduces the worst breach most, so the book walks
        back inside rather than stalling.
        """
        positions = {row['ticker']: int(row['position'])
                     for row in chain['rows']}
        pending = []
        for row, change in self.option_orders(chain, desired):
            step = self.chunk_for(row)
            remaining = abs(change)
            sign = 1 if change > 0 else -1
            while remaining > 0:
                lot = min(step, remaining)
                pending.append((row, sign * lot))
                remaining -= lot

        def excess(chunk):
            """How far past the limits the book would stand after `chunk`."""
            row, lot = chunk
            after = dict(positions)
            after[row['ticker']] = after.get(row['ticker'], 0) + lot
            gross, net = structure.limits_used(after)
            return (max(gross - config.LIMIT_OPT_GROSS, 0)
                    + max(net - config.LIMIT_OPT_NET, 0))

        def drift(chunk):
            row, lot = chunk
            return abs(delta + lot * (row['delta'] or 0.0)
                       * config.CONTRACT_MULTIPLIER)

        ordered = []
        while pending:
            legal = [c for c in pending if excess(c) == 0]
            best = (min(legal, key=drift) if legal
                    else min(pending, key=excess))
            pending.remove(best)
            row, lot = best
            positions[row['ticker']] = positions.get(row['ticker'], 0) + lot
            delta += lot * (row['delta'] or 0.0) * config.CONTRACT_MULTIPLIER
            ordered.append(best)
        return ordered

    def chunk_for(self, row):
        """
        Contracts to send at once, so ONE chunk cannot breach the delta band.

        The maximum trade size is 100 contracts, but 100 contracts is not a
        fixed amount of delta. An in-the-money leg carries most of a delta per
        share, so a single 100-lot of RTM52P at 0.79 delta moves the book 7,900
        - past the entire +/-7,000 limit on its own, before any hedge can
        follow it. Interleaving alone therefore is not enough; the chunk has to
        be small enough that the hedge can keep up.

        Budget each chunk the room between the hedge target and the limit, with
        a margin. Low-delta legs still go 100 at a time; only the legs that can
        actually breach get cut finer.
        """
        delta = abs(row.get('delta') or 0.0)
        budget = (self.delta_limit - config.HEDGE_TARGET_DELTA) * 0.8
        if delta <= 0 or budget <= 0:
            return config.MAX_TRADE_OPTIONS
        per_contract = delta * config.CONTRACT_MULTIPLIER
        return max(1, min(config.MAX_TRADE_OPTIONS, int(budget / per_contract)))

    def hedge_running(self, delta, underlying, trigger=None):
        """
        Hedge a running delta mid-resize. Returns the updated (delta, position).

        RTM carries one delta per share, so the share trade moves the portfolio
        delta one for one. Tracking both locally avoids re-reading positions
        from the API between chunks, which would cost more time than it saves.

        `trigger` lowers the bar when the chunk about to be sent needs room for
        itself; left alone it uses the ordinary band.
        """
        suggestion = vol_engine.hedge_suggestion(
            delta, limit=self.delta_limit,
            trigger=(config.HEDGE_TRIGGER_DELTA if trigger is None
                     else max(trigger, 0.0)))
        if not suggestion['action'] or suggestion['quantity'] <= 0:
            return delta, underlying
        quantity = suggestion['quantity']
        signed = quantity if suggestion['action'] == 'BUY' else -quantity
        # Test the RESULTING position, not the sum of magnitudes - a hedge that
        # unwinds an existing position never threatens the limit however large
        # that position is.
        if abs(underlying + signed) > config.LIMIT_RTM_GROSS:
            room = int(max(config.LIMIT_RTM_GROSS - abs(underlying), 0))
            if room <= 0:
                self.log(f'  CANNOT HEDGE: delta {delta:+,.0f} '
                         f'but RTM position is at its limit')
                return delta, underlying
            quantity = min(quantity, room)
            signed = quantity if suggestion['action'] == 'BUY' else -quantity
        if suggestion['over_limit']:
            self.log(f'  delta {delta:+,.0f} OVER LIMIT, '
                     f'-${suggestion["penalty_per_second"]:,.0f}/s')
        if not self.send(config.UNDERLYING, suggestion['action'], quantity,
                         config.MAX_TRADE_RTM):
            # Rejected: the shares were never bought, so the delta is unchanged
            # and the next chunk should try the same hedge again.
            return delta, underlying
        return delta + signed, underlying + signed

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
