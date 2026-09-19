"""
Replay logged heats through the tender policy, so policies are compared on the
SAME market paths.

Why this exists. Live heats compare versions on different markets. Heat P&L has
an sd of ~53,000 CAD, so telling a real 10,000/heat difference from noise needs
~450 full heats per version - days of running. Replaying two policies over the
same logged heats cancels the market out of the comparison, and what is left is
the policy difference alone.

What is replayed exactly, what is modelled, what is left out:

  EXACT     The tender stream - every tender quoted, accepted or not. It does
            not depend on our actions. The price path, and therefore every
            tender held to close-out: close-out was proven live to price at the
            mid with no fee and no impact (a v17 heat expected +24,919 CAD and
            returned +24,913).
  MODELLED  Unwind execution. Each unwind chunk exits at the logged mid plus a
            measured execution cost, `exec_cost`. The tape already contains our
            own past impact and carries no trade prints, so execution cannot be
            replayed, only assumed. Nothing here can evaluate a change to HOW we
            unwind - see VERSIONS.md v8/v9 for what trusting that costs.
  FX        Every USD flow is converted to CAD at the rate when it happens,
            because that is what the live bot does - `hedge_fx` flattens USD
            cash each cycle. This matters more than anything else modelled:
            USD/CAD moves with an sd of ~0.04 inside a heat, and a tender's
            notional is ~2.25M USD. Converting at the close instead gave a
            correlation with live heats of 0.42; converting as it happens, 0.86.
  IGNORED   The spread arb (~800 CAD/heat) and the gross it uses; the v13
            pre-hedge (has fired about once).

Run it:

    python replay_tenders.py

It validates the model against live heats before running any experiment, and
every comparison is paired: same heats, same tenders, same prices.
"""

import csv
import random
import statistics as st
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path

LOG = Path(__file__).resolve().parent / "logs"

FEE = 0.02            # per unit on a market unwind
BASIS_GAP = 0.0172    # mean chunk-VWAP gap to top-of-book, measured in v6
LAST_TICK = 299
FLICKER_JUMP = 50_000  # a position read this far off, reverting next cycle,
FLICKER_TOL = 2_000    # is a settlement flicker rather than a real move


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rows(pattern):
    for path in sorted(LOG.glob(pattern)):
        with path.open(newline="") as fh:
            yield from csv.DictReader(fh)


# ============================================================================
# DATA
# ============================================================================

@dataclass
class Obs:
    """One sighting of a live tender. A tender is seen on several cycles."""
    tick: int
    tid: str
    action: str          # what WE do: SELL or BUY RITC
    qty: float
    price: float         # USD
    mid: float           # RITC mid at the sighting
    unwind_ref: float    # price a market unwind would get: chunk VWAP


@dataclass
class Heat:
    key: tuple           # (run_id, heat)
    model: str
    pnl_cad: float
    mids: dict           # tick -> RITC mid, USD
    close_mid: float
    usd_close: float
    usd: dict = field(default_factory=dict)            # tick -> USD/CAD mid
    obs: list = field(default_factory=list)
    live_accepts: dict = field(default_factory=dict)   # tid -> tick accepted
    flickered: set = field(default_factory=set)        # tids hit by a flicker


def load_heats():
    """Every full heat (ticks 0-299) that has tender observations logged."""
    meta = {}
    for r in _rows("heats*.csv"):
        ft, lt, pnl = _f(r.get("first_tick")), _f(r.get("last_tick")), _f(r.get("heat_pnl"))
        if None not in (ft, lt, pnl) and ft <= 5 and lt >= 295:
            meta[(r.get("run_id"), r.get("heat"))] = (r.get("risk_model"), pnl)

    mids, usd, pos = defaultdict(dict), defaultdict(dict), defaultdict(list)
    for r in _rows("ticks*.csv"):
        k = (r.get("run_id"), r.get("heat"))
        if k not in meta:
            continue
        t = int(_f(r.get("tick")) or 0)
        b, a = _f(r.get("ritc_bid_usd")), _f(r.get("ritc_ask_usd"))
        if b and a and 0 < b < a < 100:
            mids[k][t] = (a + b) / 2
        ub, ua = _f(r.get("usd_bid")), _f(r.get("usd_ask"))
        if ub and ua and 0 < ub < ua < 5:
            usd[k][t] = (ua + ub) / 2
        p = _f(r.get("pos_ritc"))
        if p is not None:
            pos[k].append((t, p))

    obs, accepts, seen = defaultdict(list), defaultdict(dict), set()
    for r in _rows("tenders*.csv"):
        k = (r.get("run_id"), r.get("heat"))
        if k not in meta:
            continue
        t = int(_f(r.get("tick")) or 0)
        q, px = _f(r.get("quantity")), _f(r.get("price"))
        act = (r.get("action") or "").upper()
        tid = r.get("tender_id")
        if not q or px is None or act not in ("BUY", "SELL"):
            continue
        if r.get("accept_ok") == "True":
            accepts[k].setdefault(tid, t)
        if (k, tid, t) in seen:        # an accept writes a second row
            continue
        seen.add((k, tid, t))
        bid, ask = _f(r.get("mkt_bid")), _f(r.get("mkt_ask"))
        mid = (bid + ask) / 2 if bid and ask and 0 < bid < ask else mids[k].get(t)
        if mid is None:
            continue
        cv = _f(r.get("chunk_vwap"))
        if cv:
            ref = cv
        elif act == "SELL":   # buy back through the asks
            ref = (ask or mid) + BASIS_GAP
        else:                 # sell out through the bids
            ref = (bid or mid) - BASIS_GAP
        obs[k].append(Obs(t, tid, act, q, px, mid, ref))

    heats = []
    for k, (model, pnl) in meta.items():
        if not obs[k]:
            continue
        late = [t for t in mids[k] if t >= 290]
        if not late:
            continue
        close_mid = mids[k][max(late)]
        usd_close = usd[k][max(usd[k])] if usd[k] else 1.0
        h = Heat(k, model, pnl, mids[k], close_mid, usd_close, usd[k],
                 sorted(obs[k], key=lambda o: o.tick), accepts[k])
        h.flickered = _flickers(pos[k], accepts[k])
        heats.append(h)
    return heats


def _flickers(series, accepts):
    """Tenders followed within 5 ticks by a one-cycle transient position read."""
    hit = set()
    for i in range(1, len(series) - 1):
        (_, a), (t, b), (_, c) = series[i - 1], series[i], series[i + 1]
        if abs(a - c) < FLICKER_TOL and abs(b - a) >= FLICKER_JUMP:
            near = [(at, tid) for tid, at in accepts.items() if 0 <= t - at <= 5]
            if near:
                hit.add(max(near)[1])
    return hit


# ============================================================================
# POLICY + SIMULATION
# ============================================================================

@dataclass(frozen=True)
class Policy:
    name: str
    min_edge: float = 0.04        # unwind basis: vs chunk VWAP, after the fee
    min_edge_held: float = 0.04   # close-out basis: vs mid, no fee
    hold_tick: int = 200          # from here on nothing is unwound
    exec_cost: float = 0.0249     # per unit vs mid; v20's passive-regime figure
    unwind_ticks: int = 8         # glide from full to flat
    net_cap: float = 197_000      # server net 200,000 less the 3,000 buffer
    strand_on_flicker: bool = False   # the v16-v21 tracker write-back bug
    fx_live: bool = True          # convert USD flows as they happen (live bot)


def passes_gate(o, position, pol):
    signed = -o.qty if o.action == "SELL" else o.qty
    if abs(2 * (position + signed)) > pol.net_cap:      # RITC counts x2
        return False
    if o.tick >= pol.hold_tick:
        edge = (o.price - o.mid) if o.action == "SELL" else (o.mid - o.price)
        return edge >= pol.min_edge_held
    edge = (o.price - o.unwind_ref) if o.action == "SELL" else (o.unwind_ref - o.price)
    return edge - FEE >= pol.min_edge


def simulate(heat, pol, flicker=frozenset(), forced=None):
    """Replay one heat. Returns (P&L in CAD, {tid: accept tick}).

    `forced` replays exactly the live accept decisions instead of the gate,
    which isolates the exit model for validation.
    """
    by_tick = defaultdict(list)
    for o in heat.obs:
        by_tick[o.tick].append(o)

    cad = position = stranded = 0.0
    glide_tick, glide_from = None, 0.0
    taken, mid, fx = {}, None, None
    for t in range(LAST_TICK + 1):
        mid = heat.mids.get(t, mid)
        fx = heat.usd.get(t, fx) if pol.fx_live else heat.usd_close
        rate = fx or heat.usd_close

        # Unwind the working inventory along a linear glide to flat. Stranded
        # inventory - the part the tracker lost - is never unwound.
        working = position - stranded
        if working and mid and glide_tick is not None and t < pol.hold_tick:
            frac = max(0.0, 1 - (t - glide_tick) / pol.unwind_ticks)
            trade = glide_from * frac - working            # > 0 buys
            if abs(trade) > 1e-9:
                fill = mid + pol.exec_cost if trade > 0 else mid - pol.exec_cost
                cad -= trade * fill * rate
                position += trade

        for o in by_tick.get(t, ()):
            if o.tid in taken:
                continue
            if forced is not None:
                if forced.get(o.tid) != t:
                    continue
            elif not passes_gate(o, position, pol):
                continue
            signed = -o.qty if o.action == "SELL" else o.qty
            cad -= signed * o.price * rate
            position += signed
            taken[o.tid] = t
            if pol.strand_on_flicker and o.tid in flicker and t < pol.hold_tick:
                stranded = position
            glide_tick, glide_from = t, position - stranded

    cad += position * heat.close_mid * heat.usd_close   # close-out: exact, free, no impact
    return cad, taken


# ============================================================================
# REPORTING
# ============================================================================

def paired(a, b):
    """Mean of b - a over the same heats, its standard error and t."""
    d = [y - x for x, y in zip(a, b)]
    se = st.stdev(d) / len(d) ** 0.5 if len(d) > 1 else float("nan")
    return st.mean(d), se, st.mean(d) / se if se else float("nan")


def summary(v):
    return f"mean {st.mean(v):>+9,.0f}  median {st.median(v):>+9,.0f}  sd {st.stdev(v):>8,.0f}"


# Parameters each live version actually ran, for validating against its heats.
LIVE = {
    "v14_holdout": dict(hold_tick=200, min_edge=0.08, min_edge_held=0.08),
    "v17_holdtick": dict(hold_tick=175, min_edge=0.08, min_edge_held=0.04, strand_on_flicker=True),
    "v21_grossreserve": dict(hold_tick=200, strand_on_flicker=True),
    "v22_settlelag": dict(hold_tick=200),
    "v23_agreedclamp": dict(hold_tick=200),
}


def main():
    heats = load_heats()
    print(f"{len(heats)} full heats with tender data, "
          f"{sum(len({o.tid for o in h.obs}) for h in heats)} distinct tenders\n")

    # ---- 1. Validate the exit model: replay the live accepts, compare P&L. ----
    val = [h for h in heats if h.model in LIVE and h.live_accepts]
    rep = [simulate(h, Policy(h.model, **LIVE[h.model]), h.flickered, h.live_accepts)[0]
           for h in val]
    act = [h.pnl_cad for h in val]
    diff = [a - r for a, r in zip(act, rep)]
    mx, my = st.mean(act), st.mean(rep)
    cov = sum((x - mx) * (y - my) for x, y in zip(act, rep)) / (len(act) - 1)
    corr = cov / (st.stdev(act) * st.stdev(rep))
    print("1. EXIT MODEL - live accepts replayed, same heats")
    print(f"   {len(val)} heats   correlation {corr:.2f}   "
          f"bias {st.mean(diff):+,.0f}/heat   sd of error {st.stdev(diff):,.0f} "
          f"vs sd of heats {st.stdev(act):,.0f}")

    # ---- 2. Validate the gate: does it pick the tenders the bot picked? -------
    v21 = [h for h in heats if h.model == "v21_grossreserve" and h.live_accepts]
    live_set = {(h.key, t) for h in v21 for t in h.live_accepts}
    sim_set = set()
    for h in v21:
        _, took = simulate(h, Policy("v21", **LIVE["v21_grossreserve"]), h.flickered)
        sim_set |= {(h.key, t) for t in took}
    both = len(live_set & sim_set)
    print("\n2. GATE - v21 parameters, v21 heats")
    print(f"   live accepted {len(live_set)}, replay accepted {len(sim_set)}, "
          f"agree on {both} ({both / max(len(live_set), 1):.0%} of live)")

    # ---- 3. v21 vs v23 on every heat, same flickers --------------------------
    flick_events = sum(len(h.flickered) for h in heats)
    pre_hold = sum(1 for h in heats for t in h.live_accepts.values() if t < 200)
    p = flick_events / max(pre_hold, 1)
    base = Policy("v23", hold_tick=200)
    bug = replace(base, name="v21", strand_on_flicker=True)
    a = [simulate(h, bug, h.flickered)[0] for h in heats]
    b = [simulate(h, base, h.flickered)[0] for h in heats]
    m, se, t = paired(a, b)
    print(f"\n3. v21 vs v23 - {len(heats)} heats, flickers where they actually happened")
    print(f"   v21 {summary(a)}")
    print(f"   v23 {summary(b)}")
    print(f"   v23 - v21: {m:+,.0f}/heat  (se {se:,.0f}, t {t:+.2f})")

    rng = random.Random(7)
    mc_a, mc_b = [0.0] * len(heats), [0.0] * len(heats)
    runs = 300
    for _ in range(runs):
        for i, h in enumerate(heats):
            hit = {o.tid for o in h.obs if rng.random() < p}   # same draw, both policies
            mc_a[i] += simulate(h, bug, hit)[0] / runs
            mc_b[i] += simulate(h, base, hit)[0] / runs
    m, se, t = paired(mc_a, mc_b)
    print(f"   at the measured flicker rate ({p:.1%} of pre-hold accepts, "
          f"{runs} draws): {m:+,.0f}/heat  (se {se:,.0f}, t {t:+.2f})")

    # ---- 5. Held inventory: what is it actually exposed to? ----------------
    keep_usd = replace(base, name="keep USD", fx_live=False)
    a = [simulate(h, base)[0] for h in heats]
    b = [simulate(h, keep_usd)[0] for h in heats]
    m, se, t = paired(a, b)
    print(f"\n5. FX TREATMENT - {len(heats)} heats, v23 logic")
    print(f"   convert USD as it happens (live)  {summary(a)}")
    print(f"   keep USD until close-out          {summary(b)}")
    print(f"   keep - convert: {m:+,.0f}/heat  (se {se:,.0f}, t {t:+.2f})")

    # ---- 4. Sweep the gate and the hold, paired against the live settings ----
    print(f"\n4. SWEEP - v23 logic, {len(heats)} heats, paired against min_edge 0.04 / hold 200")
    ref = [simulate(h, base)[0] for h in heats]
    print(f"   {'min_edge':>8} {'hold':>5}   {'mean':>9} {'median':>9}   {'vs live':>9} {'t':>6}")
    for me in (0.00, 0.02, 0.04, 0.06, 0.08, 0.12):
        for hold in (150, 175, 200, 225, 250, 300):
            pol = replace(base, min_edge=me, min_edge_held=me, hold_tick=hold)
            v = [simulate(h, pol)[0] for h in heats]
            m, se, t = paired(ref, v)
            mark = "  <- live" if (me, hold) == (0.04, 200) else ("  *" if abs(t) >= 2 else "")
            print(f"   {me:>8.2f} {hold:>5}   {st.mean(v):>+9,.0f} {st.median(v):>+9,.0f}"
                  f"   {m:>+9,.0f} {t:>+6.2f}{mark}")
    print("\n   * = |t| >= 2. hold 300 means never hold. Held P&L is exact; unwind P&L")
    print("   assumes exec_cost - rerun with a different one before trusting a hold change.")


if __name__ == "__main__":
    main()
