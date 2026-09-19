"""
v27 holds every tender to close-out. The one thing that must hold in
production: with HOLD_TO_CLOSEOUT_TICK = 0 the unwind never sends an order, at
any tick, however large the tender inventory - and every tender is priced on
the close-out basis (against the mid, no fee) with MIN_TENDER_EDGE_HELD.

    python test_holdall.py
"""

import etf_arb_bot as B

failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    failures += 0 if ok else 1


class Logs:
    class Sink:
        def __init__(self): self.rows = []
        def write(self, row): self.rows.append(row)
    def __init__(self): self.trades = self.Sink()


orders = []
B.place_mkt = lambda s, t, a, q: orders.append(("MKT", t, a, q)) or {"quantity_filled": q, "vwap": 25}
B.place_lmt = lambda s, t, a, q, p: orders.append(("LMT", t, a, q)) or {"order_id": len(orders)}
B.cancel_orders = lambda s, ids: None
B.open_orders = lambda s: []

check("production holds from tick 0", B.HOLD_TO_CLOSEOUT_TICK == 0, f"{B.HOLD_TO_CLOSEOUT_TICK}")

book = {"bids": [{"price": 24.98, "quantity": 20_000}], "asks": [{"price": 25.02, "quantity": 20_000}]}
st = B.UnwindState()
for tick in (1, 10, 60, 150, 199, 250, 298):
    B.tender_position = -90_000
    B.unwind_ritc(None, {B.RITC: -90_000, B.BULL: 0, B.BEAR: 0}, book, {}, Logs(), 1, tick, tick, st)
check("the unwind never sends an order, at any tick", not orders, f"{orders[:3]}")
check("...and the tender inventory is left intact", B.tender_position == -90_000)

# Pricing: an early tender is scored against the mid with no fee, on the held bar.
offer = {"action": "SELL", "quantity": 80_000, "price": 25.10}
ok, reason, edge = B.tender_safety(offer, {B.RITC: 0, B.BULL: 0, B.BEAR: 0}, book, 1.0, 1.0, tick=10)
check("an early tender is priced against the mid, no fee",
      edge is not None and abs(edge - (25.10 - 25.00)) < 1e-9, f"edge {edge}")
check("...and accepted when it clears MIN_TENDER_EDGE_HELD", ok, reason)
thin = dict(offer, price=25.05)
ok, reason, edge = B.tender_safety(thin, {B.RITC: 0, B.BULL: 0, B.BEAR: 0}, book, 1.0, 1.0, tick=10)
check("a tender below the held bar is refused", not ok and "closeout" in reason, reason)

print(f"\n{failures} FAILED" if failures else "\nall passed")
raise SystemExit(1 if failures else 0)
