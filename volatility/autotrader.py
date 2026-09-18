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

# Enter when our forecast disagrees with the market by at least this much,
# in vol points. 0.01 = 1 volatility point.
ENTRY_VOL_EDGE = 0.010
# Scale to full size by the time the gap reaches this.
FULL_SIZE_VOL_EDGE = 0.040
# Close out once the gap decays below this - the maker has learned.
EXIT_VOL_EDGE = 0.004
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
            nearest = min(usable, key=lambda k: abs(k - spot))
            if abs(self._target_strike - spot) <= abs(nearest - spot) + 1.0:
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

        chain = vol_engine.build_chain(securities, tick, total, week_vols,
                                       mode=self.mode)
        if not chain['live'] or not chain['forecast'] or not chain['market_avg_iv']:
            return True

        gap = chain['forecast'] - chain['market_avg_iv']
        _gross_limit, net_limit = self.option_limits()

        strike, by_strike = self.pick_strike(chain)
        if strike is None:
            self.hedge(chain)
            return True

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
                     f'gap {gap * 100:+5.2f}%  '
                     f'K{strike:.0f}  held {held:+.0f}  target {target:+d}'
                     + ('  [late]' if late else ''))

        self.rebalance(call, put, target)
        self.close_other_strikes(chain, strike)
        self.hedge(chain)
        return True

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
            if target != 0 and abs(change) < REBALANCE_MIN:
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

    def hedge(self, chain):
        suggestion = vol_engine.hedge_suggestion(
            chain['portfolio_delta'], limit=self.delta_limit)
        if not suggestion['action'] or suggestion['quantity'] <= 0:
            return
        quantity = suggestion['quantity']
        signed = quantity if suggestion['action'] == 'BUY' else -quantity
        projected = chain['underlying_position'] + signed
        if abs(projected) > config.LIMIT_RTM_GROSS:
            allowed = config.LIMIT_RTM_GROSS - abs(chain['underlying_position'])
            if allowed <= 0:
                self.log(f'  CANNOT HEDGE: delta {chain["portfolio_delta"]:+,.0f} '
                         f'but RTM position is at its limit')
                return
            quantity = min(quantity, allowed)
        if suggestion['over_limit']:
            self.log(f'  delta {chain["portfolio_delta"]:+,.0f} OVER LIMIT, '
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
