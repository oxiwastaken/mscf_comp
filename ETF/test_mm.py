"""
Tests for market_make against a fake exchange.

Market making posts and cancels real limit orders every cycle, so the failure
modes are the expensive kind: quotes left resting when a tender arrives,
inventory past its cap, duplicate quotes stacking on the book, or a fill lost
between a cancel and the next poll. Each of those is driven here.

    python test_mm.py
"""

import etf_arb_bot as B

failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    failures += 0 if ok else 1


class Exchange:
    """Just enough of RIT for Resting and market_make."""

    def __init__(self):
        self.orders, self.next_id = {}, 1
        self.market = []

    def place_lmt(self, session, ticker, action, qty, price):
        oid = self.next_id; self.next_id += 1
        self.orders[oid] = {"order_id": oid, "ticker": ticker, "action": action,
                            "price": price, "quantity": qty, "quantity_filled": 0,
                            "status": "OPEN", "vwap": None}
        return dict(self.orders[oid])

    def place_mkt(self, session, ticker, action, qty):
        self.market.append((action, qty))
        return {"quantity_filled": qty, "vwap": 25.00, "order_id": 999}

    def open_orders(self, session):
        return [dict(o) for o in self.orders.values() if o["status"] == "OPEN"]

    def get_order(self, session, oid):
        return dict(self.orders[oid])

    def cancel_orders(self, session, ids):
        for oid in ids:
            if self.orders[oid]["status"] == "OPEN":
                self.orders[oid]["status"] = "CANCELLED"

    def fill(self, oid, qty):
        o = self.orders[oid]
        o["quantity_filled"] += qty
        o["vwap"] = o["price"]
        if o["quantity_filled"] >= o["quantity"]:
            o["status"] = "TRANSACTED"

    def live(self, action=None):
        return [o for o in self.orders.values() if o["status"] == "OPEN"
                and (action is None or o["action"] == action)]


class Logs:
    class Sink:
        def __init__(self): self.rows = []
        def write(self, row): self.rows.append(row)
    def __init__(self): self.trades = self.Sink()


def book(bid, ask):
    return {"bids": [{"price": bid, "quantity": 10_000}],
            "asks": [{"price": ask, "quantity": 10_000}]}


SNAP = {"usd_bid": 1.02, "usd_ask": 1.02}
ex = Exchange()
for name in ("place_lmt", "place_mkt", "open_orders", "get_order", "cancel_orders"):
    setattr(B, name, getattr(ex, name))
B.tender_position = 0
mm, logs = B.MMState(), Logs()


def cycle(bid, ask, fair, tick=50, tenders_live=False):
    B.market_make(None, book(bid, ask), SNAP, logs, 1, tick, mm, tenders_live, fair)


# Fair value in line with the market: quote both sides one tick inside.
cycle(25.00, 25.08, fair=25.04)
bids, asks = ex.live("BUY"), ex.live("SELL")
check("quotes both sides one tick inside the touch",
      len(bids) == 1 and len(asks) == 1 and bids[0]["price"] == 25.01
      and asks[0]["price"] == 25.07, f"{[(o['action'], o['price']) for o in ex.live()]}")

cycle(25.00, 25.08, fair=25.04)
check("an unchanged book does not stack duplicate quotes", len(ex.live()) == 2)

# The touch moves: stale quotes are cancelled and replaced at the new level.
cycle(25.10, 25.18, fair=25.14)
check("a moved book cancels stale quotes and re-posts",
      sorted(o["price"] for o in ex.live()) == [25.11, 25.17],
      f"{sorted(o['price'] for o in ex.live())}")

# A bid fills; the next cycle books it.
buy = ex.live("BUY")[0]
ex.fill(buy["order_id"], 3_000)
cycle(25.10, 25.18, fair=25.14)
check("a fill is booked into inventory and logged",
      mm.inv == 3_000 and mm.fills == 1 and logs.trades.rows[-1]["direction"] == "mm",
      f"inv {mm.inv:,}")

# A fill that lands between the cancel and the next poll must not be lost.
sell = ex.live("SELL")[0]
cycle(25.20, 25.28, fair=25.24)                  # cancels the 25.17 offer
ex.orders[sell["order_id"]]["status"] = "OPEN"   # ...which filled as it was cancelled
ex.fill(sell["order_id"], 3_000)
cycle(25.20, 25.28, fair=25.24)
check("a fill racing a cancel is still booked", mm.inv == 0, f"inv {mm.inv:,}")

# RITC rich to fair value: only offer, never bid for what is overpriced.
cycle(25.20, 25.28, fair=25.10)
check("rich to fair value: offer only",
      ex.live("SELL") and not ex.live("BUY"), f"{[(o['action'], o['price']) for o in ex.live()]}")

# Inventory at the cap: no more buying. Book it as a real purchase, so the
# cash matches the inventory and the P&L stays meaningful.
mm.inv, mm.cash_cad = B.MM_MAX_INV, mm.cash_cad - B.MM_MAX_INV * 25.24 * 1.02
cycle(25.20, 25.28, fair=25.24)
check("at the inventory cap, no bid", not ex.live("BUY") and ex.live("SELL"))

# A tender appears and fits: quotes pulled, inventory kept - no dump for nothing.
ex.market.clear()
cycle(25.20, 25.28, fair=25.24, tenders_live=True)
check("a live tender pulls every quote", not ex.live())
check("...but keeps inventory when nothing is blocked",
      mm.inv == B.MM_MAX_INV and not ex.market, f"inv {mm.inv:,}, market {ex.market}")

# The tender is refused on limits: now the inventory is in the way, so it goes.
B.market_make(None, book(25.20, 25.28), SNAP, logs, 1, 50, mm, True, 25.24, True)
check("a limit-blocked tender flattens inventory at market",
      mm.inv == 0 and ex.market == [("SELL", B.MM_MAX_INV)], f"{ex.market}")

# Tender inventory held: stay out entirely, and never carry inventory into it.
mm.inv, mm.cash_cad = 3_000.0, mm.cash_cad - 3_000 * 25.24 * 1.02
ex.market.clear()
B.tender_position = -80_000
cycle(25.20, 25.28, fair=25.24)
check("no quoting while tender inventory is held", not ex.live())
check("...and any inventory is flattened so the tracker never nets it",
      mm.inv == 0 and ex.market == [("SELL", 3_000)], f"{ex.market}")
B.tender_position = 0

# The kill switch: flat book, P&L just past the limit.
mm.inv, mm.rebate_cad, mm.cash_cad = 0.0, 0.0, -B.MM_MAX_LOSS - 1
cycle(25.20, 25.28, fair=25.24)
check("past the loss limit, market making switches off",
      mm.disabled and not ex.live())

print(f"\n{failures} FAILED" if failures else "\nall passed")
raise SystemExit(1 if failures else 0)
