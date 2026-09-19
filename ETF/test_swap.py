"""
Tests for swap_for_tender against a fake exchange.

The swap sends real market orders to make room for a tender, so the things that
must hold are exactly the ones a live heat would only reveal by losing money:
the dump is big enough that the tender then fits, nothing trades when the edge
does not pay for the dump, and only tender inventory is ever dumped - never the
arb's legs, which is what v11 and v16 were both about.

    python test_swap.py
"""

import etf_arb_bot as B

failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    failures += 0 if ok else 1


class Logs:
    class Sink:
        def __init__(self):
            self.rows = []

        def write(self, row):
            self.rows.append(row)

    def __init__(self):
        self.trades = self.Sink()


def run(pos, tender_position, offer, edge, fill_limit=None):
    """Drive swap_for_tender with a fake order endpoint. Returns (dumped, orders)."""
    orders = []

    def fake_mkt(session, ticker, action, qty):
        q = qty if fill_limit is None else min(qty, max(fill_limit - sum(o[2] for o in orders), 0))
        orders.append((ticker, action, q))
        return {"quantity_filled": q, "vwap": 25.0, "order_id": len(orders)}

    B.place_mkt, saved = fake_mkt, B.place_mkt
    B.tender_position = tender_position
    try:
        dumped = B.swap_for_tender(None, offer, edge, pos, None, Logs(), 1, 250, {})
    finally:
        B.place_mkt = saved
    return dumped, orders


def fits(pos, offer):
    q = float(offer["quantity"])
    signed = q if offer["action"] == "BUY" else -q
    net = pos[B.BULL] + pos[B.BEAR] + 2 * (pos[B.RITC] + signed)
    gross = abs(pos[B.BULL]) + abs(pos[B.BEAR]) + 2 * abs(pos[B.RITC] + signed)
    return (abs(net) <= B.SERVER_NET_LIMIT - B.LIMIT_BUFFER
            and gross <= B.SERVER_GROSS_LIMIT - B.LIMIT_BUFFER)


SELL_88K = {"action": "SELL", "quantity": 88_000, "price": 25.0}

# A short tender is held; a second same-direction tender arrives with real edge.
pos = {B.BULL: 0, B.BEAR: 0, B.RITC: -90_000}
dumped, orders = run(pos, -90_000, SELL_88K, edge=0.16)
check("a paying swap dumps enough that the tender then fits",
      dumped > 0 and fits(pos, SELL_88K), f"dumped {dumped:,}, ritc now {pos[B.RITC]:,}")
check("...buying back, since the held inventory is short",
      all(o[1] == "BUY" and o[0] == B.RITC for o in orders), f"{orders[:2]}")
check("...in orders no larger than the exchange maximum",
      all(o[2] <= B.MAX_SIZE_EQUITY for o in orders), f"{len(orders)} orders")
check("...and the tracker falls by exactly what was dumped",
      B.tender_position == -90_000 + dumped, f"tracker {B.tender_position:,}")
check("...without dumping more than it needed",
      dumped <= 90_000 - 1, f"dumped {dumped:,}")

# Same situation, but the edge does not pay for the dump at SWAP_DUMP_COST.
pos = {B.BULL: 0, B.BEAR: 0, B.RITC: -90_000}
dumped, orders = run(pos, -90_000, SELL_88K, edge=0.10)
check("no trade when the edge does not pay for the dump",
      dumped == 0 and not orders and pos[B.RITC] == -90_000, f"orders {orders}")

# An opposite-direction tender reduces exposure on its own; nothing to dump.
pos = {B.BULL: 0, B.BEAR: 0, B.RITC: -90_000}
dumped, orders = run(pos, -90_000, {"action": "BUY", "quantity": 88_000, "price": 25.0}, 0.30)
check("no dump for an opposite-direction tender", dumped == 0 and not orders)

# Most of the RITC belongs to the arb, not to tenders: it must not be dumped.
pos = {B.BULL: 45_000, B.BEAR: 45_000, B.RITC: -90_000}
dumped, orders = run(pos, -20_000, SELL_88K, edge=0.40)
check("never dumps beyond tender inventory into the arb's legs",
      dumped == 0 and not orders and B.tender_position == -20_000, f"orders {orders}")

# The exchange fills only part of the dump: bookkeeping follows the fills.
pos = {B.BULL: 0, B.BEAR: 0, B.RITC: -90_000}
dumped, orders = run(pos, -90_000, SELL_88K, edge=0.16, fill_limit=25_000)
check("a partial fill is booked as filled, not as requested",
      dumped == 25_000 and pos[B.RITC] == -65_000 and B.tender_position == -65_000,
      f"dumped {dumped:,}, ritc {pos[B.RITC]:,}, tracker {B.tender_position:,}")
check("...and the tender still does not fit, so it will not be accepted",
      not fits(pos, SELL_88K))

print(f"\n{failures} FAILED" if failures else "\nall passed")
raise SystemExit(1 if failures else 0)
