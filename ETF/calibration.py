"""
Per-heat threshold calibration for the ETF arbitrage bot.

An arb only pays when the raw price spread clears the round-trip cost:

    required_edge = fees + slippage + margin

`fees` is deterministic: 0.02/share on each of the two CAD equity legs plus
0.02 USD on the ETF leg, ~0.0604 CAD per basket unit at current FX.

`slippage` is not. It is the gap between the top-of-book edge the bot decides
on and the VWAPs it actually gets, driven by how stale the final leg's price
has gone by the time it lands. Measured across the first 37 logged arbs it
averaged 0.068/unit against a 0.07 threshold - which is why the bot was
trading at roughly half the edge it needed and lost on 19 of 37.

`margin` is a feedback term: widen after a losing heat, narrow after a winning
one, so the bot walks toward the bar that actually pays instead of sitting on
a hand-picked constant.

Everything here is pure - it reads the CSVs the bot already writes and touches
no network - so a proposed setting can be backtested against past heats before
it is allowed to trade.
"""

import csv
from dataclasses import dataclass

FEE_MKT = 0.02
DIRECTIONS = ("basket_rich", "etf_rich")

# Candidate bars to search, in CAD per unit on top of fees.
BASE_GRID = [round(0.01 * i, 2) for i in range(2, 31)]
# Net per unit a bar must clear historically before it is allowed. Above zero
# so the chosen bar has cushion rather than sitting exactly at break-even.
TARGET_NET = 0.020
# Arbs that must survive a candidate bar before its mean is trusted.
MIN_SAMPLES_AT_BAR = 4
# Margin only reacts outside this band, so it stops hunting on noise.
MARGIN_DEADBAND = 0.010
# Most the fitted bar may move per heat. The fit can only judge bars at or
# below the one that produced its data - every logged arb cleared the bar in
# force at the time, so a lower candidate keeps all of them and always looks
# profitable. Stepping limits that extrapolation: the bar walks down one notch
# per heat, each level gets observed, and the margin controller widens again if
# the step turns out badly.
MAX_BASE_STEP = 0.010
# Most recent arbs per direction to estimate from. Older heats carry stale
# microstructure and shouldn't anchor today's threshold.
WINDOW = 60
# Below this, the slippage estimate is noise - fall back to BOOTSTRAP_SLIP.
MIN_SAMPLES = 5
# Fewer than this in a heat and the margin controller holds rather than
# reacting to one or two unlucky fills.
MIN_SAMPLES_MARGIN = 3

# Used only until MIN_SAMPLES arbs exist for a direction.
BOOTSTRAP_BASE = {"basket_rich": 0.080, "etf_rich": 0.090}

MARGIN_INIT = 0.010
MARGIN_STEP = 0.010
MARGIN_MIN = 0.000
MARGIN_MAX = 0.100

# Bounds on the non-fee part of the threshold, so a bad streak cannot drive it
# to absurdity in either direction.
BASE_MIN = 0.020
BASE_MAX = 0.300


@dataclass
class Arb:
    run_id: str
    heat: str
    ts: str
    direction: str
    expected: float    # top-of-book edge at decision time
    realised: float    # edge implied by actual fill VWAPs
    fee: float         # round-trip fee per basket unit
    net: float         # realised - fee; what the trade actually earned
    slippage: float    # expected - realised


def _f(row, key):
    v = row.get(key)
    if v in (None, "", "None"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_arbs(paths):
    """Rebuild completed 3-leg arbs from one or more trades CSVs."""
    arbs = []
    for path in paths:
        try:
            raw = list(csv.DictReader(open(path, newline="")))
        except FileNotFoundError:
            continue
        # Drop non-arb rows (unwind fills, say) BEFORE grouping. They carry
        # leg=0 and would otherwise be swept into the preceding arb's group,
        # pushing it to four rows so _build discards a perfectly good arb.
        rows = [r for r in raw if r.get("direction") in DIRECTIONS]
        group = []
        for r in rows:
            if r.get("leg") == "1" and group:
                arbs.extend(_build(group))
                group = []
            group.append(r)
        if group:
            arbs.extend(_build(group))
    arbs.sort(key=lambda a: a.ts)
    return arbs


def _build(group):
    if len(group) != 3:
        return []
    head = group[0]
    d = head.get("direction")
    if d not in DIRECTIONS:
        return []
    expected = _f(head, "edge_at_decision")
    vwap = {g.get("ticker"): _f(g, "vwap") for g in group}
    if expected is None or any(vwap.get(t) is None for t in ("BULL", "BEAR", "RITC")):
        return []
    # The FX side actually used depends on which way the ETF leg went.
    fx = _f(head, "usd_ask") if d == "basket_rich" else _f(head, "usd_bid")
    if not fx:
        return []
    ritc_cad = vwap["RITC"] * fx
    realised = (vwap["BULL"] + vwap["BEAR"] - ritc_cad) if d == "basket_rich" \
        else (ritc_cad - vwap["BULL"] - vwap["BEAR"])
    fee = 2 * FEE_MKT + FEE_MKT * fx
    return [Arb(head.get("run_id", ""), head.get("heat", ""), head.get("ts", ""),
                d, expected, realised, fee, realised - fee, expected - realised)]


def quantile(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def required_edge(base, fx):
    """Raw top-of-book edge a trade must clear, given the live FX rate."""
    return 2 * FEE_MKT + FEE_MKT * fx + base


def lowest_profitable_base(arbs_dir):
    """Lowest bar whose surviving arbs historically net > TARGET_NET.

    Scanning upward and stopping at the FIRST profitable bar keeps volume as
    high as profitability allows. The earlier design estimated slippage and
    added it to fees, which ratcheted: raising the bar admitted only wide
    spreads, wide spreads slip more in absolute terms, the estimate rose, and
    the bar rose again - 16 of 31 heats ended up taking no trades at all.
    Fitting the bar directly on net outcome has no such feedback, because a
    bar that admits profitable trades is accepted immediately.
    """
    for base in BASE_GRID:
        kept = [a for a in arbs_dir if a.expected >= a.fee + base]
        if len(kept) < MIN_SAMPLES_AT_BAR:
            continue
        mean_net = sum(a.net for a in kept) / len(kept)
        if mean_net >= TARGET_NET:
            return base, mean_net, len(kept)
    return None, None, 0


def calibrate(arbs, margins, last_heat=None, prev_bases=None):
    """Derive next heat's per-direction base thresholds from logged fills.

    `margins` and `prev_bases` carry controller state between heats.
    `last_heat` is the (run_id, heat) whose results drive the widen/narrow step.

    Returns (bases, margins, diagnostics).
    """
    bases, out_margins, diag = {}, {}, {}

    for d in DIRECTIONS:
        same = [a for a in arbs if a.direction == d]
        recent = same[-WINDOW:]

        if len(recent) >= MIN_SAMPLES:
            fitted, mean_net, n_kept = lowest_profitable_base(recent)
            if fitted is None:
                fitted = BASE_GRID[-1]
                slip_src = f"no profitable bar in {len(recent)} arbs - at ceiling"
            else:
                slip_src = f"fit on {len(recent)} arbs: {n_kept} survive, net {mean_net:+.4f}"
            slip = fitted
        else:
            slip = BOOTSTRAP_BASE[d]
            slip_src = f"bootstrap (only {len(recent)} arbs)"

        margin = margins.get(d, MARGIN_INIT)
        prev = [a for a in same if last_heat and (a.run_id, a.heat) == last_heat]
        if len(prev) >= MIN_SAMPLES_MARGIN:
            mean_net = sum(a.net for a in prev) / len(prev)
            if mean_net < -MARGIN_DEADBAND:
                margin = min(margin + MARGIN_STEP, MARGIN_MAX)
                action = f"widen (last heat {mean_net:+.4f}/unit over {len(prev)})"
            elif mean_net > MARGIN_DEADBAND:
                margin = max(margin - MARGIN_STEP, MARGIN_MIN)
                action = f"narrow (last heat {mean_net:+.4f}/unit over {len(prev)})"
            else:
                action = f"hold (last heat {mean_net:+.4f}/unit, inside deadband)"
        else:
            action = f"hold (only {len(prev)} arbs last heat)"

        base = max(BASE_MIN, min(BASE_MAX, slip + margin))

        # Walk toward the fitted bar rather than jumping to it.
        prior = (prev_bases or {}).get(d)
        if prior is not None and abs(base - prior) > MAX_BASE_STEP:
            stepped = prior + (MAX_BASE_STEP if base > prior else -MAX_BASE_STEP)
            slip_src += f" | step {prior:.3f}->{stepped:.3f} (want {base:.3f})"
            base = stepped

        bases[d] = round(base, 4)
        out_margins[d] = round(margin, 4)
        diag[d] = {"slip": round(slip, 4), "slip_src": slip_src,
                   "margin": round(margin, 4), "action": action,
                   "base": round(base, 4), "n_total": len(same)}

    return bases, out_margins, diag
