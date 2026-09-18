"""
Always-on-top overlay that floats over the RIT client.

    python overlay.py                      # display only (safe default)
    python overlay.py --trade              # enable buttons + global hotkeys
    python overlay.py --trade --auto-hedge # also manages delta in background
    python overlay.py --geometry +1200+700 --opacity 0.85

RIT has no plugin API - the workspace format only accepts its own fixed window
types - so analytics cannot be drawn inside the client itself. This is the
closest workable thing: a compact, borderless, always-on-top panel you park over
a spare corner of your RIT workspace and read while trading.

TRADING IS OFF BY DEFAULT. Pass --trade to arm it. Once armed there are two
ways to fire, and the difference matters under time pressure:

  BUTTONS     obvious and discoverable, but clicking the panel takes focus away
              from RIT, so you must click back into RIT afterwards.

  HOTKEYS     ctrl+alt+B / S / H / F. These are registered system-wide, so they
              fire WITHOUT leaving RIT - your hands never leave the RIT window.
              This is the one to use during a heat.

              ctrl+alt+B   buy the best buy-edge option
              ctrl+alt+S   sell the best sell-edge option
              ctrl+alt+H   hedge delta back inside the band
              ctrl+alt+F   flatten delta to zero

ctrl+alt combinations are used precisely because RIT will never see them as
ordinary typing. Hotkeys are only registered when --trade is passed, so an
unarmed overlay cannot fire anything.

Drag the panel by its title bar. Right-click it to close.
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import tkinter as tk
from tkinter import font as tkfont

import config
import news_parser
import vol_engine
from rit_api import ApiException, RitClient

BG = '#0d1117'
PANEL = '#161b22'
FG = '#c9d1d9'
DIM = '#6e7681'
GREEN = '#3fb950'
RED = '#f85149'
YELLOW = '#d29922'
CYAN = '#58a6ff'

# Win32 hotkey plumbing. RegisterHotKey posts WM_HOTKEY to this thread's message
# queue; tkinter never drains it, so we drain it ourselves from the poll loop.
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY, PM_REMOVE = 0x0312, 0x0001
HOTKEYS = {1: ('B', 0x42), 2: ('S', 0x53), 3: ('H', 0x48), 4: ('F', 0x46)}


class Overlay:
    def __init__(self, root, client, trade=False, auto_hedge=False, mode=None,
                 opacity=0.92, size=20):
        self.root = root
        self.client = client
        self.trade_enabled = trade
        self.auto_hedge = auto_hedge
        self.mode = mode or config.TAIL_VOL_MODE
        self.size = size
        self.delta_limit = config.DELTA_LIMIT
        self.chain = None
        self.suggestion = None
        self.note = ''
        self._last_hedge_tick = -1
        self._drag = (0, 0)
        self._hotkeys = []

        root.overrideredirect(True)
        root.attributes('-topmost', True)
        root.attributes('-alpha', opacity)
        root.configure(bg=BG)

        mono = tkfont.Font(family='Consolas', size=9)
        mono_bold = tkfont.Font(family='Consolas', size=9, weight='bold')
        small = tkfont.Font(family='Consolas', size=8)

        bar = tk.Frame(root, bg=PANEL)
        bar.pack(fill='x')
        self.title = tk.Label(bar, text=' RITCx VOL', bg=PANEL, fg=CYAN,
                              font=mono_bold, anchor='w')
        self.title.pack(side='left')
        self.status = tk.Label(bar, text='', bg=PANEL, fg=DIM, font=mono)
        self.status.pack(side='right', padx=4)
        for widget in (bar, self.title, self.status):
            widget.bind('<Button-1>', self._grab)
            widget.bind('<B1-Motion>', self._move)
            widget.bind('<Button-3>', lambda _e: self.close())

        self.body = tk.Label(root, text='connecting...', bg=BG, fg=FG,
                             font=mono, justify='left', anchor='nw',
                             padx=8, pady=6)
        self.body.pack(fill='both', expand=True)
        self.body.bind('<Button-3>', lambda _e: self.close())

        if self.trade_enabled:
            self._build_buttons(small)
            self._register_hotkeys()
        else:
            tk.Label(root, text=' display only - restart with --trade to arm ',
                     bg=PANEL, fg=DIM, font=small).pack(fill='x')

        self.root.after(200, self.tick)

    def _build_buttons(self, font):
        row = tk.Frame(self.root, bg=PANEL)
        row.pack(fill='x')

        def button(text, command, colour=FG):
            widget = tk.Button(row, text=text, command=command, font=font,
                               bg=PANEL, fg=colour, activebackground='#21262d',
                               activeforeground=colour, relief='flat',
                               bd=0, padx=6, pady=2, highlightthickness=0,
                               cursor='hand2')
            widget.pack(side='left')
            return widget

        button('BUY', self.buy_best, GREEN)
        button('SELL', self.sell_best, RED)
        button('HEDGE', self.do_hedge, YELLOW)
        button('FLAT', self.do_flatten, YELLOW)
        button('-', lambda: self.bump(-10), DIM)
        self.size_label = tk.Label(row, text=str(self.size), bg=PANEL, fg=FG,
                                   font=font)
        self.size_label.pack(side='left')
        button('+', lambda: self.bump(10), DIM)
        self.auto_button = button('AUTO', self.toggle_auto,
                                  GREEN if self.auto_hedge else DIM)

    # ------------------------------------------------------------- hotkeys
    def _register_hotkeys(self):
        user32 = ctypes.windll.user32
        mods = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
        for hotkey_id, (name, vk) in HOTKEYS.items():
            if user32.RegisterHotKey(None, hotkey_id, mods, vk):
                self._hotkeys.append(hotkey_id)
            else:
                self.note = f'ctrl+alt+{name} unavailable (already in use)'

    def _drain_hotkeys(self):
        """Pull WM_HOTKEY messages off this thread's queue and act on them."""
        if not self._hotkeys:
            return
        msg = wintypes.MSG()
        user32 = ctypes.windll.user32
        while user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY,
                                  WM_HOTKEY, PM_REMOVE):
            action = {1: self.buy_best, 2: self.sell_best,
                      3: self.do_hedge, 4: self.do_flatten}.get(msg.wParam)
            if action:
                action()

    def close(self):
        user32 = ctypes.windll.user32
        for hotkey_id in self._hotkeys:
            user32.UnregisterHotKey(None, hotkey_id)
        self._hotkeys = []
        self.root.destroy()

    # ------------------------------------------------------------- dragging
    def _grab(self, event):
        self._drag = (event.x_root - self.root.winfo_x(),
                      event.y_root - self.root.winfo_y())

    def _move(self, event):
        self.root.geometry(f'+{event.x_root - self._drag[0]}'
                           f'+{event.y_root - self._drag[1]}')

    # -------------------------------------------------------------- actions
    def bump(self, delta):
        self.size = max(10, min(self.size + delta, config.MAX_TRADE_OPTIONS))
        self.size_label.config(text=str(self.size))

    def toggle_auto(self):
        self.auto_hedge = not self.auto_hedge
        self.auto_button.config(fg=GREEN if self.auto_hedge else DIM)
        self.note = f'auto-hedge {"ON" if self.auto_hedge else "OFF"}'

    def _guard(self):
        """Refuse to trade unless armed and looking at a live, quoting heat."""
        if not self.trade_enabled:
            self.note = 'not armed (restart with --trade)'
            return False
        if not self.chain or not self.chain.get('live'):
            self.note = 'no live quotes - refusing to trade'
            return False
        return True

    def _send(self, ticker, action, quantity, max_chunk):
        if quantity <= 0:
            return
        try:
            self.client.send_in_chunks(ticker, action, quantity, max_chunk)
            self.note = f'{action} {quantity:,} {ticker}'
        except (ApiException, ValueError) as exc:
            self.note = f'rejected: {exc}'

    def _best(self, field):
        rows = [r for r in self.chain['rows'] if r['tradeable']
                and r[field] is not None and r[field] >= config.MIN_EDGE_DOLLARS]
        return max(rows, key=lambda r: r[field]) if rows else None

    def buy_best(self):
        if not self._guard():
            return
        row = self._best('buy_edge')
        if not row:
            self.note = 'no option clears the buy threshold'
            return
        self._send(row['ticker'], 'BUY', self.size, config.MAX_TRADE_OPTIONS)

    def sell_best(self):
        if not self._guard():
            return
        row = self._best('sell_edge')
        if not row:
            self.note = 'no option clears the sell threshold'
            return
        self._send(row['ticker'], 'SELL', self.size, config.MAX_TRADE_OPTIONS)

    def do_hedge(self, flatten=False):
        if not self._guard() or not self.suggestion:
            return
        suggestion = self.suggestion
        quantity = (suggestion['flatten_quantity'] if flatten
                    else suggestion['quantity'])
        if quantity <= 0:
            self.note = 'delta already comfortable'
            return
        if flatten:
            action = 'SELL' if suggestion['delta'] > 0 else 'BUY'
        else:
            action = suggestion['action']
        if not action:
            self.note = 'delta inside the trigger'
            return
        self._send(config.UNDERLYING, action, quantity, config.MAX_TRADE_RTM)

    def do_flatten(self):
        self.do_hedge(flatten=True)

    # ---------------------------------------------------------------- render
    def tick(self):
        try:
            self._drain_hotkeys()
            self._refresh()
        except ApiException as exc:
            self.status.config(text='no api', fg=RED)
            self.body.config(text=str(exc), fg=RED)
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

        self.chain = vol_engine.build_chain(securities, tick, total, week_vols,
                                            mode=self.mode)
        chain = self.chain

        if case.get('status') != 'ACTIVE' or not chain['live']:
            self.suggestion = None
            self.status.config(text=case.get('status', '?').lower(), fg=YELLOW)
            self.body.config(
                text=f'no live quotes\n\ntick {tick}/{total}\n'
                     f'{config.UNDERLYING} {chain["spot"]:.2f} (stale)\n\n'
                     f'waiting for a heat'
                     + (f'\n\n{self.note}' if self.note else ''), fg=DIM)
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
        self.suggestion = vol_engine.hedge_suggestion(
            delta, limit=self.delta_limit)
        suggestion = self.suggestion

        lines.append('')
        risk = f'delta {delta:>+9,.0f} / {self.delta_limit:,}'
        if suggestion['over_limit']:
            risk += f'   OVER  -${suggestion["penalty_per_second"]:,.0f}/s'
        elif suggestion['action']:
            risk += f'   hedge {suggestion["action"]} {suggestion["quantity"]:,}'
        lines.append(risk)

        if (self.auto_hedge and self.trade_enabled and suggestion['action']
                and tick != self._last_hedge_tick):
            self._last_hedge_tick = tick
            self._send(config.UNDERLYING, suggestion['action'],
                       suggestion['quantity'], config.MAX_TRADE_RTM)

        if self.note:
            lines.append('')
            lines.append(self.note)

        colour = FG
        if suggestion['over_limit']:
            colour = RED
        elif abs(delta) > config.HEDGE_TRIGGER_DELTA:
            colour = YELLOW
        elif abs(gap) * 100 >= 1.0:
            colour = GREEN

        self.body.config(text='\n'.join(lines), fg=colour)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--trade', action='store_true',
                        help='arm the buttons and ctrl+alt hotkeys')
    parser.add_argument('--auto-hedge', action='store_true',
                        help='manage delta in the background (needs --trade)')
    parser.add_argument('--mode', choices=('market', 'fixed', 'persist'))
    parser.add_argument('--size', type=int, default=20,
                        help='contracts per option order')
    parser.add_argument('--opacity', type=float, default=0.92)
    parser.add_argument('--geometry', default='+40+40',
                        help='initial position, e.g. +1200+700')
    args = parser.parse_args()

    if args.auto_hedge and not args.trade:
        parser.error('--auto-hedge requires --trade')

    root = tk.Tk()
    root.geometry(args.geometry)
    Overlay(root, RitClient(), trade=args.trade, auto_hedge=args.auto_hedge,
            mode=args.mode, opacity=args.opacity, size=args.size)
    root.mainloop()


if __name__ == '__main__':
    main()
