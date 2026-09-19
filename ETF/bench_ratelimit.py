"""Does lifting the request ceiling actually buy anything?

Two halves, because the question has two halves.

PART 1 - MECHANISM (measured here, exactly).
  Drives the real `step_once` against test_cycle's synthetic exchange with a
  latency injected into every API call, and measures what a cycle costs and how
  stale the book is when the arb legs are sent. Config A is the shipped
  behaviour before this change (serial fetch, 0.5s sleep, arb fired last);
  config B is after (concurrent snapshot, governor pacing, arb fired first).
  Nothing here is modelled - it is the real cycle on a stopwatch.

PART 2 - VALUE (estimated from logs/, with its assumptions stated).
  A faster cycle is only worth something if the bot was losing money by being
  slow. Reads logs/ticks.csv and logs/tenders.csv and reports the three places
  the speed shows up, with the measurement behind each.

Latency default is 0.058s/request, which is what the logged runs imply:
mean cycle gap 0.965s, of which LOOP_SLEEP was 0.500s, over ~8 requests.

    python bench_ratelimit.py
    python bench_ratelimit.py --latency 0.03 --ticks 60
"""

import argparse
import csv
import statistics
import sys
import threading
import time
from collections import defaultdict

import etf_arb_bot as bot
import test_cycle as tc

MEASURED_LATENCY = 0.058
SANE = 5.0


# ---------------------------------------------------------------------------
# PART 1: the cycle on a stopwatch
# ---------------------------------------------------------------------------

# How fast a dislocation closes, in CAD per unit per second.
#
# Calibrated on logs/ticks.csv, not assumed: over 2,094 clearing observations
# the mean surplus over the bar was +0.0775/unit when read and -0.0329/unit one
# observation (~0.96s) later. That is 0.1104 of decay in 0.96s.
#
# Modelled as linear over the sub-second window, which is the honest choice:
# 1 Hz logs cannot resolve the shape inside one second, so a straight line
# between the two points we DO have is the only claim the data supports. If the
# real decay is front-loaded - and for a dislocation being competed away it
# probably is - this understates what removing latency is worth.
DECAY_PER_SEC = 0.1104 / 0.96

# Size of a dislocation at the moment it appears, CAD per unit of basket.
#
# The bar is required_edge = 2*FEE_MKT + FEE_MKT*fx + base = 0.1302 at base
# 0.07. A dislocation must clear that for long enough that the SLOW config also
# trades - otherwise the benchmark reports "8 arbs vs 0" and measures whether a
# knife-edge was crossed rather than how much edge survives the wait. At 0.30 it
# stays above the bar for (0.30-0.1302)/0.115 = 1.5s, so both configs trade and
# the difference lands where it belongs: in the edge left when the legs arrive.
DISLOCATION = 0.30
# Ticks between dislocations.
DISLOCATION_EVERY = 3


class TimedExchange(tc.HeatExchange):
    """test_cycle's exchange, plus latency, a call trace, and a decaying arb.

    test_cycle pins BULL + BEAR to RITC parity on purpose, so no arb can ever
    fire there and it never has to model one. That makes it useless for this
    question, so this adds a dislocation that opens on a schedule and closes
    in WALL-CLOCK time rather than in ticks.

    The wall-clock part is the whole point. A bot that reaches the book sooner
    captures more of the dislocation, which is precisely the effect being
    measured; if the edge decayed per tick instead, every configuration would
    capture the same thing and the benchmark would be measuring nothing.
    """

    def __init__(self, quotes, tenders, latency):
        super().__init__(quotes, tenders)
        self.latency = latency
        self.trace = []
        self.tlock = threading.Lock()
        self.tick_started = time.monotonic()
        self._tick = 0

    @property
    def tick(self):
        return self._tick

    @tick.setter
    def tick(self, v):
        if v != getattr(self, "_tick", None):
            self.tick_started = time.monotonic()
        self._tick = v

    def dislocation(self):
        """Edge available right now, in CAD per unit of basket."""
        if self._tick % DISLOCATION_EVERY != 0:
            return 0.0
        age = time.monotonic() - self.tick_started
        return max(0.0, DISLOCATION - DECAY_PER_SEC * age)

    def request(self, session, method, endpoint, params=None):
        time.sleep(self.latency)
        with self.tlock:
            self.trace.append((time.monotonic(), method, endpoint))
        p = params or {}
        if endpoint == "securities/book" and p.get("ticker") in (bot.BULL, bot.BEAR):
            # Parity, as test_cycle builds it, plus half the live dislocation on
            # each of the two legs so the basket carries all of it.
            bid, ask = self.top()
            mid = (bid + ask) / 2 * 1.0101
            share = mid * (0.4 if p["ticker"] == bot.BULL else 0.6)
            lift = self.dislocation() / 2.0
            return {
                "bids": [{"price": round(share - 0.03 + lift, 4), "quantity": 8000}],
                "asks": [{"price": round(share + 0.03 + lift, 4), "quantity": 8000}],
            }
        return super().request(session, method, endpoint, p)


def instrument(record):
    """Time the snapshot and the arb, rather than inferring them from endpoints.

    Inferring was wrong: on most cycles the arb is refused by the net gate, so
    the first order POST in the cycle belongs to the UNWIND, which legitimately
    runs later. That made the arb look stale on cycles where no arb was sent.
    Wrapping the two functions measures the thing the change is supposed to
    move - snapshot complete -> first arb leg away - and nothing else.
    """
    real_parallel = bot.parallel
    real_arb = bot.execute_arb

    def timed_parallel(calls):
        out = real_parallel(calls)
        record["snapshot"] = time.monotonic()
        return out

    def timed_arb(session, logs, heat, tick, direction, edge, legs, snap, *a, **kw):
        if record.get("snapshot") is not None:
            record.setdefault("gaps", []).append(
                time.monotonic() - record["snapshot"])
        # `edge` is what the gate saw. What matters is what is still there when
        # the legs actually land, so the exchange is asked again at send time.
        ex = record.get("exchange")
        if ex is not None:
            record.setdefault("captured", []).append(ex.dislocation())
            record.setdefault("seen", []).append(edge)
        return real_arb(session, logs, heat, tick, direction, edge, legs,
                        snap, *a, **kw)

    bot.parallel = timed_parallel
    bot.execute_arb = timed_arb
    return lambda: (setattr(bot, "parallel", real_parallel),
                    setattr(bot, "execute_arb", real_arb))


def drive(label, ticks, latency, parallel, loop_sleep, max_cpt, arb_first):
    """Run `ticks` ticks of the real cycle under one configuration."""
    quotes, tenders = tc.build_heat() if hasattr(tc, "build_heat") else (None, None)
    if quotes is None:
        quotes, tenders = _synthetic_heat(ticks)

    ex = TimedExchange(quotes, tenders, latency)
    bot.api_request = ex.request
    bot.PARALLEL_FETCH = parallel
    bot.ARB_FIRST = arb_first
    bot.LOOP_SLEEP = loop_sleep
    bot.MAX_CYCLES_PER_TICK = max_cpt
    # The baseline had no governor at all, so give it one that never binds.
    # The new config gets the ceiling it will actually ship with.
    bot.GOVERNOR = bot.RateGovernor(rate=bot.REQ_PER_SEC if parallel else None)
    bot.tender_position = 0
    bot.prehedge_position = 0
    record = {"snapshot": None, "exchange": ex}
    restore = instrument(record)

    import sim_unwind as sim
    logs = sim.NullLogs()
    st = bot.UnwindState()
    bases = {"basket_rich": bot.ARB_THRESHOLD_CAD, "etf_rich": bot.ARB_THRESHOLD_CAD}

    started = time.monotonic()
    cycles = 0
    for tick in range(ticks):
        ex.tick = tick
        for _ in range(max_cpt):
            ex.request(None, "GET", "case")       # the cycle's own tick poll
            try:
                bot.step_once(None, logs, 1, tick, bases, cycles, st, None)
            except Exception as e:                # noqa: BLE001
                restore()
                print(f"  {label}: cycle raised {type(e).__name__}: {e}")
                return None
            cycles += 1
            time.sleep(loop_sleep)
            if not parallel:
                break                              # baseline was 1 cycle/tick
    elapsed = time.monotonic() - started
    restore()

    gaps = record.get("gaps", [])
    return {
        "label": label,
        "elapsed": elapsed,
        "cycles": cycles,
        "ticks": ticks,
        "requests": len(ex.trace),
        "cycle_s": elapsed / cycles if cycles else float("nan"),
        "req_per_cycle": len(ex.trace) / cycles if cycles else float("nan"),
        "req_per_s": len(ex.trace) / elapsed if elapsed else float("nan"),
        "staleness": statistics.fmean(gaps) if gaps else float("nan"),
        "n_arbs": len(gaps),
        "captured": statistics.fmean(record["captured"]) if record.get("captured") else float("nan"),
        "total_captured": sum(record.get("captured", [])),
        "n_orders": sum(1 for _, m, e in ex.trace if m == "POST" and e == "orders"),
    }


def _synthetic_heat(ticks):
    """Quote path for part 1. Deliberately WITHOUT tenders.

    The first version of this fed in 90,000-unit tenders on the logged cadence
    and no arb fired in either config - accepting one puts 180,000 of net on
    the book against a +/-25,000 cap, so the arb gate was shut for the rest of
    the run. That is a faithful reproduction of finding (b) in part 2, and it
    is exactly why part 1 must not include it: mixing the two means measuring
    the net cap instead of the latency.

    Part 1 isolates the arb path. Part 2 measures what the net cap costs.
    """
    quotes = []
    price = 26.40
    for t in range(ticks + 5):
        price += (0.01 if t % 7 < 4 else -0.012)
        quotes.append((round(price - 0.01, 2), round(price + 0.01, 2)))
    return quotes, {}


def part1(ticks, latency):
    print("=" * 74)
    print(" PART 1 - the cycle on a stopwatch")
    print(f"   real step_once, synthetic exchange, {latency*1000:.0f}ms per API call")
    print("=" * 74)

    configs = [
        ("before  (serial, 0.5s sleep, arb last)", False, 0.5, 1, False),
        ("reorder (serial, 0.5s sleep, arb first)", False, 0.5, 1, True),
        ("after   (concurrent, governor, arb first)", True, 0.02, 8, True),
    ]
    out = []
    for label, par, slp, cpt, first in configs:
        r = drive(label, ticks, latency, par, slp, cpt, first)
        if r:
            out.append(r)

    if len(out) < 3:
        print("  benchmark did not complete")
        return out

    print(f"\n{'config':<44} {'cycle':>8} {'c/tick':>7} {'req/s':>7} {'stale':>8} {'arbs':>5}")
    for r in out:
        print(f"{r['label']:<44} {r['cycle_s']*1000:>7.0f}ms "
              f"{r['cycles']/r['ticks']:>7.2f} {r['req_per_s']:>7.1f} "
              f"{r['staleness']*1000:>7.0f}ms {r['n_arbs']:>5}")
    if min(r["n_arbs"] for r in out) == 0:
        print("\n  WARNING: no arb fired in one config - the staleness column is")
        print("  empty there and the comparison below means nothing. Widen the")
        print("  synthetic quote path until both configs trade.")

    base, reorder, full = out
    q = bot.ORDER_QTY

    def delta(x, y):
        return (y["captured"] - x["captured"]) * q

    print(f"\n  The two halves of the change, separated:")
    print(f"    reordering alone (NO extra requests, ships today):")
    print(f"      edge at the legs {base['captured']:.4f} -> {reorder['captured']:.4f} "
          f"/unit = {delta(base, reorder):+,.0f} CAD per arb")
    print(f"      request rate     {base['req_per_s']:.1f}/s -> {reorder['req_per_s']:.1f}/s")
    print(f"    plus the lifted ceiling:")
    print(f"      edge at the legs {reorder['captured']:.4f} -> {full['captured']:.4f} "
          f"/unit = {delta(reorder, full):+,.0f} CAD per arb")
    print(f"      cycles per tick  {reorder['cycles']/reorder['ticks']:.2f} -> "
          f"{full['cycles']/full['ticks']:.2f}")
    print(f"      request rate     {reorder['req_per_s']:.1f}/s -> {full['req_per_s']:.1f}/s")
    print(f"\n  total {delta(base, full):+,.0f} CAD per arb, of which "
          f"{delta(base, reorder)/delta(base, full)*100:.0f}% needs no extra requests."
          if delta(base, full) else "")
    return out


# ---------------------------------------------------------------------------
# PART 2: what the logs say it is worth
# ---------------------------------------------------------------------------

def fnum(row, key):
    try:
        return float(row.get(key) or "")
    except (ValueError, TypeError):
        return None


def truthy(v):
    return (v or "").strip() in ("True", "true", "1")


def part2(staleness_removed):
    print("\n" + "=" * 74)
    print(" PART 2 - what the logged heats say that is worth")
    print("=" * 74)

    try:
        ticks = [r for r in csv.DictReader(open("logs/ticks.csv"))]
    except OSError:
        print("  logs/ticks.csv not found - run a heat first")
        return
    ticks = [r for r in ticks
             if fnum(r, "edge1") is not None and abs(fnum(r, "edge1")) <= SANE
             and fnum(r, "edge2") is not None and abs(fnum(r, "edge2")) <= SANE]

    # (a) staleness: how fast does a clearing edge rot?
    pairs = []
    by_run = defaultdict(list)
    for r in ticks:
        by_run[r["run_id"]].append(r)
    for rr in by_run.values():
        for i in range(len(rr) - 1):
            for ec, bc in (("edge1", "req_basket_rich"), ("edge2", "req_etf_rich")):
                e, b = fnum(rr[i], ec), fnum(rr[i], bc)
                e2, b2 = fnum(rr[i + 1], ec), fnum(rr[i + 1], bc)
                if None not in (e, b, e2, b2) and e >= b:
                    pairs.append((e - b, e2 - b2, e2 >= b2))
    if pairs:
        now = statistics.fmean(p[0] for p in pairs)
        later = statistics.fmean(p[1] for p in pairs)
        still = sum(1 for p in pairs if p[2]) / len(pairs)
        print(f"\n (a) ARB STALENESS   {len(pairs)} clearing observations")
        print(f"     surplus over the bar when read : {now:+.4f} /unit")
        print(f"     the same edge ~0.96s later     : {later:+.4f} /unit")
        print(f"     still clearing the bar then    : {still:.1%}")
        # Linear interpolation over the decay we can actually see. The true
        # path inside one second is unobserved at 1Hz, so this is a floor if
        # the decay is convex and a ceiling if concave - it is not a claim
        # about sub-second structure.
        frac = min(1.0, staleness_removed / 0.96) if staleness_removed else 0.0
        recovered = (now - later) * frac
        arbs = sum(1 for r in ticks if truthy(r.get("traded")))
        print(f"     removing {staleness_removed*1000:.0f}ms of that decay recovers "
              f"~{recovered:.4f} /unit")
        print(f"     over {arbs} logged arbs x {bot.ORDER_QTY:,} units "
              f"= {recovered * bot.ORDER_QTY * arbs:,.0f} CAD across all runs")
        print(f"     ASSUMPTION: decay is linear across the 0.96s we can see.")

    # (b) is the arb observation-bound, or limit-bound?
    clear = blocked = traded = 0
    for r in ticks:
        e1, b1 = fnum(r, "edge1"), fnum(r, "req_basket_rich")
        e2, b2 = fnum(r, "edge2"), fnum(r, "req_etf_rich")
        if (e1 is not None and b1 is not None and e1 >= b1) or \
           (e2 is not None and b2 is not None and e2 >= b2):
            clear += 1
            if truthy(r.get("traded")):
                traded += 1
            elif not truthy(r.get("within_limits")):
                blocked += 1
    if clear:
        print(f"\n (b) MORE ARBS?      {clear} observations cleared the bar")
        print(f"     traded                          : {traded} ({traded/clear:.1%})")
        print(f"     refused on the net/gross gate   : {blocked} ({blocked/clear:.1%})")
        print(f"     => the arb is LIMIT-bound, not observation-bound. Looking at")
        print(f"        the book more often does not lift this; only the net cap")
        print(f"        (currently +/-{bot.MAX_LONG_NET:,} against a {bot.SERVER_NET_LIMIT:,}")
        print(f"        server cap) or the tender reserve does.")

    # (c) tender timing, which is where extra looks genuinely land
    try:
        trows = list(csv.DictReader(open("logs/tenders.csv")))
    except OSError:
        return
    g = defaultdict(list)
    for r in trows:
        try:
            g[(r["run_id"], r["heat"], r["tender_id"])].append(
                (int(float(r["tick"])), float(r["edge_vs_mkt"]),
                 int(float(r["expires"])), float(r.get("quantity") or 0)))
        except (ValueError, TypeError, KeyError):
            continue
    lives, looks, spreads = [], [], []
    for v in g.values():
        v.sort()
        life = v[0][2] - min(t for t, _, _, _ in v) + 1
        if life <= 0:
            continue
        lives.append(life)
        looks.append(len(v))
        if len(v) >= 3:
            rng = max(e for _, e, _, _ in v) - min(e for _, e, _, _ in v)
            spreads.append(rng * statistics.fmean(q for _, _, _, q in v))
    if lives:
        spreads.sort()
        print(f"\n (c) TENDER TIMING   {len(lives)} tenders")
        print(f"     life                            : median {sorted(lives)[len(lives)//2]} ticks")
        print(f"     looks we took at it             : median {sorted(looks)[len(looks)//2]}")
        print(f"     looks per tick                  : {statistics.fmean(l/v for l, v in zip(looks, lives)):.2f}")
        if spreads:
            med = spreads[len(spreads)//2]
            print(f"     edge range across its life      : median {med:,.0f} CAD on its own size")
            print(f"     => this is the real prize. The gate re-evaluates every cycle")
            print(f"        and fires on the first clearing look; more looks inside the")
            print(f"        same {sorted(lives)[len(lives)//2]}-tick window means a better one to fire on.")
            print(f"     ASSUMPTION: the within-life range stays similar at finer")
            print(f"        sampling. Only {sum(1 for v in g.values() if len(v) >= 8)} tenders")
            print(f"        have >=8 looks, so this is not yet measured at density.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--latency", type=float, default=MEASURED_LATENCY,
                    help="simulated seconds per API call")
    ap.add_argument("--ticks", type=int, default=40)
    ap.add_argument("--skip-bench", action="store_true")
    a = ap.parse_args()

    removed = 0.0
    if not a.skip_bench:
        out = part1(a.ticks, a.latency)
        if len(out) == 2:
            removed = out[0]["staleness"] - out[1]["staleness"]
    part2(removed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
