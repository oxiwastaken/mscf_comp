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
# Flat book: the loop above left tender_position at -90,000, and the same-way
# bar would otherwise make this a test of BAR_ADD_SAME_WAY by accident.
B.tender_position = 0
offer = {"action": "SELL", "quantity": 80_000, "price": 25.10}
ok, reason, edge = B.tender_safety(offer, {B.RITC: 0, B.BULL: 0, B.BEAR: 0}, book, 1.0, 1.0, tick=10)
check("an early tender is priced against the mid, no fee",
      edge is not None and abs(edge - (25.10 - 25.00)) < 1e-9, f"edge {edge}")
check("...and accepted when it clears MIN_TENDER_EDGE_HELD", ok, reason)
thin = dict(offer, price=25.05)
ok, reason, edge = B.tender_safety(thin, {B.RITC: 0, B.BULL: 0, B.BEAR: 0}, book, 1.0, 1.0, tick=10)
check("a tender below the held bar is refused", not ok and "closeout" in reason, reason)

# BAR_ADD_SAME_WAY: doubling down costs more, reducing exposure does not.
# Pinned rather than read from production: the competition runs v27, which sets
# it to 0.0, and these checks are for the MECHANISM - it has to still work the
# day someone turns it back on. Same reason sim_unwind.py pins the hold tick.
B.BAR_ADD_SAME_WAY = 0.04
# Edge is 0.10 in both cases - only the direction of existing inventory differs.
flat = {B.RITC: 0, B.BULL: 0, B.BEAR: 0}
sell = {"action": "SELL", "quantity": 80_000, "price": 25.10}   # signed -80,000
buy = {"action": "BUY", "quantity": 80_000, "price": 24.90}     # signed +80,000

B.tender_position = 0
ok_flat, _, _ = B.tender_safety(sell, flat, book, 1.0, 1.0, tick=10)
check("on a flat book the same-way bar does not apply", ok_flat)

B.tender_position = -90_000          # already short: SELL doubles down
ok_same, reason_same, _ = B.tender_safety(sell, flat, book, 1.0, 1.0, tick=10)
check("a tender that doubles down is refused at +0.10",
      not ok_same and "sameway" in reason_same, reason_same)

ok_opp, reason_opp, _ = B.tender_safety(buy, flat, book, 1.0, 1.0, tick=10)
check("the opposite direction is NOT penalised - it flattens us and pays",
      ok_opp and "sameway" not in reason_opp, reason_opp)

B.tender_position = +90_000          # already long: now BUY doubles down
ok_same2, reason_same2, _ = B.tender_safety(buy, flat, book, 1.0, 1.0, tick=10)
ok_opp2, _, _ = B.tender_safety(sell, flat, book, 1.0, 1.0, tick=10)
check("the rule is symmetric in direction",
      not ok_same2 and ok_opp2, f"long: buy {ok_same2}, sell {ok_opp2}")

B.tender_position = -90_000
big = {"action": "SELL", "quantity": 80_000, "price": 25.13}     # edge 0.13 > 0.12
ok_big, _, _ = B.tender_safety(big, flat, book, 1.0, 1.0, tick=10)
check("a big enough edge still gets through the raised bar", ok_big)
B.tender_position = 0

print(f"\n{failures} FAILED" if failures else "\nall passed")
raise SystemExit(1 if failures else 0)
