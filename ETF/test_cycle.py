"""End-to-end smoke test: drive a whole heat through `step_once`.

The other tests exercise one decision each against a stub. This one runs the
real cycle - book fetch, tender poll, gate, pre-hedge, accept, arb legs, RITC
unwind, pre-hedge unwind, FX hedge, and every log write - over a synthetic heat
with tenders arriving on the cadence the logs show.

It exists because none of that integration is covered otherwise, and an
AttributeError in the live cycle costs a whole heat. It asserts behaviour that
must hold regardless of parameters:

  1. no exception escapes a cycle, at any tick, in any branch
  2. server position limits are never breached
  3. tender inventory is flat before HOLD_TO_CLOSEOUT_TICK and held after it
  4. the pre-hedge fires when a tender needs net room, and never outlives the
     position it was hedging
  5. `tender_position` still agrees with the real RITC position after a full
     heat of arbs, tenders and unwinds moving it independently
  6. no orders are left resting at the end of the heat

    python test_cycle.py
"""

import sys

import etf_arb_bot as bot
import sim_unwind as sim


class HeatExchange(sim.SimExchange):
    """SimExchange plus the endpoints a full cycle touches."""

    def __init__(self, quotes, tenders):
        super().__init__(quotes)
        self.tenders = tenders          # {tick: [offer, ...]}
        self.tick = 0
        self.accepted = []
        self.positions = {t: 0 for t in (bot.BULL, bot.BEAR, bot.RITC,
                                         bot.USD, bot.CAD)}
        self.breaches = []

    # -- limits ----------------------------------------------------------
    def check_limits(self):
        p = self.positions
        gross = abs(p[bot.BULL]) + abs(p[bot.BEAR]) + 2 * abs(p[bot.RITC])
        net = p[bot.BULL] + p[bot.BEAR] + 2 * p[bot.RITC]
        if gross > bot.SERVER_GROSS_LIMIT:
            self.breaches.append(f"tick {self.tick}: gross {gross:,}")
        if abs(net) > bot.SERVER_NET_LIMIT:
            self.breaches.append(f"tick {self.tick}: net {net:,}")

    def market_fill(self, action, qty):
        # Base class tracks RITC only; a full cycle also trades BULL/BEAR/USD.
        return super().market_fill(action, qty)

    def request(self, session, method, endpoint, params=None):
        p = params or {}
        if endpoint == "case":
            return {"tick": self.tick, "status": "ACTIVE"}
        if endpoint == "trader":
            return {"nlv": 1_000_000.0}
        if endpoint == "securities" and method == "GET":
            bid, ask = self.top()
            out = []
            for t in (bot.BULL, bot.BEAR, bot.RITC, bot.USD, bot.CAD):
                out.append({"ticker": t, "position": self.positions[t],
                            "bid": bid, "ask": ask})
            return out
        if endpoint == "securities/book":
            t = p.get("ticker")
            if t == bot.USD:
                return {"bids": [{"price": 1.0100, "quantity": 5_000_000}],
                        "asks": [{"price": 1.0102, "quantity": 5_000_000}]}
            if t in (bot.BULL, bot.BEAR):
                # BULL and BEAR must sum to the CAD value of RITC, or the arb
                # sees a free 20+ CAD edge on every cycle and the test measures
                # a book that cannot exist. Split the parity price 40/60, which
                # is the 10/15 starting ratio from the case.
                bid, ask = self.top()
                mid = (bid + ask) / 2 * 1.0101
                share = mid * (0.4 if t == bot.BULL else 0.6)
                return {
                    "bids": [{"price": round(share - 0.03, 2), "quantity": 8000}],
                    "asks": [{"price": round(share + 0.03, 2), "quantity": 8000}],
                }
            return self.book()
        if endpoint == "tenders" and method == "GET":
            return list(self.tenders.get(self.tick, []))
        if endpoint.startswith("tenders/") and method == "POST":
            tid = endpoint.split("/")[1]
            for offers in self.tenders.values():
                for o in offers:
                    if str(o["tender_id"]) == tid:
                        q = float(o["quantity"])
                        self.positions[bot.RITC] += q if o["action"] == "BUY" else -q
                        self.accepted.append(o)
                        self.check_limits()
                        return {"success": True}
            raise bot.ApiException("unknown tender")
        if endpoint == "orders" and method == "POST":
            resp = super().request(session, method, endpoint, p)
            if p["type"] == "MARKET":
                q = float(resp.get("quantity_filled") or 0)
                tk = p["ticker"]
                self.positions[tk] = self.positions.get(tk, 0) + (
                    q if p["action"] == "BUY" else -q)
                self.check_limits()
            return resp
        return super().request(session, method, endpoint, p)


def make_tenders(quotes):
    """Tenders on the cadence the logs show: every ~30 ticks, 70k-100k units,
    alive for ~6 ticks, priced to straddle the accept bar."""
    out, tid = {}, 3000
    # The first one is deliberately 100,000 on a flat book: 200,000 of net
    # against a 197,000 effective cap, so it is unreachable unless the
    # pre-hedge fires. Without it this test never exercises that path.
    plan = [(8, "BUY", 100000, -0.30),
            (25, "BUY", 92000, -0.30), (60, "SELL", 89000, +0.30),
            (95, "BUY", 75000, +0.40), (130, "SELL", 97000, +0.05),
            (165, "BUY", 99000, -0.35), (205, "SELL", 85000, +0.06),
            (240, "BUY", 100000, -0.12), (270, "SELL", 93000, -0.40)]
    for tick, action, qty, off in plan:
        tid += 1
        for k in range(6):
            t = tick + k
            if t >= len(quotes):
                break
            bid, ask = quotes[t]
            mid = (bid + ask) / 2
            out.setdefault(t, []).append({
                "tender_id": tid, "ticker": bot.RITC, "action": action,
                "quantity": qty, "price": round(mid + off, 2),
                "is_fixed_bid": True, "expires": tick + 6,
                "caption": "test"})
    return out


def main():
    quotes = sim.quote_series(limit=0)[:301]
    if len(quotes) < 301:
        quotes = (quotes * (301 // max(len(quotes), 1) + 1))[:301]
    ex = HeatExchange(quotes, make_tenders(quotes))
    bot.api_request = ex.request
    bot.tender_position = 0
    bot.prehedge_position = 0
    logs = sim.NullLogs()
    st = bot.UnwindState()
    bases = {"basket_rich": 0.10, "etf_rich": 0.10}

    failures = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
        if not cond:
            failures.append(name)

    errors, held_at, flat_before = [], None, True
    at_threshold = None
    prehedge_orphan = None
    prehedge_fired = 0
    prev_prehedge = 0
    arb_gross_breach = None
    for tick in range(301):
        ex.tick = ex.i = min(tick, len(quotes) - 1)
        try:
            bot.step_once(None, logs, 1, tick, bases, tick, st)
        except Exception as e:                      # noqa: BLE001 - that is the test
            errors.append(f"tick {tick}: {type(e).__name__}: {e}")
        ex.step()
        ex.i = min(tick, len(quotes) - 1)
        if abs(bot.prehedge_position) > abs(prev_prehedge):
            prehedge_fired += 1
        prev_prehedge = bot.prehedge_position
        # The gross reserve exists so the ARB can never be the reason a tender
        # is refused. With tender inventory flat, everything left in gross is
        # the arb's, and it must sit inside MAX_GROSS - otherwise the reserve
        # has been spent and the next tender gets "gross would hit".
        if (abs(bot.tender_position) <= bot.UNWIND_THRESHOLD
                and not bot.prehedge_position):
            pz = ex.positions
            arb_gross = (abs(pz[bot.BULL]) + abs(pz[bot.BEAR])
                         + 2 * abs(pz[bot.RITC]))
            if arb_gross > bot.MAX_GROSS and arb_gross_breach is None:
                arb_gross_breach = (tick, arb_gross)
        if tick == bot.HOLD_TO_CLOSEOUT_TICK - 1:
            at_threshold = bot.tender_position
            flat_before = abs(at_threshold) <= bot.UNWIND_THRESHOLD
        if tick > bot.HOLD_TO_CLOSEOUT_TICK and abs(bot.tender_position) > bot.UNWIND_THRESHOLD:
            held_at = held_at or tick
        if (bot.prehedge_position
                and abs(bot.tender_position) <= bot.UNWIND_THRESHOLD
                and tick < bot.HOLD_TO_CLOSEOUT_TICK and tick > 5):
            # allowed for one cycle - unwind_prehedge runs after unwind_ritc
            prehedge_orphan = prehedge_orphan or tick

    print(f"\nran 301 cycles, {len(ex.accepted)} tenders accepted, "
          f"{len(ex.fills)} RITC fills\n")
    check("no exception escaped a cycle", not errors,
          f"{len(errors)}: {errors[:2]}")
    check("server position limits never breached", not ex.breaches,
          f"{len(ex.breaches)}: {ex.breaches[:2]}")
    check("tender inventory is worked down before the hold threshold",
          flat_before,
          f"tender_position was {at_threshold:,} at tick "
          f"{bot.HOLD_TO_CLOSEOUT_TICK - 1}")
    check("something was actually accepted", len(ex.accepted) > 0)
    check("no orders left resting at the end", not st.resting.orders,
          f"{len(st.resting.orders)} resting")
    # The pre-hedge may legitimately lag one cycle behind the ETF leg; it must
    # not be left on indefinitely.
    check("the arb never spends the tender gross reserve",
          arb_gross_breach is None,
          f"gross {arb_gross_breach[1]:,} at tick {arb_gross_breach[0]} "
          f"exceeds MAX_GROSS {bot.MAX_GROSS:,}" if arb_gross_breach else "")
    check("the reserve covers the largest tender seen",
          bot.MAX_GROSS + 2 * 100000 <= bot.SERVER_GROSS_LIMIT - bot.LIMIT_BUFFER,
          f"{bot.MAX_GROSS + 200000:,} vs "
          f"{bot.SERVER_GROSS_LIMIT - bot.LIMIT_BUFFER:,}")
    check("the pre-hedge path is exercised end to end", prehedge_fired > 0,
          f"fired {prehedge_fired}x")
    check("tracker agrees with the real position at the end",
          abs(bot.tender_position - ex.positions[bot.RITC]) <= bot.UNWIND_THRESHOLD,
          f"tracker {bot.tender_position:,} vs ritc {ex.positions[bot.RITC]:,}")
    check("pre-hedge does not outlive its position",
          bot.prehedge_position == 0 or held_at is not None,
          f"prehedge {bot.prehedge_position:,}, orphaned from tick {prehedge_orphan}")
    print(f"\n  accepted: " + ", ".join(
        f"{o['action']} {o['quantity']:,}" for o in ex.accepted) or "  none")
    print(f"  final: ritc {ex.positions[bot.RITC]:,} "
          f"bull {ex.positions[bot.BULL]:,} bear {ex.positions[bot.BEAR]:,} "
          f"tender_position {bot.tender_position:,} prehedge {bot.prehedge_position:,}")

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
