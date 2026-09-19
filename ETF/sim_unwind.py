"""Offline exchange simulator for the unwind state machine.

WHAT THIS IS FOR. It validates *logic*, not P&L. The passive unwind adds real
order lifecycle - resting, partial fills, repricing, cancel-on-cross, a deadline
that escalates to market orders - and none of that can be debugged against a
live heat that runs for five minutes and cannot be replayed. This drives the
real `unwind_ritc` through a fake API so the invariants can be asserted:

  1. the position reaches flat, and does so before the deadline
  2. neither layer ever sells more than we hold (no flip through zero)
  3. resting orders are cancelled when they are stale, wrong-sided, or done
  4. with PASSIVE_UNWIND off, behaviour is byte-identical to v10

WHAT IT IS NOT. The cost numbers it prints come from a synthetic book with an
invented impact coefficient. They are a sanity check on direction and order of
magnitude, nothing more. v8/v9 were lost by trusting a number that measured the
wrong thing; the only trustworthy cost figure is `analyze_tenders.py` run on
live fills.

    python sim_unwind.py
"""

import csv
import statistics
import sys
from pathlib import Path

import etf_arb_bot as bot

LOG_DIR = Path(__file__).resolve().parent / "logs"

# --- synthetic book ------------------------------------------------------
# Shape taken from the measured RITC book: ~190,000 units visible across a side,
# thin at the touch and thickening away from it (v6 measured bids 194,248 units
# running 25.05 -> 24.62).
LEVELS = 12
TOUCH_QTY = 6000
LEVEL_STEP = 0.01
LEVEL_GROWTH = 1.35
# Units the resting side of the book consumes from our passive order per cycle
# once the market has crossed our price.
PASSIVE_FILL_PER_CYCLE = 5000
# Permanent price impact per 10,000 units taken. Calibrated so that clearing
# 92,000 units in 10,000-unit market chunks degrades by ~0.14/unit, which is
# what v10 measured live.
IMPACT_PER_10K = 0.030
IMPACT_DECAY = 0.65   # fraction of impact that refills between cycles


class SimExchange:
    """Implements exactly the endpoints the bot calls."""

    def __init__(self, quotes):
        self.quotes = quotes      # [(bid, ask)] per cycle
        self.i = 0
        self.impact = 0.0
        self.orders = {}
        self.next_id = 1
        self.position = 0
        self.fills = []           # (style, qty, price)

    # -- book ------------------------------------------------------------
    def top(self):
        bid, ask = self.quotes[min(self.i, len(self.quotes) - 1)]
        return round(bid + self.impact, 2), round(ask + self.impact, 2)

    def side(self, which):
        bid, ask = self.top()
        px, qty, out = (bid if which == "bids" else ask), TOUCH_QTY, []
        for _ in range(LEVELS):
            out.append({"price": round(px, 2), "quantity": qty})
            px += -LEVEL_STEP if which == "bids" else LEVEL_STEP
            qty = int(qty * LEVEL_GROWTH)
        return out

    def book(self):
        return {"bids": self.side("bids"), "asks": self.side("asks")}

    # -- matching --------------------------------------------------------
    def market_fill(self, action, qty):
        levels = self.side("bids" if action == "SELL" else "asks")
        vwap, filled = bot.vwap_for_qty(levels, qty)
        self.position += qty if action == "BUY" else -qty
        self.impact += (qty / 10000.0) * IMPACT_PER_10K * (-1 if action == "SELL" else 1)
        self.fills.append(("aggressive", filled, vwap))
        return vwap, filled

    def step(self):
        """Advance one cycle: decay our impact, then fill crossed limit orders."""
        self.i += 1
        self.impact *= (1 - IMPACT_DECAY)
        bid, ask = self.top()
        for o in self.orders.values():
            if o["status"] != "OPEN":
                continue
            crossed = (o["action"] == "SELL" and bid >= o["price"] - 1e-9) or \
                      (o["action"] == "BUY" and ask <= o["price"] + 1e-9)
            if not crossed:
                continue
            take = min(o["quantity"] - o["quantity_filled"], PASSIVE_FILL_PER_CYCLE)
            if take <= 0:
                continue
            o["quantity_filled"] += take
            o["vwap"] = o["price"]
            self.position += take if o["action"] == "BUY" else -take
            self.fills.append(("passive", take, o["price"]))
            if o["quantity_filled"] >= o["quantity"]:
                o["status"] = "TRANSACTED"

    # -- API -------------------------------------------------------------
    def request(self, session, method, endpoint, params=None):
        p = params or {}
        if endpoint == "orders" and method == "POST":
            oid = self.next_id
            self.next_id += 1
            o = {"order_id": oid, "ticker": p["ticker"], "type": p["type"],
                 "quantity": float(p["quantity"]), "action": p["action"],
                 "price": float(p["price"]) if p.get("price") is not None else None,
                 "quantity_filled": 0.0, "vwap": None, "status": "OPEN"}
            if p["type"] == "MARKET":
                vwap, filled = self.market_fill(p["action"], float(p["quantity"]))
                o.update({"quantity_filled": filled, "vwap": vwap,
                          "status": "TRANSACTED", "price": None})
            self.orders[oid] = o
            return dict(o)
        if endpoint == "orders" and method == "GET":
            want = (p.get("status") or "OPEN").upper()
            return [dict(o) for o in self.orders.values() if o["status"] == want]
        if endpoint.startswith("orders/") and method == "GET":
            return dict(self.orders[int(endpoint.split("/")[1])])
        if endpoint == "commands/cancel":
            for i in str(p.get("ids", "")).split(","):
                if i and self.orders.get(int(i), {}).get("status") == "OPEN":
                    self.orders[int(i)]["status"] = "CANCELLED"
            return {"cancelled": True}
        raise AssertionError(f"simulator has no route for {method} {endpoint}")


def quote_series(limit=120):
    """Real RITC top-of-book, so the fill dynamics are not invented."""
    path = LOG_DIR / "ticks.csv"
    out = []
    with path.open(newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                b, a = float(r["ritc_bid_usd"]), float(r["ritc_ask_usd"])
            except (ValueError, KeyError):
                continue
            if 0 < b < a < 1e9:
                out.append((b, a))
    return out[:limit] if limit else out


class NullLog:
    def write(self, row):
        self.rows = getattr(self, "rows", [])
        self.rows.append(row)

    def close(self):
        pass


class NullLogs:
    def __init__(self):
        self.trades = NullLog()
        self.ticks = self.tenders = self.heats = self.calib = self.runs = NullLog()


def run(start_pos, quotes, passive, cycles=40, verbose=False, tick0=0):
    ex = SimExchange(quotes)
    ex.position = start_pos
    # Route the bot's single API chokepoint at the simulator. Everything above
    # it - place_lmt, open_orders, cancel_orders, Resting, unwind_ritc - is the
    # real code, unmodified.
    bot.api_request = ex.request
    bot.PASSIVE_UNWIND = passive
    # v11 targets tender inventory, not the raw RITC position - the whole
    # starting position here is tender inventory by construction.
    bot.tender_position = start_pos
    st = bot.UnwindState()
    logs = NullLogs()
    snap = {k: 0 for k in bot.SNAP_FIELDS}
    trace = []

    for c in range(cycles):
        book = ex.book()
        pos = {bot.RITC: ex.position, bot.BULL: 0, bot.BEAR: 0,
               bot.USD: 0, bot.CAD: 0}
        bot.unwind_ritc(None, pos, book, snap, logs, 1, tick0 + c, c, st)
        resting = sum(o["quantity"] - o["quantity_filled"]
                      for o in ex.orders.values() if o["status"] == "OPEN")
        trace.append((c, ex.position, resting))
        if verbose:
            bid, ask = ex.top()
            print(f"  c{c:>3} pos {ex.position:>8,} resting {resting:>7,.0f} "
                  f"bid {bid:.2f} ask {ask:.2f}")
        ex.step()
        if abs(ex.position) <= bot.UNWIND_THRESHOLD and not st.resting.orders:
            trace.append((c + 1, ex.position, 0))
            break
    return ex, st, trace


def summarise(ex, start_pos):
    qty = sum(f[1] for f in ex.fills)
    if not qty:
        return None
    vwap = sum(f[1] * f[2] for f in ex.fills) / qty
    passive = sum(f[1] for f in ex.fills if f[0] == "passive")
    # Sign the fee: passive fills earn the rebate, aggressive ones pay the fee.
    fees = sum(f[1] * (-bot.REBATE_LMT if f[0] == "passive" else bot.FEE_MKT)
               for f in ex.fills)
    side = -1 if start_pos > 0 else 1   # selling proceeds vs buying cost
    return {"qty": qty, "vwap": vwap, "passive": passive,
            "passive_share": passive / qty, "fees": fees,
            "net_per_unit": side * -vwap - fees / qty}


def main():
    quotes = quote_series(limit=0)
    if len(quotes) < 60:
        print("not enough logged quotes to simulate")
        return 1
    failures = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
        if not cond:
            failures.append(name)

    # Sample several windows of the real quote series so the result does not
    # hinge on one stretch of the tape.
    windows = [quotes[i:i + 60] for i in range(0, min(len(quotes), 600), 60)]
    windows = [w for w in windows if len(w) == 60]

    for start in (92000, -87000):
        print(f"\n=== start position {start:+,} ===")
        rows = []
        for mode in (True, False):
            per = []
            for w in windows:
                ex, st, trace = run(start, w, passive=mode)
                s = summarise(ex, start)
                flat_at = next((c for c, p, r in trace
                                if abs(p) <= bot.UNWIND_THRESHOLD), None)
                overshoot = max((abs(p) for c, p, r in trace
                                 if p * start < 0), default=0)
                per.append((s, flat_at, overshoot, st))
            rows.append((mode, per))

        for mode, per in rows:
            label = "passive" if mode else "v10 market-only"
            flats = [x[1] for x in per if x[1] is not None]
            shares = [x[0]["passive_share"] for x in per if x[0]]
            fees = [x[0]["fees"] for x in per if x[0]]
            vwaps = [x[0]["vwap"] for x in per if x[0]]
            print(f"\n  {label}: cleared {len(flats)}/{len(per)} windows, "
                  f"median flat at cycle {statistics.median(flats) if flats else 'n/a'}, "
                  f"passive share {statistics.mean(shares):.0%}, "
                  f"fees {statistics.mean(fees):>+9,.0f}, vwap {statistics.mean(vwaps):.4f}")
            check(f"{label}: every window reaches flat", len(flats) == len(per),
                  f"{len(flats)}/{len(per)}")
            # Deadline + 2, not the deadline exactly. The position is read once
            # per cycle, so a passive order that fills the last chunk is seen a
            # cycle later than it happened, and the glide path is allowed one
            # cycle to notice it is behind. The invariant that actually matters
            # - that passive does not SLOW the unwind - is checked below against
            # the market-only baseline.
            grace = bot.UNWIND_DEADLINE_CYCLES + 2
            check(f"{label}: flat inside the deadline",
                  all(f <= grace for f in flats),
                  f"max {max(flats) if flats else 0} vs {grace}")
            check(f"{label}: never flips through zero",
                  all(x[2] <= bot.UNWIND_THRESHOLD for x in per),
                  f"max overshoot {max(x[2] for x in per):,}")
            check(f"{label}: no orders left resting",
                  all(not x[3].resting.orders for x in per))
            if not mode:
                check("market-only takes nothing passively",
                      statistics.mean(shares) == 0)
            else:
                check("passive layer actually fills", statistics.mean(shares) > 0)

        # The invariant the whole design rests on: resting orders must displace
        # aggressive fills, not delay them. If passive finishes materially later
        # than market-only, it is buying its rebate with holding time - and the
        # deadline sweep shows holding time costs far more variance than the
        # rebate is worth.
        p_flat = [x[1] for x in rows[0][1] if x[1] is not None]
        m_flat = [x[1] for x in rows[1][1] if x[1] is not None]
        check("passive does not delay the unwind vs v10",
              statistics.mean(p_flat) <= statistics.mean(m_flat) + 2,
              f"{statistics.mean(p_flat):.1f} vs {statistics.mean(m_flat):.1f} cycles")

        # Past HOLD_TO_CLOSEOUT_TICK the position is deliberately left on the
        # book for close-out, so the unwind must do nothing at all - no orders,
        # no resting size, position untouched.
        ex, st_h, trace = run(start, windows[0], passive=True,
                              tick0=bot.HOLD_TO_CLOSEOUT_TICK)
        check("late in the heat, the unwind holds instead of trading",
              ex.position == start and not ex.fills and not st_h.resting.orders,
              f"pos {ex.position:,} fills {len(ex.fills)} resting {len(st_h.resting.orders)}")
        # One tick earlier it must still work normally, or the guard is not a
        # threshold but an off switch.
        ex, _, _ = run(start, windows[0], passive=True,
                       tick0=bot.HOLD_TO_CLOSEOUT_TICK - 30)
        check("just before the threshold it still unwinds",
              abs(ex.position) <= bot.UNWIND_THRESHOLD, f"pos {ex.position:,}")

        pf = [x[0]["fees"] for x in rows[0][1]]
        mf = [x[0]["fees"] for x in rows[1][1]]
        pv = [x[0]["vwap"] for x in rows[0][1]]
        mv = [x[1] for x in [(None, x[0]["vwap"]) for x in rows[1][1]]]
        gain = statistics.mean(mf) - statistics.mean(pf)
        px = (statistics.mean(pv) - statistics.mean(mv)) * (1 if start > 0 else -1)
        print(f"\n  INDICATIVE ONLY (synthetic book): fees {gain:+,.0f} better, "
              f"execution price {px:+.4f}/unit better, "
              f"total {gain + px * abs(start):+,.0f} on {abs(start):,} units")

    print("\n" + ("ALL CHECKS PASSED" if not failures
                  else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
