"""
Always-on-top overlay that floats over the RIT client.

    python overlay.py                  # display only (safe default)
    python overlay.py --auto-hedge     # also manages delta in the background
    python overlay.py --opacity 0.85

RIT has no plugin API - the workspace format only accepts its own fixed window
types - so analytics cannot be drawn inside the client itself. This is the
closest workable thing: a compact, borderless, always-on-top panel you park over
a spare corner of your RIT workspace and read while trading in RIT's own
BookTrader.

It deliberately never takes keyboard focus, so your typing always goes to RIT.
Drag it by its title bar; right-click it to close.

DISPLAY ONLY BY DEFAULT. It sends no orders unless you pass --auto-hedge, and
even then it only ever trades RTM shares to control delta, never options.
"""

import argparse
import tkinter as tk
from tkinter import font as tkfont

import config
import news_parser
import vol_engine
from rit_api import ApiException, RitClient

BG = '#0d1117'
FG = '#c9d1d9'
DIM = '#6e7681'
GREEN = '#3fb950'
RED = '#f85149'
YELLOW = '#d29922'
CYAN = '#58a6ff'
HEAD = '#8b949e'


class Overlay:
    def __init__(self, root, client, auto_hedge=False, mode=None, opacity=0.92):
        self.root = root
        self.client = client
        self.auto_hedge = auto_hedge
        self.mode = mode or config.TAIL_VOL_MODE
        self.delta_limit = config.DELTA_LIMIT
        self._last_hedge_tick = -1
        self._drag = (0, 0)

        root.overrideredirect(True)              # no OS title bar
        root.attributes('-topmost', True)
        root.attributes('-alpha', opacity)
        root.configure(bg=BG)

        mono = tkfont.Font(family='Consolas', size=9)
        mono_bold = tkfont.Font(family='Consolas', size=9, weight='bold')

        bar = tk.Frame(root, bg='#161b22')
        bar.pack(fill='x')
        self.title = tk.Label(bar, text=' RITCx VOL', bg='#161b22', fg=CYAN,
                              font=mono_bold, anchor='w')
        self.title.pack(side='left')
        self.status = tk.Label(bar, text='', bg='#161b22', fg=DIM, font=mono)
        self.status.pack(side='right', padx=4)

        for widget in (bar, self.title, self.status):
            widget.bind('<Button-1>', self._grab)
            widget.bind('<B1-Motion>', self._move)
            widget.bind('<Button-3>', lambda _e: self.root.destroy())

        self.body = tk.Label(root, text='connecting...', bg=BG, fg=FG,
                             font=mono, justify='left', anchor='nw', padx=8,
                             pady=6)
        self.body.pack(fill='both', expand=True)
        self.body.bind('<Button-3>', lambda _e: self.root.destroy())

        self.root.after(200, self.tick)

    # ------------------------------------------------------------- dragging
    def _grab(self, event):
        self._drag = (event.x_root - self.root.winfo_x(),
                      event.y_root - self.root.winfo_y())

    def _move(self, event):
        self.root.geometry(f'+{event.x_root - self._drag[0]}'
                           f'+{event.y_root - self._drag[1]}')

    # ---------------------------------------------------------------- render
    def tick(self):
        try:
            self._refresh()
        except ApiException as exc:
            self.status.config(text='no api', fg=RED)
            self.body.config(text=f'{exc}', fg=RED)
        except Exception as exc:                      # never kill the window
            self.status.config(text='error', fg=RED)
            self.body.config(text=f'{type(exc).__name__}: {exc}', fg=RED)
        self.root.after(400, self.tick)

    def _refresh(self):
        case = self.client.case()
        securities = self.client.securities()
        news = self.client.news()

        tick = case.get('tick', 0)
        total = case.get('ticks_per_period') or 300
        week_vols, _ = news_parser.parse_vol_news(news, total)
        announced = news_parser.parse_delta_limit(news)
        if announced:
            self.delta_limit = announced

        chain = vol_engine.build_chain(securities, tick, total, week_vols,
                                       mode=self.mode)

        if case.get('status') != 'ACTIVE' or not chain['live']:
            self.status.config(text=case.get('status', '?').lower(), fg=YELLOW)
            self.body.config(
                text=f'no live quotes\n\ntick {tick}/{total}\n'
                     f'{config.UNDERLYING} {chain["spot"]:.2f} (stale)\n\n'
                     'waiting for a heat', fg=DIM)
            return

        self.status.config(text=f'{tick}/{total}  w{chain["week"]}', fg=DIM)

        forecast = chain['forecast'] or 0
        market = chain['market_avg_iv'] or 0
        gap = forecast - market
        lines = [
            f'{config.UNDERLYING} {chain["spot"]:>7.2f}'
            f'    fcst {forecast * 100:>5.2f}%'
            f'  mkt {market * 100:>5.2f}%'
            f'  gap {gap * 100:>+5.2f}%',
            '',
            f'{"":<8}{"IV":>7}{"EDGE":>8}  ACTION',
        ]

        for row in chain['rows']:
            if not row['tradeable']:
                continue
            buy, sell = row['buy_edge'], row['sell_edge']
            action, edge = '', 0.0
            if buy is not None and buy >= config.MIN_EDGE_DOLLARS:
                action, edge = 'BUY', buy
            elif sell is not None and sell >= config.MIN_EDGE_DOLLARS:
                action, edge = 'SELL', sell
            lines.append(
                f'{row["ticker"]:<8}{(row["market_iv"] or 0) * 100:>6.2f}%'
                f'{edge:>8.3f}  {action}'
                + (f'  pos {row["position"]:+.0f}' if row['position'] else ''))

        delta = chain['portfolio_delta']
        suggestion = vol_engine.hedge_suggestion(delta, limit=self.delta_limit)

        lines.append('')
        risk = f'delta {delta:>+9,.0f} / {self.delta_limit:,}'
        if suggestion['over_limit']:
            risk += f'   OVER  -${suggestion["penalty_per_second"]:,.0f}/s'
        elif suggestion['action']:
            risk += f'   hedge {suggestion["action"]} {suggestion["quantity"]:,}'
        lines.append(risk)

        if self.auto_hedge and suggestion['action'] and tick != self._last_hedge_tick:
            self._last_hedge_tick = tick
            try:
                self.client.send_in_chunks(
                    config.UNDERLYING, suggestion['action'],
                    suggestion['quantity'], config.MAX_TRADE_RTM)
                lines.append(f'auto-hedged {suggestion["action"]} '
                             f'{suggestion["quantity"]:,}')
            except (ApiException, ValueError) as exc:
                lines.append(f'hedge failed: {exc}')

        colour = FG
        if suggestion['over_limit']:
            colour = RED
        elif abs(delta) > config.HEDGE_TRIGGER_DELTA:
            colour = YELLOW
        elif abs(gap) * 100 >= 1.0:
            colour = GREEN

        self.body.config(text='\n'.join(lines), fg=colour)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--auto-hedge', action='store_true',
                        help='manage delta in the background (trades RTM only)')
    parser.add_argument('--mode', choices=('market', 'fixed', 'persist'))
    parser.add_argument('--opacity', type=float, default=0.92)
    parser.add_argument('--geometry', default='+40+40',
                        help='initial position, e.g. +1200+700')
    args = parser.parse_args()

    root = tk.Tk()
    root.geometry(args.geometry)
    Overlay(root, RitClient(), auto_hedge=args.auto_hedge, mode=args.mode,
            opacity=args.opacity)
    root.mainloop()


if __name__ == '__main__':
    main()
