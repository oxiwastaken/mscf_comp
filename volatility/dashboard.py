"""
Live decision-support dashboard for the RITCx Volatility Trading Case.

    python dashboard.py                # live
    python dashboard.py --dry-run      # never sends an order, for practice
    python dashboard.py --manual-hedge # you hedge by hand instead
    python dashboard.py --mode fixed   # override the tail-week prior

OPTION TRADES require a keypress. The keypress IS the confirmation - there is no
second prompt, because in a five-minute heat a confirmation dialog is worse than
useless.

THE DELTA HEDGE IS AUTOMATIC by default, and that asymmetry is deliberate. The
delta penalty accrues every second: being 3,000 over the limit costs $300 per
second, and a single 100-lot of a 0.50-delta option moves delta by 5,000 at
once, while gamma keeps delta drifting even when you are not trading. Hedging
also requires no judgement - the target is a closed-form number - so a human in
that loop adds nothing and costs seconds. Entering option positions is the
opposite: sizing and strike choice are real decisions, so those stay manual.

Auto-hedging is rate-limited to once per tick and uses the 5,000-trigger /
2,000-target hysteresis band, so it cannot oscillate and bleed commission.
Disable it with --manual-hedge if you want the old behaviour.

    h   hedge delta back inside the band (the suggested trade)
    f   flatten delta to zero
    b   BUY the option with the best buy edge
    s   SELL the option with the best sell edge
    d   put on the best dispersion pair (sell richest IV, buy cheapest)
    + / -   adjust order size
    c   cancel all open orders
    q   quit
"""

import argparse
import os
import sys
import time

import config
import news_parser
import vol_engine
from rit_api import ApiException, RitClient

if os.name == 'nt':
    import msvcrt
    os.system('')          # enables ANSI escape handling on Windows terminals
else:
    msvcrt = None

RESET, BOLD, DIM = '\033[0m', '\033[1m', '\033[2m'
RED, GREEN, YELLOW, CYAN, MAGENTA = (
    '\033[31m', '\033[32m', '\033[33m', '\033[36m', '\033[35m')


def fmt(value, width=7, places=2, blank='-'):
    if value is None:
        return blank.rjust(width)
    return f'{value:>{width}.{places}f}'


def pct(value, width=7):
    if value is None:
        return '-'.rjust(width)
    return f'{value * 100:>{width - 1}.2f}%'


def read_key():
    """Non-blocking single keypress, or None."""
    if msvcrt and msvcrt.kbhit():
        try:
            return msvcrt.getch().decode('utf-8', 'ignore').lower()
        except Exception:
            return None
    return None


class Dashboard:
    def __init__(self, client, mode=None, dry_run=False, size=20,
                 auto_hedge=True):
        self.client = client
        self.mode = mode or config.TAIL_VOL_MODE
        self.dry_run = dry_run
        self.size = size
        self.auto_hedge = auto_hedge
        self.messages = []
        self.week_vols = {}
        self.observations = []
        self.delta_limit = config.DELTA_LIMIT
        self._last_hedge_tick = -1

    def log(self, text, colour=''):
        stamp = time.strftime('%H:%M:%S')
        self.messages.append(f'{colour}[{stamp}] {text}{RESET if colour else ""}')
        self.messages = self.messages[-6:]

    # ------------------------------------------------------------- execution
    def _send(self, ticker, action, quantity, max_chunk):
        if quantity <= 0:
            return
        if self.dry_run:
            self.log(f'DRY RUN  would {action} {quantity} {ticker}', DIM)
            return
        try:
            self.client.send_in_chunks(ticker, action, quantity, max_chunk)
            self.log(f'{action} {quantity} {ticker}', GREEN)
        except (ApiException, ValueError) as exc:
            self.log(f'order rejected: {exc}', RED)

    def trade_option(self, row, action):
        if row is None:
            self.log('no option qualifies right now', YELLOW)
            return
        self._send(row['ticker'], action, self.size, config.MAX_TRADE_OPTIONS)

    def hedge(self, suggestion, flatten=False):
        quantity = suggestion['flatten_quantity'] if flatten else suggestion['quantity']
        if quantity <= 0:
            self.log('delta is already comfortable, no hedge needed', DIM)
            return
        if flatten:
            action = 'SELL' if suggestion['delta'] > 0 else 'BUY'
        else:
            action = suggestion['action']
        if action is None:
            self.log('delta is inside the trigger, no hedge suggested', DIM)
            return
        self._send(config.UNDERLYING, action, quantity, config.MAX_TRADE_RTM)

    # ----------------------------------------------------------------- render
    def render(self, chain, case, suggestion, option_gross, option_net):
        out = []
        tick, total = chain['tick'], chain['ticks_total']
        remaining = max(total - tick, 0)

        out.append(f'{BOLD}RITCx VOLATILITY{RESET}   '
                   f'tick {tick}/{total}  ({remaining} left)   '
                   f'week {chain["week"]}/{config.WEEKS_IN_CASE}   '
                   f'{config.UNDERLYING} {fmt(chain["spot"], 6)}   '
                   f'status {case.get("status", "?")}'
                   + (f'   {GREEN}[auto-hedge]{RESET}' if self.auto_hedge
                      else f'   {YELLOW}[MANUAL HEDGE]{RESET}')
                   + (f'   {YELLOW}[DRY RUN]{RESET}' if self.dry_run else ''))

        # forecast breakdown
        forecast = chain['forecast']
        pieces = []
        for part in chain['forecast_detail']:
            colour = CYAN if part['source'] == 'news' else DIM
            pieces.append(f'{colour}w{part["week"]}={part["sigma"] * 100:.1f}%'
                          f'({part["weight"]:.0f}t){RESET}')
        out.append(f'  forecast {BOLD}{pct(forecast)}{RESET}  '
                   f'market {pct(chain["market_avg_iv"])}  '
                   f'gap {pct((forecast - chain["market_avg_iv"]) if forecast and chain["market_avg_iv"] else None)}'
                   f'   tail={self.mode}')
        out.append('  blend: ' + '  '.join(pieces))
        out.append('')

        header = (f'{BOLD}{"TICKER":<9}{"BID":>7}{"ASK":>7}{"MKT_IV":>8}'
                  f'{"FAIR":>8}{"BUY_ED":>8}{"SELL_ED":>9}{"DISP":>8}'
                  f'{"DELTA":>8}{"POS":>6}  ACTION{RESET}')
        out.append(header)

        for row in chain['rows']:
            action, colour = '', ''
            buy, sell = row['buy_edge'], row['sell_edge']
            if buy is not None and buy >= config.MIN_EDGE_DOLLARS:
                action, colour = f'BUY  (+{buy:.3f})', GREEN
            elif sell is not None and sell >= config.MIN_EDGE_DOLLARS:
                action, colour = f'SELL (+{sell:.3f})', RED
            disp = row['dispersion']
            if action == '' and disp is not None and abs(disp) >= config.MIN_DISPERSION_VOL:
                action = 'rich' if disp > 0 else 'cheap'
                colour = DIM

            out.append(
                f'{colour}{row["ticker"]:<9}'
                f'{fmt(row["bid"])}{fmt(row["ask"])}'
                f'{pct(row["market_iv"], 8)}'
                f'{fmt(row["fair"], 8, 3)}'
                f'{fmt(buy, 8, 3)}{fmt(sell, 9, 3)}'
                f'{pct(disp, 8)}'
                f'{fmt(row["delta"], 8, 3)}'
                f'{row["position"]:>6}  {action}{RESET}')

        out.append('')

        # risk line
        delta = chain['portfolio_delta']
        if suggestion['over_limit']:
            colour = RED + BOLD
            note = (f'  OVER LIMIT  penalty '
                    f'${suggestion["penalty_per_second"]:.2f}/sec')
        elif abs(delta) > config.HEDGE_TRIGGER_DELTA:
            colour, note = YELLOW, '  approaching limit'
        else:
            colour, note = GREEN, ''
        out.append(f'{colour}  portfolio delta {delta:>+10,.0f}'
                   f'   limit +/-{self.delta_limit:,}{note}{RESET}')

        if suggestion['action']:
            out.append(f'  suggested hedge: {BOLD}{suggestion["action"]} '
                       f'{suggestion["quantity"]:,} {config.UNDERLYING}{RESET}'
                       f'   (flatten would be {suggestion["flatten_quantity"]:,})')

        out.append(f'{DIM}  options gross {option_gross}/{config.LIMIT_OPT_GROSS}'
                   f'   net {option_net:+}/{config.LIMIT_OPT_NET}'
                   f'   {config.UNDERLYING} pos {chain["underlying_position"]:+,}'
                   f'   order size {self.size}{RESET}')

        out.append('')
        for message in self.messages:
            out.append('  ' + message)

        out.append(f'\n{DIM}  [h]edge  [f]latten  [b]uy  [s]ell  [d]ispersion'
                   f'  [a]uto-hedge toggle  [+/-]size  [c]ancel  [q]uit{RESET}')

        sys.stdout.write('\033[H\033[J' + '\n'.join(out) + '\n')
        sys.stdout.flush()

    # ------------------------------------------------------------------- loop
    def run(self):
        last_news_count = -1
        while True:
            try:
                case = self.client.case()
                securities = self.client.securities()
                news = self.client.news()
            except ApiException as exc:
                sys.stdout.write('\033[H\033[J')
                print(f'{RED}{exc}{RESET}\nRetrying...')
                time.sleep(1.5)
                continue

            tick = case.get('tick', 0)
            total = case.get('ticks_per_period') or 300

            if len(news) != last_news_count:
                last_news_count = len(news)
                self.week_vols, self.observations = news_parser.parse_vol_news(
                    news, total)
                announced = news_parser.parse_delta_limit(news)
                if announced and announced != self.delta_limit:
                    self.delta_limit = announced
                    self.log(f'delta limit announced: {announced:,}', MAGENTA)
                if self.observations:
                    latest = self.observations[-1]
                    kind = 'exact' if latest['exact'] else 'range'
                    self.log(f'news: week {latest["week"]} vol '
                             f'{latest["sigma"] * 100:.1f}% ({kind})', CYAN)

            try:
                chain = vol_engine.build_chain(
                    securities, tick, total, self.week_vols, mode=self.mode)
            except ValueError as exc:
                sys.stdout.write('\033[H\033[J')
                print(f'{RED}{exc}{RESET}')
                time.sleep(1.5)
                continue

            suggestion = vol_engine.hedge_suggestion(
                chain['portfolio_delta'], limit=self.delta_limit)

            # Auto-hedge. The delta penalty accrues every second and hedging
            # needs no judgement - the target is a closed-form number - so a
            # human in this loop adds nothing and costs money. Option entry
            # stays manual, where judgement actually matters.
            #
            # Rate-limited to once per tick. Market orders fill instantly in
            # this case, so one hedge per tick is enough, and the 5,000/2,000
            # hysteresis band stops it oscillating and bleeding fees.
            if (self.auto_hedge and suggestion['action']
                    and tick != self._last_hedge_tick):
                self._last_hedge_tick = tick
                self.log(f'auto-hedge: {suggestion["action"]} '
                         f'{suggestion["quantity"]:,}', YELLOW)
                self.hedge(suggestion)

            option_gross = sum(abs(r['position']) for r in chain['rows'])
            option_net = sum(r['position'] for r in chain['rows'])

            self.render(chain, case, suggestion, option_gross, option_net)

            key = read_key()
            if key == 'q':
                break
            elif key == 'h':
                self.hedge(suggestion)
            elif key == 'f':
                self.hedge(suggestion, flatten=True)
            elif key == 'b':
                self.trade_option(self._best(chain, 'buy_edge'), 'BUY')
            elif key == 's':
                self.trade_option(self._best(chain, 'sell_edge'), 'SELL')
            elif key == 'd':
                self.dispersion_pair(chain)
            elif key == 'a':
                self.auto_hedge = not self.auto_hedge
                self.log(f'auto-hedge {"ON" if self.auto_hedge else "OFF"}',
                         GREEN if self.auto_hedge else YELLOW)
            elif key in ('+', '='):
                self.size = min(self.size + 10, config.MAX_TRADE_OPTIONS)
            elif key in ('-', '_'):
                self.size = max(self.size - 10, 10)
            elif key == 'c':
                try:
                    self.client.cancel_all()
                    self.log('cancelled all open orders', YELLOW)
                except ApiException as exc:
                    self.log(f'cancel failed: {exc}', RED)

            time.sleep(config.REFRESH_SECONDS)

    def _best(self, chain, field):
        candidates = [r for r in chain['rows']
                      if r[field] is not None and r[field] >= config.MIN_EDGE_DOLLARS]
        return max(candidates, key=lambda r: r[field]) if candidates else None

    def dispersion_pair(self, chain):
        """
        Sell the richest implied vol and buy the cheapest.

        This trade does not depend on our volatility forecast being right - it
        only requires the market maker to be quoting two strikes at different
        implied vols when, sharing one expiry and one pricing model, they should
        be identical.
        """
        rows = [r for r in chain['rows'] if r['dispersion'] is not None]
        if len(rows) < 2:
            self.log('not enough implied vols to compare', YELLOW)
            return
        richest = max(rows, key=lambda r: r['dispersion'])
        cheapest = min(rows, key=lambda r: r['dispersion'])
        spread = richest['dispersion'] - cheapest['dispersion']
        if spread < config.MIN_DISPERSION_VOL:
            self.log(f'dispersion only {spread * 100:.2f}% - not worth fees', DIM)
            return
        self.log(f'dispersion {spread * 100:.2f}%: '
                 f'sell {richest["ticker"]} / buy {cheapest["ticker"]}', MAGENTA)
        self._send(richest['ticker'], 'SELL', self.size, config.MAX_TRADE_OPTIONS)
        self._send(cheapest['ticker'], 'BUY', self.size, config.MAX_TRADE_OPTIONS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='never send orders')
    parser.add_argument('--mode', choices=('market', 'fixed', 'persist'),
                        help='prior for unannounced weeks')
    parser.add_argument('--size', type=int, default=20,
                        help='contracts per option order')
    parser.add_argument('--manual-hedge', action='store_true',
                        help='disable auto-hedging (not recommended: the delta '
                             'penalty accrues every second and hedging needs '
                             'no judgement)')
    parser.add_argument('--host', default=config.API_HOST)
    args = parser.parse_args()

    client = RitClient(host=args.host)
    Dashboard(client, mode=args.mode, dry_run=args.dry_run,
              size=args.size, auto_hedge=not args.manual_hedge).run()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nstopped')
