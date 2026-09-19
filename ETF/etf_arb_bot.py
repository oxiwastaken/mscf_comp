"""
RITCx Algorithmic ETF Arbitrage - working bot with trade logging.

Derived from "RITCx ETF Arbitrage Case base script-DMA API.py"
(Rotman BMO Finance Research and Trading Lab, University of Toronto).

What this adds over the base script:
  1. LOGGING. Every tick, every order leg, every tender and every heat is
     appended to CSV under logs/. The case wipes /orders when it resets, so
     anything not written down during the heat is gone afterwards.
  2. CONTINUOUS HEATS. Heats run tick 0-300, pause, then start again. The base
     script exits the moment status stops being ACTIVE; this one waits out the
     gap and picks up the next heat on its own.
  3. VISIBLE FILLS. The base script computed `traded` and never printed it, so
     a run left no evidence of having traded. Each arb now prints its legs and
     the edge it actually realised.

Edges and ARB_THRESHOLD_CAD are unchanged from the base script. The exposure
maths in risk_metrics() is NOT: see RISK_MODEL. Tenders are still accepted
unconditionally, left as-is until there is enough logged tender data to
justify a profitability rule.
"""

import base64
import csv
import os
import signal
from datetime import datetime
from pathlib import Path
from time import sleep

import requests

import calibration

# ============================================================================
# CONNECTION
# ============================================================================

API_ENDPOINT = "http://flserver.rotman.utoronto.ca:16635/v1"

def _load_dotenv(path=".env"):
    """Read KEY=VALUE lines from a gitignored .env into the environment.

    Credentials stay out of the source file so this can be committed. Kept
    dependency-free rather than pulling in python-dotenv for six lines.
    """
    f = Path(__file__).resolve().parent / path
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()
USERNAME = os.environ.get("RIT_USER")
PASSWORD = os.environ.get("RIT_PASS")


def authorization():
    """Basic-auth header, resolved at connect time rather than at import.

    Checked here and not at module scope so the module stays importable without
    credentials - `sim_unwind.py` drives the real trading functions against a
    fake exchange, and it cannot do that if importing the bot exits the process.
    """
    if not USERNAME or not PASSWORD:
        raise SystemExit(
            "Missing credentials. Copy .env.example to .env and fill in "
            "RIT_USER / RIT_PASS, or export them in your shell."
        )
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    return {'Authorization': 'Basic ' + token}

shutdown = False

# RITC acquired from tenders, signed, reset each heat. The unwind targets THIS
# rather than the raw RITC position: the arb's ETF leg is a hedge against its
# own BULL/BEAR legs, and selling it off turns a hedged arb into a naked short
# basket. That is exactly what happened in v10 heat 4 - the arb bought 5,000
# RITC per cycle, the unwind sold it straight back, RITC sat flat at 7,000
# while BULL/BEAR ran to -20,000 each and NLV fell ~9,000 in ten ticks.
tender_position = 0

# Basket put on purely to make net room for an oversized tender, signed, units
# per side (BULL and BEAR carry the same amount). Tracked separately from the
# arb's own BULL/BEAR for the same reason `tender_position` is tracked
# separately from raw RITC: flattening it by reading the aggregate position
# would eat the arb's legs, which is the v11 bug one instrument over.
prehedge_position = 0


class ApiException(Exception):
    pass


def signal_handler(signum, frame):
    # Graceful shutdown on CTRL+C, so the CSVs get closed cleanly.
    global shutdown
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    shutdown = True
    print("\nShutdown requested - finishing current cycle...")


def handle_rate_limit(response):
    if response.status_code == 429:
        wait_time = float(response.headers.get('Retry-After', response.json().get('wait', 1)))
        print(f"Rate limit exceeded. Waiting {wait_time}s before retrying.")
        sleep(wait_time)
        return True
    return False


def handle_auth_failure(response):
    global shutdown
    if response.status_code == 401:
        print("Authentication failed. Check RIT_USER / RIT_PASS.")
        shutdown = True
        return True
    return False


MAX_NET_RETRIES = 6
HTTP_TIMEOUT = 10


def api_request(session, method, endpoint, params=None):
    attempt = 0
    while True:
        url = f"{API_ENDPOINT}/{endpoint}"
        try:
            if method == 'GET':
                resp = session.get(url, params=params, timeout=HTTP_TIMEOUT)
            elif method == 'POST':
                resp = session.post(url, params=params, timeout=HTTP_TIMEOUT)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
        except requests.exceptions.RequestException as e:
            # Transport-level failure: connection reset, read timeout, DNS.
            # The RIT server drops connections periodically and an unhandled
            # one killed a run mid-heat, so retry with backoff instead of
            # letting it escape as a bare requests exception.
            attempt += 1
            if attempt > MAX_NET_RETRIES:
                raise ApiException(f"network error after {MAX_NET_RETRIES} retries: {e}")
            backoff = min(0.25 * 2 ** attempt, 5.0)
            print(f"Network error ({type(e).__name__}) - retry {attempt}/{MAX_NET_RETRIES} in {backoff:.2f}s")
            sleep(backoff)
            continue
        attempt = 0
        if handle_auth_failure(resp):
            return None
        if handle_rate_limit(resp):
            continue
        if resp.ok:
            return resp.json()
        raise ApiException(f"API request failed: {resp.text}")


# ============================================================================
# CONFIG
# ============================================================================

CAD = "CAD"
USD = "USD"
BULL = "BULL"
BEAR = "BEAR"
RITC = "RITC"

FEE_MKT = 0.02
REBATE_LMT = 0.01
MAX_SIZE_EQUITY = 10000
MAX_SIZE_FX = 2500000

MAX_LONG_NET = 25000
MAX_SHORT_NET = -25000
MAX_GROSS = 250000   # server gross_limit is 300000 (0.5/unit fine above it)
ORDER_QTY = 5000

# Residual USD below this is left alone, to avoid churning on rounding.
FX_HEDGE_TOLERANCE = 2000

# Server-enforced caps (from GET /limits); fines are 0.5/unit past them.
SERVER_GROSS_LIMIT = 300000
SERVER_NET_LIMIT = 200000
# Absolute headroom, not a percentage. Tenders here are sized 90k-99k units of
# RITC = 180k-198k exposure at the x2 multiplier, i.e. deliberately just inside
# the 200k net cap. A 10% safety factor rejected a legal 182,000 outright.
LIMIT_BUFFER = 3000
# Minimum edge per unit, after the unwind fee, before a tender is worth taking.
# The edge swings across a tender's ~5-tick life, so the gate is re-evaluated
# each cycle and fires on a good one rather than the first one.
#
# RESTORED to 0.08 in v10. The 0.05 of v8/v9 was justified by a per-chunk
# slippage figure of +0.0133/unit that turned out to measure the wrong thing
# (see MAX_UNWIND_SLIP), so the break-even it implied was far too optimistic.
# 0.08 is the bar v7 ran at while realising +0.235/unit.
#
# Originally lowered from 0.10 when v6 started pricing the unwind at chunk VWAP. That
# removed ~0.017/unit of phantom edge from the measurement, so an unchanged
# 0.10 would have been materially stricter than the 0.10 that produced heat 2's
# profitable 75% capture rate - a silent second tightening. 0.08 keeps the
# effective bar where the evidence says it worked.
MIN_TENDER_EDGE = 0.08
# Bar for a tender that will be HELD to close-out rather than unwound, priced
# against the mid with no fee (see HOLD_TO_CLOSEOUT_TICK).
#
# The two bars are not comparable, because they sit on different measurements.
# `MIN_TENDER_EDGE` is measured after subtracting a market unwind; this one is
# not, and across 385 logged tenders that difference is worth +0.0626/unit on
# average (median +0.0550, p10 +0.0250). So 0.04 here is a materially STRICTER
# requirement than 0.08 there, not a looser one.
#
# 0.04 because the risk on a held tender is not execution - there is none - but
# that close-out lands on the wrong side of the book rather than at the mid.
# One half-spread covers that, and the median RITC spread is 0.08. Drift to the
# close is mean-zero (-0.0126/unit against a 0.0779 sd), so it needs no cushion.
#
# Worth 11 tenders and ~3,017 USD/heat across the logged history: tenders like
# +0.0086 on 93,000 units that the unwind-priced bar refused, which are
# +0.0836 once the exit is close-out.
MIN_TENDER_EDGE_HELD = 0.04
# Guards the threshold comparison against float noise. Without it an edge of
# 0.09999999 printed as "+0.1000 < 0.1" and declined an 84,000-unit tender
# worth ~8,400 USD over a rounding artefact.
EDGE_EPSILON = 1e-6
# Chunks per cycle. Capacity, not slippage, is the binding constraint: six
# tenders worth ~78,000 USD across three heats were refused on "net would hit"
# because an earlier tender was still unwinding when they arrived (~26,000
# USD/heat). Measured unwind slippage is +0.0133/unit against blocked edges of
# +0.09 to +0.235, so clearing the book faster wins by roughly 10x even if
# slippage worsens. The server allows 50 orders/sec, so 3 is well inside limits.
# REVERTED to 1 in v10. Three chunks per cycle concentrated the unwind and
# moved the market against us: realised edge per unit fell from +0.235 (v7, one
# chunk) to +0.082 (v8) to +0.031 (v9), turning several genuinely good tenders
# into losses. The book refills between cycles, so one chunk per cycle lets the
# price recover instead of chasing it.
UNWIND_CHUNKS_PER_CYCLE = 1
# Retained as a safety stop, but do NOT trust it as a cost measure. It compares
# each fill against top-of-book AT THAT INSTANT, so it cannot see our own
# unwind dragging the quote between cycles - it read +0.0123/unit while the
# true cost of a 92,000 unit unwind was ~0.14/unit of self-inflicted drift.
# Realised cost is measured properly in tender_pnl.csv: tender price against the
# VWAP of the entire unwind.
#
# Stop chunking this cycle once a fill comes back worse than this. The book
# refills BETWEEN cycles, not within one, so back-to-back orders just walk it:
# measured 3-chunk cycles ran +0.0316 / +0.1100 / +0.1397, a 4x escalation, for
# ~0.083/unit against 0.0133 at one chunk. Reacting to the realised fill keeps
# the speed when the book is deep and backs off when it is thin, instead of
# picking a fixed chunk count that is wrong in one direction or the other.
MAX_UNWIND_SLIP = 0.05
# Work an oversized RITC position back toward flat at this rate per cycle.
UNWIND_CHUNK = 10000
# Book levels counted when sizing an unwind chunk.
DEPTH_LEVELS = 3
# Never take more than this share of visible depth in one order.
DEPTH_FRACTION = 0.5
# Clear tender positions all the way down, not to a resting level. Tenders
# arrive ~4x a heat and each commits ~160k of the 300k gross cap, so capacity
# left parked in a residual is a tender not taken. 2000 only damps churn against
# the arb's own small positions.
UNWIND_THRESHOLD = 2000

# --- passive unwind ------------------------------------------------------
# Every order this bot has ever sent has been a MARKET order, so the unwind has
# been paying 0.02/share in fees and crossing a 0.08-0.10 spread on ~490,000
# shares a heat. The case pays a 0.01/share REBATE on passive limit fills, so
# each share unwound passively instead of aggressively is worth:
#
#     0.03  fee (-0.02) turned into rebate (+0.01)
#   + 0.01  sold at bid+0.01 instead of at the bid
#   + ~     no book-walk: a resting order does not take liquidity, so it cannot
#           produce the self-inflicted drift that v10 measured at ~0.14/unit
#           across a 92,000 unit market unwind
#
# At 490,000 shares/heat the fee term alone is ~14,700 CAD/heat.
#
# Fill rates, measured on the logged RITC quote series (9,312 observations): a
# sell resting at bid+0.01 is reached by a later bid 54% of the time within 3
# cycles, 71% within 8, 80% within 20. Median RITC spread is 0.08, so bid+0.01
# is comfortably inside it and rests as the best offer rather than joining a
# queue.
PASSIVE_UNWIND = True
# Minimum price increment. Every BULL/BEAR/RITC quote in 55,886 logged
# observations is cent-quantised; only USD trades in finer increments.
PRICE_TICK = 0.01
# Cycles allowed to clear a tender position before the aggressive layer takes
# over completely.
#
# Set to the pace the v10 market chunker already achieves (~8 cycles) rather
# than to anything slower, so the passive layer DISPLACES aggressive fills
# instead of DELAYING them. sweep_deadline.py over 463 windows of the logged
# tape, per ~90,000 unit unwind:
#
#   deadline   passive   flat@   fee saved   price sd   worst window
#          8       15%     8.7        +405     0.0248        -36,000
#         12       23%    12.0        +608     0.0750        -89,000
#         25       47%    24.9      +1,280     0.1847       -175,000
#
# The extra fee at a longer deadline is real but small; the variance it buys is
# not. Drift over the extra cycles has a mean near zero (-0.005/unit at every
# setting) and a spread that grows 7x from 8 to 25 cycles, on a heat P&L that
# already ranges -16,522 to +74,664. Paying ~875 USD of certain fee saving for
# a 0.16/unit wider distribution on 90,000 units is a bad trade.
#
# At 8 the price sd (0.0248) matches the market-only baseline (0.0254) and the
# position still clears in 8.7 cycles against 8.0 - the rebate is earned for
# free. Below 8 the setting stops binding: the depth-limited chunker cannot go
# faster anyway, so 3 and 5 give identical results.
UNWIND_DEADLINE_CYCLES = 8
# Passive orders resting at once. One order at the touch is what earns the
# rebate; stacking more of them just shows the market our full size.
PASSIVE_MAX_LIVE = 1
# Size of a single passive order. Larger than the market chunk because a
# resting order does not move the price - it only fills if someone comes to it.
PASSIVE_CHUNK = 10000
# Re-post a resting order once the touch has moved this far from its price.
# Below this it stays put: cancel/replace costs two requests and loses queue
# position, and the order is still the best offer while the touch is within a
# tick of it.
PASSIVE_REPRICE_TICKS = 1

# --- hold to close-out ---------------------------------------------------
# Past this tick, tender inventory is NOT unwound. It is left on the book for
# the case to close out.
#
# The case is explicit: "Any non-zero position will be closed out at the end of
# trading at either the last traded price or the correct price... participants
# are not required to close statistical arbitrage positions as the iteration
# ends." Close-out charges no commission and has no market impact, so relative
# to the mid it costs 0. Measured against that, unwinding costs:
#
#     market order   -0.0550 (fill vs mid) - 0.0200 (fee)  = -0.0750/unit
#     passive order  -0.0300 (bid+0.01 vs mid) + 0.0100    = -0.0200/unit
#     close-out                                            =  0
#
# decompose_unwind.py measured that -0.0550 across 32 live tenders with a
# standard deviation of 0.0304 - it is systematic, not noise, and it is 81% of
# everything a tender gives back. On a median 85,000 unit tender the market
# figure is 6,375 USD.
#
# The only thing the unwind buys is capacity for the NEXT tender, so the
# question is how many more are coming - and specifically how many that a held
# position would actually block. Only LATER, SAME-DIRECTION tenders are
# blocked: an opposite-direction tender reduces net rather than adding to it,
# and is in fact the ideal outcome, since it flattens the held position and
# pays us for the privilege. 43% of consecutive tenders are opposite, which
# roughly halves the cost of holding. Over 234 logged tenders:
#
#     arrives      0-29  30-59  60-89  90-119 120-149 150-179 180-209 210-239
#     all later    3.50   3.11   2.62    1.89    1.70    0.96    0.48    0.03
#     same-dir     2.04   1.75   1.62    1.04    0.91    0.46    0.34    0.00
#
# `sweep_holdtick.py` turns that into expected value per heat. The central
# estimate peaks at 150 (+6,629) with 175 (+6,300) and 200 (+5,666) behind it -
# a flat curve, so the choice is decided by robustness rather than by the peak.
#
# The cost side depends on what a captured tender is worth, which is a median
# over 32 noisy observations. Sweeping that from 4,000 to 20,000 USD moves the
# argmax from 90 all the way to 225, so the point estimate cannot be trusted on
# its own. Worst-case regret across that whole range:
#
#     threshold    0      60      90     120    150    175    200    225
#     regret    92,125  45,441  25,139  14,640  3,706  3,732  5,278  8,092
#
# 150 and 175 are tied (26 USD/heat apart) and both clearly beat 200. The
# tiebreak is the one thing the sweep deliberately does not price: drift.
# Holding is mean-zero (-0.0126/unit against a 0.0779 sd) so it cannot move the
# argmax, but it does widen heat P&L, and 175 holds 1.79 tenders/heat for up to
# 125 ticks against 150's 2.28 for up to 150. Same expected value, less
# variance - so 175.
HOLD_TO_CLOSEOUT_TICK = 175

# --- tender pre-hedge ----------------------------------------------------
# The largest tenders do not fit the net limit even on a completely flat book.
# At the ETF's x2 multiplier a 99,000 unit tender is 198,000 of net against a
# 200,000 cap, and 100,000 is exactly 200,000 - so the buffer alone refuses
# them. They are not rare: 6% of 234 logged tenders are 98,500 or larger, and
# the ones clearing the edge bar are worth ~4,200 USD/heat.
#
# One BULL plus one BEAR carries the same exposure as one RITC but at a x1
# multiplier each, so selling `need/2` of both moves net by `need` in the
# opposite direction and buys exactly that much room. It costs 2 x need/2
# shares of fee - 60 USD to reach a 22,000 USD tender in the worst logged case
# - and it is a genuine hedge rather than a limits trick: short basket against
# long ETF lowers the directional exposure carried through the unwind.
PREHEDGE = True
# Cap per side. Sized to cover the whole observed shortfall (max 3,000 of net,
# so 1,500 a side) with room to spare, while staying small enough that a hedge
# left on by a failed accept is a rounding error rather than a position.
MAX_PREHEDGE = 5000

# Retained only as the pre-calibration fallback; live bars come from
# calibration.calibrate() and are logged per heat.
ARB_THRESHOLD_CAD = 0.07

# Tag for the exposure maths in risk_metrics(). BUMP THIS whenever the trading
# formula changes: it is stamped on every logged row, so rows produced under
# different formulas never get averaged together by accident.
#   ritc_x1 - base script. RITC weighted x1; scored a hedged book as net 25k.
#   ritc_x2 - RITC weighted x2 (1 BULL + 1 BEAR); matches server. Fixed 0.07
#             threshold, which sat below the ~0.129 break-even.
#   ritc_x2_adaptive - threshold from fees + slippage quantile. Ratcheted:
#             16 of 31 heats ended up taking no trades at all.
#   netbar_fxhedge - bar fitted on net outcome; residual USD flattened each
#             cycle; tender accept fixed (see VERSIONS.md).
#   v5_depthunwind - current. Unwind chunks sized to visible book depth and
#             every unwind fill logged, to measure what the unwind costs.
#   v10_revert - v7 unwind rate and bar restored; per-tender realised P&L logged.
#   v11_hedgesafe - unwind targets tender inventory only, never the arb's ETF
#             hedge leg.
#   v12_passiveunwind - v11 plus a passive layer: the unwind rests limit orders
#             at the touch for the 0.01 rebate and only crosses when it is
#             behind the glide path to flat.
#   v13_prehedge - a small basket is put on to buy net room for tenders too
#             large to fit the x2 net cap even on a flat book.
#   v14_holdout - tender inventory taken late in the heat is held for
#             close-out rather than unwound, because close-out is free against
#             the mid and the capacity it buys is worth almost nothing by then.
#   v15_exitbasis - the tender gate prices the exit the tender will actually
#             take: close-out at the mid past the hold threshold, a market
#             unwind before it.
#   v16_trackersync - an accept updates the cycle's position read, and the
#             tender tracker is reconciled to the live position so it cannot
#             ratchet into reaching for the arb's hedge leg.
#   v17_holdtick - hold threshold moved 200 -> 175, chosen by worst-case
#             regret once only same-direction tenders are counted as blocked.
RISK_MODEL = "v17_holdtick"
RISK_MODEL_DESC = "v16 plus: hold threshold 175, picked for robustness not the peak"

LOOP_SLEEP = 0.5    # between cycles inside a heat (~8 requests per cycle)
WAIT_POLL = 2.0     # between polls while waiting for the next heat

LOG_DIR = Path(__file__).resolve().parent / "logs"
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")


def now_iso():
    return datetime.now().isoformat(timespec="milliseconds")


# ============================================================================
# LOGGING
# ============================================================================

# Prices seen at the moment a decision was made. Repeated into trades.csv so a
# trade row is self-contained for expected-vs-realised analysis.
SNAP_FIELDS = [
    "bull_bid", "bull_ask", "bear_bid", "bear_ask",
    "ritc_bid_usd", "ritc_ask_usd", "usd_bid", "usd_ask",
    "ritc_bid_cad", "ritc_ask_cad", "edge1", "edge2",
]

TICK_FIELDS = ["run_id", "risk_model", "heat", "tick", "ts"] + SNAP_FIELDS + [
    "nlv", "pos_bull", "pos_bear", "pos_ritc", "pos_usd",
    "gross", "net", "within_limits", "traded", "direction",
    "req_basket_rich", "req_etf_rich", "fx_hedge_action", "fx_hedge_qty",
    "unwind_action", "unwind_qty",
]

TRADE_FIELDS = [
    "run_id", "risk_model", "heat", "tick", "ts", "direction", "edge_at_decision", "leg",
    "ticker", "action", "qty", "ok", "order_id", "quantity_filled", "vwap",
    "order_status", "exec_style", "limit_price",
] + SNAP_FIELDS

TENDER_FIELDS = [
    "run_id", "risk_model", "heat", "tick", "ts", "tender_id", "ticker", "action",
    "quantity", "price", "is_fixed_bid", "expires",
    "caption", "mkt_bid", "mkt_ask", "chunk_vwap", "depth_visible",
    "usd_bid", "usd_ask",
    "edge_vs_mkt", "safe_ok", "safe_reason", "prehedge",
    "accepted", "accept_ok", "accept_err",
]

HEAT_FIELDS = [
    "run_id", "risk_model", "heat", "start_ts", "end_ts", "first_tick", "last_tick",
    "cycles", "arbs_fired", "tenders_seen", "tenders_accepted",
    "nlv_start", "nlv_end", "heat_pnl",
    "base_basket_rich", "base_etf_rich", "margin_basket_rich", "margin_etf_rich",
    "unwind_passive", "unwind_aggressive", "passive_share", "fee_saved",
]


CALIB_FIELDS = [
    "run_id", "risk_model", "heat", "ts", "direction", "n_total",
    "slip", "slip_src", "margin", "action", "base",
]

RUN_FIELDS = [
    "run_id", "ts", "risk_model", "risk_model_desc", "arb_threshold_cad",
    "order_qty", "max_long_net", "max_short_net", "max_gross", "loop_sleep",
]


class CsvLog:
    """Append-only CSV that flushes every row, so a crash or CTRL+C keeps data."""

    def __init__(self, path, fields):
        if path.exists():
            with path.open(newline="") as fh:
                header = next(csv.reader(fh), [])
            if header and header != fields:
                archive = path.with_name(f"{path.stem}.{RUN_ID}.old.csv")
                path.rename(archive)
                print(f"  schema changed - archived previous {path.name} -> {archive.name}")
        is_new = not path.exists()
        self.fh = path.open("a", newline="")
        self.writer = csv.DictWriter(self.fh, fieldnames=fields, extrasaction="ignore")
        if is_new:
            self.writer.writeheader()
            self.fh.flush()

    def write(self, row):
        self.writer.writerow(row)
        self.fh.flush()

    def close(self):
        self.fh.close()


class Logs:
    def __init__(self):
        LOG_DIR.mkdir(exist_ok=True)
        self.ticks = CsvLog(LOG_DIR / "ticks.csv", TICK_FIELDS)
        self.trades = CsvLog(LOG_DIR / "trades.csv", TRADE_FIELDS)
        self.tenders = CsvLog(LOG_DIR / "tenders.csv", TENDER_FIELDS)
        self.heats = CsvLog(LOG_DIR / "heats.csv", HEAT_FIELDS)
        self.calib = CsvLog(LOG_DIR / "calibration.csv", CALIB_FIELDS)
        self.runs = CsvLog(LOG_DIR / "runs.csv", RUN_FIELDS)
        self.runs.write({
            "run_id": RUN_ID, "ts": now_iso(),
            "risk_model": RISK_MODEL, "risk_model_desc": RISK_MODEL_DESC,
            "arb_threshold_cad": ARB_THRESHOLD_CAD, "order_qty": ORDER_QTY,
            "max_long_net": MAX_LONG_NET, "max_short_net": MAX_SHORT_NET,
            "max_gross": MAX_GROSS, "loop_sleep": LOOP_SLEEP,
        })

    def close(self):
        for log in (self.ticks, self.trades, self.tenders, self.heats,
                    self.calib, self.runs):
            log.close()


# ============================================================================
# HELPERS
# ============================================================================

def get_tick_status(session):
    j = api_request(session, 'GET', 'case')
    if j is None:
        return None, None
    return j["tick"], j["status"]


def get_nlv(session):
    j = api_request(session, 'GET', 'trader')
    return float(j["nlv"]) if j else None


def fetch_book(session, ticker):
    """Full order book. Fetched once per cycle and reused for both pricing and
    unwind sizing, so depth awareness costs no extra requests."""
    book = api_request(session, 'GET', 'securities/book', params={"ticker": ticker})
    return book or {"bids": [], "asks": []}


def top_of_book(book):
    # bids come back descending and asks ascending, so [0] is best on each side.
    bid = float(book["bids"][0]["price"]) if book.get("bids") else 0.0
    ask = float(book["asks"][0]["price"]) if book.get("asks") else 1e12
    return bid, ask


def vwap_for_qty(levels, qty):
    """Volume-weighted price of taking `qty` through these levels.

    Returns (vwap, filled); filled < qty means the visible book ran out.
    """
    filled = cost = 0.0
    for lvl in levels:
        take = min(qty - filled, float(lvl.get("quantity", 0) or 0))
        if take <= 0:
            continue
        cost += take * float(lvl.get("price", 0) or 0)
        filled += take
        if filled >= qty:
            break
    return (cost / filled if filled else None), filled


def depth_within(book, side, levels=DEPTH_LEVELS):
    """Size resting in the top `levels` of one side.

    Unwinding a tender means pushing ~90k units through a book whose top level
    holds only a few thousand, so a flat 10k chunk walks several levels every
    time. Sizing to what is actually there is what keeps the unwind from paying
    for its own impact.
    """
    rows = book.get("bids" if side == "bid" else "asks") or []
    return sum(float(r.get("quantity", 0)) for r in rows[:levels])


def best_bid_ask(session, ticker):
    return top_of_book(fetch_book(session, ticker))


def positions_map(session):
    data = api_request(session, 'GET', 'securities')
    if data is None:
        return {k: 0 for k in (BULL, BEAR, RITC, USD, CAD)}, {}
    out = {p["ticker"]: int(p.get("position", 0)) for p in data}
    quotes = {p["ticker"]: (p.get("bid"), p.get("ask")) for p in data}
    for k in (BULL, BEAR, RITC, USD, CAD):
        out.setdefault(k, 0)
    return out, quotes


def place_mkt(session, ticker, action, qty):
    # Returns the full order JSON (not just a bool) so the fill can be logged.
    return api_request(session, 'POST', 'orders',
                       params={"ticker": ticker, "type": "MARKET",
                               "quantity": int(qty), "action": action})


def place_lmt(session, ticker, action, qty, price):
    """Post a passive limit order. `price` is required when type is LIMIT."""
    return api_request(session, 'POST', 'orders',
                       params={"ticker": ticker, "type": "LIMIT",
                               "quantity": int(qty), "action": action,
                               "price": round(float(price), 2)})


def open_orders(session):
    """Currently resting orders. GET /orders defaults to status=OPEN."""
    return api_request(session, 'GET', 'orders', params={"status": "OPEN"}) or []


def get_order(session, order_id):
    return api_request(session, 'GET', f"orders/{order_id}")


def cancel_orders(session, ids):
    """Cancel a set of orders in ONE request.

    POST /commands/cancel takes a comma-separated `ids` list, so a reprice costs
    one cancel plus one insert regardless of how many orders are being pulled.
    """
    if not ids:
        return
    api_request(session, 'POST', 'commands/cancel',
                params={"ids": ",".join(str(i) for i in ids)})


class Resting:
    """Our live passive orders, and the fills they produce.

    A limit order fills in pieces over several cycles, so the fill has to be
    tracked rather than read off the POST response the way a market order's is.
    Each cycle the open list is re-read and `quantity_filled` compared against
    what was last seen; the difference is the new fill. An order that has left
    the open list is resolved with a single GET so its final fill is never lost
    - which matters because an order that transacts completely disappears in
    exactly the same way as one that was cancelled.
    """

    def __init__(self):
        self.orders = {}   # order_id -> {ticker, action, price, qty, filled, tick}

    def add(self, resp, tick):
        oid = resp.get("order_id")
        if oid is None:
            return None
        self.orders[oid] = {
            "order_id": oid,
            "ticker": resp.get("ticker"), "action": resp.get("action"),
            "price": float(resp.get("price") or 0),
            "qty": float(resp.get("quantity") or 0),
            "filled": float(resp.get("quantity_filled") or 0),
            "tick": tick,
        }
        return oid

    def ids_for(self, ticker):
        return [oid for oid, o in self.orders.items() if o["ticker"] == ticker]

    def live_qty(self, ticker, action):
        """Unfilled size still working, so the next order is not a duplicate."""
        return sum(max(o["qty"] - o["filled"], 0) for o in self.orders.values()
                   if o["ticker"] == ticker and o["action"] == action)

    def prices(self, ticker, action):
        return [o["price"] for o in self.orders.values()
                if o["ticker"] == ticker and o["action"] == action]

    def reconcile(self, session):
        """Return [(order, newly_filled, vwap)] and drop anything finished.

        Called once per cycle before any new order is placed, so decisions are
        made against the true remaining size rather than the position alone -
        the position does not move until a resting order actually fills, and
        re-posting against it would stack duplicate size on the book.
        """
        try:
            live = {int(o["order_id"]): o for o in open_orders(session)}
        except ApiException as e:
            print(f"  open-order poll failed: {e}")
            return []

        events = []
        for oid in list(self.orders):
            tracked = self.orders[oid]
            snapshot = live.get(oid)
            if snapshot is None:
                # Gone from the open list: transacted in full, or cancelled.
                # One GET settles which, and carries the final fill and vwap.
                try:
                    snapshot = get_order(session, oid)
                except ApiException as e:
                    print(f"  order {oid} lookup failed: {e}")
                    snapshot = None
                done = True
            else:
                done = False
            if snapshot:
                filled = float(snapshot.get("quantity_filled") or 0)
                new = filled - tracked["filled"]
                if new > 0:
                    events.append((dict(tracked), new,
                                   snapshot.get("vwap") or tracked["price"]))
                tracked["filled"] = filled
            if done:
                self.orders.pop(oid, None)
        return events

    def cancel_all(self, session, ticker=None):
        ids = self.ids_for(ticker) if ticker else list(self.orders)
        cancel_orders(session, ids)
        for oid in ids:
            self.orders.pop(oid, None)
        return len(ids)


def hedge_fx(session, pos):
    """Flatten leftover USD so P&L reflects the arb, not currency drift.

    The ETF leg settles in USD, so every arb leaves a currency position behind.
    Left alone it dominated results: mean |USD| 91,966 and peaks over 1.6M
    against ~30k of arb earnings, with NLV swinging 72k inside a single run -
    roughly 2.4x the entire arb signal, which made heat P&L useless as feedback.

    USD carries zero commission here, so flattening costs only the ~0.0002
    spread. Deliberately self-correcting rather than hedging each fill: it
    works off the position actually held, so drift from any source (tenders
    included) gets cleaned up on the following cycle.
    """
    usd = int(pos.get(USD, 0))
    if abs(usd) < FX_HEDGE_TOLERANCE:
        return None, 0
    qty = min(abs(usd), MAX_SIZE_FX)
    action = "SELL" if usd > 0 else "BUY"
    place_mkt(session, USD, action, qty)
    return action, qty


class UnwindState:
    """Per-heat state for the unwind: resting orders, the clock, and the tally.

    The clock is what makes passive execution safe. Resting an order is free
    only if the position still clears before the next tender arrives; past that
    point a cheap fill is worth less than the ~26,000 USD/heat of tenders that
    get refused on the net limit while the book is still full.
    """

    def __init__(self):
        self.resting = Resting()
        self.opened = None      # cycle the current position was taken on
        self.peak = 0           # size to glide down from
        self.passive_qty = 0.0
        self.aggressive_qty = 0.0
        self.holding = False    # latched so the hold prints once, not every cycle

    def reset(self):
        self.opened = None
        self.peak = 0
        self.holding = False

    def note_size(self, size, cycle):
        """Restart the glide path when a new tender lands on top of the old one."""
        if self.opened is None or size > self.peak + UNWIND_CHUNK:
            self.opened = cycle
            self.peak = size
        self.peak = max(self.peak, size)

    def behind(self, size, cycle):
        """Units we are behind a linear glide from `peak` to flat.

        Linear rather than front- or back-loaded: the passive layer fills at an
        unpredictable rate, and a straight line is the schedule that asks the
        aggressive layer for the least total liquidity while still finishing on
        time.
        """
        # `is None`, not `or`: cycle 0 is a real cycle, and `self.opened or
        # cycle` reads a legitimate 0 as unset, which pins elapsed at 0 forever
        # and silently disables the deadline.
        opened = self.opened if self.opened is not None else cycle
        elapsed = max(cycle - opened, 0)
        left = max(UNWIND_DEADLINE_CYCLES - elapsed, 0)
        allowed = self.peak * (left / UNWIND_DEADLINE_CYCLES)
        return size - allowed


def passive_price(action, bid, ask):
    """Best price that still rests rather than crossing.

    A sell one tick above the bid becomes the best offer, since the median RITC
    spread is 0.08. When the spread is a single tick that is not possible, so
    the order joins the existing offer instead - still passive, still rebated,
    just behind a queue.
    """
    if action == "SELL":
        px = bid + PRICE_TICK
        return round(ask if px >= ask else px, 2)
    px = ask - PRICE_TICK
    return round(bid if px <= bid else px, 2)


def unwind_ritc(session, pos, book, snap, logs, heat, tick, cycle, st):
    """Work TENDER inventory back toward flat, passively first.

    A tender leaves ~91k units of ETF on the book, which is 182k of the 300k
    gross limit. A full basket hedge would add another 182k and breach that
    cap - only the small `prehedge_for_tender` slice fits - and the converters
    that would collapse the position are human-only per the case rules. So it
    is sold back into the market, or held (below).

    It targets `tender_position`, never the raw RITC position: the arb's ETF
    leg is a hedge against its own BULL/BEAR, and selling it off turns a
    net-neutral arb into a naked short basket (v11).

    Three exits, cheapest first:

    HOLD, past HOLD_TO_CLOSEOUT_TICK. Close-out is free against the mid and a
    tender that late blocks almost nothing, so nothing is traded at all (v14).

    PASSIVE rests one order at the touch. It earns the 0.01/share rebate
    instead of paying the 0.02 fee, sells at bid+0.01 instead of at the bid,
    and - the part v10 showed matters most - takes no liquidity at all, so it
    cannot drag the quote the way 92,000 units of market orders did.

    AGGRESSIVE is v11's market chunker, unchanged, but it only fires when the
    position is behind the glide path. That keeps the deadline that stops a
    slow unwind blocking the next tender, while paying the spread only on the
    units the passive layer could not clear in time.

    Worst case - nothing ever fills passively - every unit goes through the
    aggressive layer and the behaviour degrades exactly to v11.
    """
    global tender_position
    # Passive fills are booked before anything is decided, so `tender_position`
    # and the glide path both see them. A resting order fills between cycles
    # without passing through this function, so it is the only inventory change
    # the unwind does not make itself.
    for order, filled, vwap in st.resting.reconcile(session):
        st.passive_qty += filled
        tender_position -= filled if order["action"] == "SELL" else -filled
        _log_fill(logs, heat, tick, order["action"], filled, vwap,
                  snap, "passive", order["price"], order.get("order_id"))
        print(f"  PASSIVE fill {order['action']} {filled:,.0f} RITC @ {vwap} "
              f"(rested {order['price']:.2f}, rebate +{filled * REBATE_LMT:,.0f})")

    # Only tender-derived inventory is unwound. Clamp to the live position so a
    # stale tracker can never drive trades in the wrong direction.
    ritc = int(pos.get(RITC, 0))
    target = tender_position
    if target > 0:
        target = min(target, max(ritc, 0))
    elif target < 0:
        target = max(target, min(ritc, 0))
    # Believe the position, not the tracker. If the clamp bound, RITC moved for
    # a reason the tracker never saw - the arb's own ETF leg is the only one -
    # and the inventory it still claims is gone. Left uncorrected the tracker
    # ratchets: a later arb SELL would take RITC negative again and the stale
    # claim would let the unwind buy back the arb's hedge leg, which is exactly
    # the v11 bug. Safe only because `handle_tenders` now folds an accept into
    # `pos` in the same cycle; before that, `pos` was 90,000 units stale on the
    # accept cycle and this would have zeroed a tender we had just taken on.
    if target != tender_position:
        tender_position = target
    if abs(target) <= UNWIND_THRESHOLD:
        if st.resting.orders:
            st.resting.cancel_all(session, RITC)
        st.reset()
        return None, 0

    # Too late in the heat for the capacity to be worth anything: hold the
    # position and let close-out price it, which costs nothing against the mid.
    if tick >= HOLD_TO_CLOSEOUT_TICK:
        if st.resting.orders:
            # A resting fill is cheaper than crossing but still worse than
            # close-out, so once we have decided to hold, stop offering.
            st.resting.cancel_all(session, RITC)
        if not st.holding:
            st.holding = True
            print(f"  HOLD {target:+,} RITC to close-out (tick {tick} "
                  f">= {HOLD_TO_CLOSEOUT_TICK}) - saves ~"
                  f"{abs(target) * (0.055 + FEE_MKT):,.0f} of execution")
        return None, 0
    st.holding = False

    action = "SELL" if target > 0 else "BUY"
    # A resting order on the wrong side would build the position, not clear it.
    if st.resting.live_qty(RITC, "BUY" if action == "SELL" else "SELL"):
        st.resting.cancel_all(session, RITC)

    size = abs(target) - UNWIND_THRESHOLD
    st.note_size(size, cycle)
    ref_bid, ref_ask = top_of_book(book)
    ref = ref_bid if action == "SELL" else ref_ask
    avail = depth_within(book, "bid" if action == "SELL" else "ask")

    remaining = size
    sent = 0
    behind = st.behind(size, cycle) if PASSIVE_UNWIND else remaining
    if behind > 0:
        # Crossing is the decision; a resting order on the same side would
        # double-count the size we are about to take, so pull it first.
        st.resting.cancel_all(session, RITC)
        for _ in range(UNWIND_CHUNKS_PER_CYCLE):
            if remaining <= 0 or behind <= 0:
                break
            # Take at most half of what is visible, so the chunk scales down on a
            # thin book. `avail` is decremented as we go: the book is only fetched
            # once per cycle, so later chunks must not re-spend the same depth.
            qty = int(min(remaining, behind, UNWIND_CHUNK, MAX_SIZE_EQUITY,
                          max(avail * DEPTH_FRACTION, 1000)))
            if qty <= 0:
                break
            try:
                resp = place_mkt(session, RITC, action, qty)
            except ApiException as e:
                print(f"  unwind failed: {e}")
                break
            slip = _log_unwind(logs, heat, tick, action, qty, resp, ref, snap, ritc, avail)
            remaining -= qty
            behind -= qty
            avail = max(avail - qty, 0)
            sent += qty
            st.aggressive_qty += qty
            tender_position -= qty if action == "SELL" else -qty
            if slip is not None and slip > MAX_UNWIND_SLIP:
                print(f"    slip {slip:+.4f} > {MAX_UNWIND_SLIP} - holding the rest for next cycle")
                break

    if PASSIVE_UNWIND:
        _rest_passive(session, logs, heat, tick, action, remaining,
                      ref_bid, ref_ask, snap, st)

    return (action, sent) if sent else (None, 0)


def _rest_passive(session, logs, heat, tick, action, remaining, bid, ask, snap, st):
    """Keep one order resting at the touch, repricing it when the touch moves."""
    if bid <= 0 or ask >= 1e12:
        return
    target = passive_price(action, bid, ask)

    # Repricing costs a cancel plus an insert and gives up queue position, so it
    # is only worth doing once the touch has actually moved away from us.
    stale = [oid for oid, o in st.resting.orders.items()
             if o["ticker"] == RITC
             and abs(o["price"] - target) >= PASSIVE_REPRICE_TICKS * PRICE_TICK - 1e-9]
    if stale:
        cancel_orders(session, stale)
        for oid in stale:
            st.resting.orders.pop(oid, None)

    live = st.resting.live_qty(RITC, action)
    if len(st.resting.ids_for(RITC)) >= PASSIVE_MAX_LIVE:
        return
    # `remaining` already excludes what the aggressive layer just sold, and
    # `live` excludes what is still working, so the two layers cannot between
    # them sell more RITC than we actually hold.
    room = int(min(remaining - live, PASSIVE_CHUNK, MAX_SIZE_EQUITY))
    if room < 1000:
        return
    try:
        resp = place_lmt(session, RITC, action, room, target)
    except ApiException as e:
        print(f"  passive rest failed: {e}")
        return
    if resp:
        st.resting.add(resp, tick)
        print(f"  REST {action} {room:,} RITC @ {target:.2f} "
              f"(bid {bid:.2f} / ask {ask:.2f})")


def _log_fill(logs, heat, tick, action, qty, vwap, snap, style, limit_px, order_id):
    """One passive fill. Written with direction='unwind' so the realised-cost
    analysis sees passive and aggressive fills in the same unwind VWAP."""
    row = {
        "run_id": RUN_ID, "risk_model": RISK_MODEL, "heat": heat, "tick": tick,
        "ts": now_iso(), "direction": "unwind", "edge_at_decision": "",
        "leg": 0, "ticker": RITC, "action": action, "qty": qty, "ok": True,
        "order_id": order_id, "quantity_filled": qty, "vwap": vwap,
        "order_status": "TRANSACTED", "exec_style": style, "limit_price": limit_px,
    }
    row.update(snap)
    logs.trades.write(row)


def _log_unwind(logs, heat, tick, action, qty, resp, ref, snap, ritc, avail):
    row = {
        "run_id": RUN_ID, "risk_model": RISK_MODEL, "heat": heat, "tick": tick,
        "ts": now_iso(), "direction": "unwind", "edge_at_decision": "",
        "leg": 0, "ticker": RITC, "action": action, "qty": qty,
        "ok": resp is not None, "exec_style": "aggressive", "limit_price": "",
    }
    slip = None
    if resp:
        row["order_id"] = resp.get("order_id")
        row["quantity_filled"] = resp.get("quantity_filled")
        row["vwap"] = resp.get("vwap")
        row["order_status"] = resp.get("status")
        if resp.get("vwap") and ref:
            # Positive = worse than the quote we saw, i.e. what the unwind cost.
            slip = (ref - resp["vwap"]) if action == "SELL" else (resp["vwap"] - ref)
    row.update(snap)
    logs.trades.write(row)
    print(f"  UNWIND {action} {qty:,} RITC (pos {ritc:+,}, depth {avail:,.0f}"
          f"{'' if slip is None else f', slip {slip:+.4f}'})")
    return slip


def risk_metrics(pos):
    """Exposure in underlying-share units, matching the server's own arithmetic.

    One RITC is one BULL plus one BEAR, so it carries two shares of underlying
    and must be weighted x2. The base script weighted it x1, which scored a
    fully hedged arb book (+25k BULL, +25k BEAR, -25k RITC) as net +25k and
    froze trading, while the server's LIMIT-STOCK read net 0 / gross 100k on
    the same position.
    """
    gross = abs(pos[BULL]) + abs(pos[BEAR]) + 2 * abs(pos[RITC])
    net = pos[BULL] + pos[BEAR] + 2 * pos[RITC]
    ok = (gross < MAX_GROSS) and (MAX_SHORT_NET < net < MAX_LONG_NET)
    return gross, net, ok


# ============================================================================
# TENDERS
# ============================================================================

def tender_safety(offer, pos, book, usd_bid, usd_ask, tick=0):
    """Minimum bar before accepting a tender: beats the market we must unwind
    into, and fits inside the server's position limits.

    This is NOT the profitability model - that needs logged tender history,
    which until now did not exist because accepts were crashing before the log
    write. It only rules out tenders that are plainly loss-making or that would
    breach limits and draw 0.5/unit fines. Tenders here run 90k-99k units of
    RITC, which at the ETF's x2 multiplier is 180k-198k against a 300k gross
    and 200k net server limit, so an unchecked accept is not survivable.
    """
    qty = float(offer.get("quantity") or 0)
    price = offer.get("price")
    action = (offer.get("action") or "").upper()
    if not qty or price is None:
        return False, "missing quantity/price", None

    # Price the unwind at the VWAP of ONE chunk, not top-of-book.
    #
    # Top-of-book flattered the edge: the RITC book holds ~190k visible but only
    # ~5-10k at the touch, so a 90k tender priced at the touch overstates the
    # edge by ~0.26/unit (~23k USD). Full-size VWAP is the opposite error - the
    # unwind goes out as ~10k chunks over many cycles and the book refills
    # between them, so a single sweep never happens. Chunk VWAP is what each
    # order actually experiences, and it matches the ~18% leak measured on the
    # first captured tenders.
    if action not in ("SELL", "BUY"):
        return False, f"unknown action {action}", None

    # Which exit this tender is actually going to take. Past
    # HOLD_TO_CLOSEOUT_TICK the unwind does not run (see v14), so pricing the
    # tender against a market unwind values an exit that will never happen -
    # and values it ~0.063/unit too low, measured across 385 logged tenders.
    if tick >= HOLD_TO_CLOSEOUT_TICK:
        bid, ask = top_of_book(book)
        if bid <= 0 or ask >= 1e12:
            return False, "no book depth", None
        # Close-out is at the last traded price, charges no commission and has
        # no impact, so the mid is the honest reference and no fee comes off.
        mid = (bid + ask) / 2.0
        edge = (price - mid) if action == "SELL" else (mid - price)
        # A wider bar than the unwind case despite the cheaper exit. The risk
        # here is not execution, it is that close-out lands on the wrong side
        # of the book rather than at the mid; one half-spread (median RITC
        # spread 0.08) covers that, and drift to the close is mean-zero.
        bar, basis = MIN_TENDER_EDGE_HELD, "closeout"
    else:
        if action == "SELL":      # we sell to them, buy back through the asks
            ref, _ = vwap_for_qty(book.get("asks") or [], UNWIND_CHUNK)
            edge = (price - ref) if ref else None
        else:                     # we buy from them, sell out through the bids
            ref, _ = vwap_for_qty(book.get("bids") or [], UNWIND_CHUNK)
            edge = (ref - price) if ref else None
        if edge is None:
            return False, "no book depth", None
        # Unwind pays the market fee per unit; tender itself is fee-free.
        edge -= FEE_MKT
        bar, basis = MIN_TENDER_EDGE, "unwind"

    if edge < bar - EDGE_EPSILON:
        return False, f"edge {edge:+.5f} < {bar} ({basis})", edge

    # Projected exposure in underlying units (ETF counts double).
    signed = -qty if action == "SELL" else qty
    proj_net = pos[BULL] + pos[BEAR] + 2 * (pos[RITC] + signed)
    proj_gross = abs(pos[BULL]) + abs(pos[BEAR]) + 2 * abs(pos[RITC] + signed)
    if proj_gross > SERVER_GROSS_LIMIT - LIMIT_BUFFER:
        return False, f"gross would hit {proj_gross:,.0f}", edge
    if abs(proj_net) > SERVER_NET_LIMIT - LIMIT_BUFFER:
        return False, f"net would hit {proj_net:,.0f}", edge
    return True, "ok", edge


def net_shortfall(offer, pos):
    """Net room, in exposure units, that this tender is short of. 0 if it fits."""
    qty = float(offer.get("quantity") or 0)
    action = (offer.get("action") or "").upper()
    signed = -qty if action == "SELL" else qty
    proj_net = pos[BULL] + pos[BEAR] + 2 * (pos[RITC] + signed)
    return max(abs(proj_net) - (SERVER_NET_LIMIT - LIMIT_BUFFER), 0.0)


def prehedge_for_tender(session, offer, pos, logs, heat, tick, snap):
    """Put on a small basket so an oversized tender fits inside the net limit.

    A BUY tender pushes net up, so the hedge sells BULL and BEAR; a SELL tender
    does the reverse. `need / 2` of each side buys `need` of room, because the
    two stocks together carry one ETF's exposure at half its multiplier.

    Returns units hedged per side, and updates `pos` in place so the caller's
    re-check sees the new book rather than the stale one.
    """
    global prehedge_position
    need = net_shortfall(offer, pos)
    if need <= 0:
        return 0
    per_side = int(-(-need // 2))          # ceil, so we clear the bar not tie it
    if per_side > MAX_PREHEDGE:
        return 0
    # Several tenders can be live in the same cycle and each would size its
    # hedge against the same book. Cap the running total, not just the single
    # order, so they cannot stack into a position of their own.
    if abs(prehedge_position + (per_side if need else 0)) > MAX_PREHEDGE:
        print(f"    prehedge skipped: {abs(prehedge_position):,} already on")
        return 0

    # The hedge itself consumes gross. Check the whole projected book, not just
    # the hedge, or we trade room on one limit for a breach of the other.
    qty = float(offer.get("quantity") or 0)
    signed = -qty if (offer.get("action") or "").upper() == "SELL" else qty
    action = "SELL" if signed > 0 else "BUY"
    step = -per_side if action == "SELL" else per_side
    proj_gross = (abs(pos[BULL] + step) + abs(pos[BEAR] + step)
                  + 2 * abs(pos[RITC] + signed))
    if proj_gross > SERVER_GROSS_LIMIT - LIMIT_BUFFER:
        print(f"    prehedge skipped: gross would hit {proj_gross:,.0f}")
        return 0

    for ticker in (BULL, BEAR):
        try:
            resp = place_mkt(session, ticker, action, per_side)
        except ApiException as e:
            print(f"    prehedge {ticker} failed: {e}")
            return 0
        row = {
            "run_id": RUN_ID, "risk_model": RISK_MODEL, "heat": heat,
            "tick": tick, "ts": now_iso(), "direction": "prehedge",
            "edge_at_decision": "", "leg": 0, "ticker": ticker,
            "action": action, "qty": per_side, "ok": resp is not None,
            "exec_style": "aggressive", "limit_price": "",
        }
        if resp:
            row.update({"order_id": resp.get("order_id"),
                        "quantity_filled": resp.get("quantity_filled"),
                        "vwap": resp.get("vwap"),
                        "order_status": resp.get("status")})
        row.update(snap)
        logs.trades.write(row)
        pos[ticker] = pos.get(ticker, 0) + step

    prehedge_position += step
    print(f"    PREHEDGE {action} {per_side:,} BULL + BEAR "
          f"(buys {per_side * 2:,} of net room, fee ~{per_side * 2 * FEE_MKT:,.0f})")
    return per_side


def unwind_prehedge(session, pos, logs, heat, tick, snap):
    """Take the pre-hedge off once the tender it was carrying is gone.

    Held for the whole life of the tender position rather than unwound
    alongside it: short basket against long ETF is what makes the exposure
    smaller while the risky leg is still on the book. It only becomes a naked
    directional position once the ETF has cleared, which is when this runs.
    """
    global prehedge_position
    if not prehedge_position or abs(tender_position) > UNWIND_THRESHOLD:
        return 0
    action = "SELL" if prehedge_position > 0 else "BUY"
    qty = min(abs(prehedge_position), MAX_SIZE_EQUITY)
    for ticker in (BULL, BEAR):
        try:
            resp = place_mkt(session, ticker, action, qty)
        except ApiException as e:
            print(f"  prehedge unwind {ticker} failed: {e}")
            return 0
        row = {
            "run_id": RUN_ID, "risk_model": RISK_MODEL, "heat": heat,
            "tick": tick, "ts": now_iso(), "direction": "prehedge_unwind",
            "edge_at_decision": "", "leg": 0, "ticker": ticker,
            "action": action, "qty": qty, "ok": resp is not None,
            "exec_style": "aggressive", "limit_price": "",
        }
        if resp:
            row.update({"order_id": resp.get("order_id"),
                        "quantity_filled": resp.get("quantity_filled"),
                        "vwap": resp.get("vwap"),
                        "order_status": resp.get("status")})
        row.update(snap)
        logs.trades.write(row)
    prehedge_position -= qty if action == "SELL" else -qty
    print(f"  PREHEDGE UNWIND {action} {qty:,} BULL + BEAR")
    return qty


def handle_tenders(session, logs, heat, tick, snap, quotes, pos, books):
    """Log every active tender, then decide whether to accept it.

    Ordering is the whole point here. The previous version attempted the accept
    POST first and wrote the log afterwards; the server answers these with HTTP
    500 ("Value cannot be null"), the ApiException unwound the entire cycle, and
    the tender was never recorded. That is why 32 heats logged zero tenders
    while tenders were in fact arriving in every heat. The row is now written
    before anything can fail, and the accept is isolated so a failure costs one
    tender rather than the cycle.
    """
    try:
        offers = api_request(session, 'GET', 'tenders') or []
    except ApiException as e:
        print(f"  tender poll failed: {e}")
        return 0, 0

    seen = accepted = 0
    for offer in offers:
        seen += 1
        tid = offer.get('tender_id')
        ticker = offer.get('ticker')
        mkt_bid, mkt_ask = quotes.get(ticker, (None, None))
        book = books.get(ticker, {"bids": [], "asks": []})
        safe, reason, edge = tender_safety(
            offer, pos, book, snap["usd_bid"], snap["usd_ask"], tick)
        # Only the net check is worth buying our way past. A gross breach means
        # the book is genuinely full; a net breach on a tender this size is
        # usually just the x2 multiplier, and a few thousand shares of basket
        # fixes it. The edge gate is never overridden.
        hedged = 0
        if PREHEDGE and not safe and reason.startswith("net would hit"):
            hedged = prehedge_for_tender(session, offer, pos, logs, heat, tick, snap)
            if hedged:
                safe, reason, edge = tender_safety(
                    offer, pos, book, snap["usd_bid"], snap["usd_ask"], tick)
                reason = f"{reason} after prehedge {hedged:,}"
        side = "asks" if (offer.get('action') or '').upper() == "SELL" else "bids"
        chunk_ref, _ = vwap_for_qty(book.get(side) or [], UNWIND_CHUNK)
        visible = sum(float(r.get("quantity", 0) or 0) for r in (book.get(side) or []))

        row = {
            "run_id": RUN_ID, "risk_model": RISK_MODEL, "heat": heat,
            "tick": tick, "ts": now_iso(), "tender_id": tid, "ticker": ticker,
            "action": offer.get('action'), "quantity": offer.get('quantity'),
            "price": offer.get('price'), "is_fixed_bid": offer.get('is_fixed_bid'),
            "expires": offer.get('expires'), "caption": offer.get('caption'),
            "mkt_bid": mkt_bid, "mkt_ask": mkt_ask,
            "chunk_vwap": round(chunk_ref, 4) if chunk_ref else "",
            "depth_visible": round(visible),
            "usd_bid": snap["usd_bid"], "usd_ask": snap["usd_ask"],
            "edge_vs_mkt": round(edge, 6) if edge is not None else "",
            "safe_ok": safe, "safe_reason": reason, "prehedge": hedged,
        }
        # Written BEFORE any accept attempt - see docstring.
        logs.tenders.write(row)
        edge_str = "n/a" if edge is None else f"{edge:+.5f}"
        print(f"  TENDER {ticker} {offer.get('action')} {offer.get('quantity'):,.0f} "
              f"@ {offer.get('price')} edge={edge_str} "
              f"(~{0 if edge is None else edge * float(offer.get('quantity') or 0):,.0f} USD) "
              f"-> {'ACCEPT' if safe else 'SKIP: ' + reason}")

        if not safe:
            continue
        try:
            # `price` is REQUIRED on every accept, fixed-bid included, and for a
            # fixed-bid tender it must equal the tender price exactly (per the
            # DMA swagger for POST /tenders/{id}). The Rotman base template
            # omits it precisely when is_fixed_bid is true, so every accept came
            # back HTTP 500 "Value cannot be null. Parameter name: String".
            resp = api_request(session, 'POST', f"tenders/{tid}",
                               params={"price": offer.get('price')})
            ok = resp is not None
            err = ""
            if ok:
                global tender_position
                q = float(offer.get("quantity") or 0)
                signed = q if (offer.get("action") or "").upper() == "BUY" else -q
                tender_position += signed
                # `pos` was read at the top of the cycle, before this accept, so
                # without this the unwind and the limit checks spend the rest of
                # the cycle looking at a book that is 90,000 units out of date -
                # and `tender_position` would claim inventory the position does
                # not yet show, which is what the clamp in unwind_ritc would
                # then wrongly correct away.
                pos[RITC] = pos.get(RITC, 0) + signed
        except ApiException as e:
            ok, err = False, str(e)[:200]
        accepted += 1 if ok else 0
        row.update({"accepted": True, "accept_ok": ok, "accept_err": err})
        logs.tenders.write(row)
        print(f"    accept -> {ok} {err}")

    return seen, accepted


# ============================================================================
# EXECUTION
# ============================================================================

def execute_arb(session, logs, heat, tick, direction, edge, legs, snap):
    """Fire the three legs of an arb, logging each leg with its actual fill."""
    fills = {}
    for seq, (ticker, action) in enumerate(legs, start=1):
        resp = place_mkt(session, ticker, action, ORDER_QTY)

        row = {
            "run_id": RUN_ID, "risk_model": RISK_MODEL, "heat": heat, "tick": tick, "ts": now_iso(),
            "direction": direction, "edge_at_decision": round(edge, 6),
            "leg": seq, "ticker": ticker, "action": action, "qty": ORDER_QTY,
            "ok": resp is not None,
        }
        if resp:
            row["order_id"] = resp.get("order_id")
            row["quantity_filled"] = resp.get("quantity_filled")
            row["vwap"] = resp.get("vwap")
            row["order_status"] = resp.get("status")
            fills[ticker] = resp.get("vwap")
        row.update(snap)
        logs.trades.write(row)

    # Realised edge from actual fill prices, using the FX quote seen at decision
    # time. Compare against edge_at_decision to measure slippage.
    realised = None
    if all(fills.get(t) is not None for t in (BULL, BEAR, RITC)):
        fx = snap["usd_ask"] if direction == "basket_rich" else snap["usd_bid"]
        ritc_cad = fills[RITC] * fx
        if direction == "basket_rich":
            realised = fills[BULL] + fills[BEAR] - ritc_cad
        else:
            realised = ritc_cad - fills[BULL] - fills[BEAR]

    slip = f"{edge - realised:+.4f}" if realised is not None else "n/a"
    realised_str = f"{realised:.4f}" if realised is not None else "n/a"
    print(f"  ARB {direction} qty={ORDER_QTY} expected={edge:.4f} "
          f"realised={realised_str} slippage={slip}")


# ============================================================================
# CORE LOGIC
# ============================================================================

def step_once(session, logs, heat, tick, bases, cycle, st):
    books = {t: fetch_book(session, t) for t in (BULL, BEAR, RITC, USD)}
    bull_bid, bull_ask = top_of_book(books[BULL])
    bear_bid, bear_ask = top_of_book(books[BEAR])
    ritc_bid_usd, ritc_ask_usd = top_of_book(books[RITC])
    usd_bid, usd_ask = top_of_book(books[USD])

    ritc_bid_cad = ritc_bid_usd * usd_bid
    ritc_ask_cad = ritc_ask_usd * usd_ask

    basket_sell_value = bull_bid + bear_bid
    basket_buy_cost = bull_ask + bear_ask

    # Direction 1: basket rich -> sell BULL & BEAR, buy RITC
    edge1 = basket_sell_value - ritc_ask_cad
    # Direction 2: ETF rich -> buy BULL & BEAR, sell RITC
    edge2 = ritc_bid_cad - basket_buy_cost

    snap = {
        "bull_bid": bull_bid, "bull_ask": bull_ask,
        "bear_bid": bear_bid, "bear_ask": bear_ask,
        "ritc_bid_usd": ritc_bid_usd, "ritc_ask_usd": ritc_ask_usd,
        "usd_bid": usd_bid, "usd_ask": usd_ask,
        "ritc_bid_cad": round(ritc_bid_cad, 6), "ritc_ask_cad": round(ritc_ask_cad, 6),
        "edge1": round(edge1, 6), "edge2": round(edge2, 6),
    }

    pos, quotes = positions_map(session)
    tenders_seen, tenders_accepted = handle_tenders(session, logs, heat, tick, snap, quotes, pos, books)

    gross, net, ok = risk_metrics(pos)
    nlv = get_nlv(session)

    # Bar to clear = fees (FX-dependent) + calibrated slippage allowance.
    req1 = calibration.required_edge(bases["basket_rich"], usd_ask)
    req2 = calibration.required_edge(bases["etf_rich"], usd_bid)

    unwind_action, unwind_qty = unwind_ritc(
        session, pos, books[RITC], snap, logs, heat, tick, cycle, st)
    # After the ETF leg, so the basket comes off only once the position it was
    # hedging is actually gone.
    unwind_prehedge(session, pos, logs, heat, tick, snap)
    fx_action, fx_qty = hedge_fx(session, pos)

    traded = False
    direction = ""
    if edge1 >= req1 and ok:
        direction = "basket_rich"
        execute_arb(session, logs, heat, tick, direction, edge1,
                    [(BULL, "SELL"), (BEAR, "SELL"), (RITC, "BUY")], snap)
        traded = True
    elif edge2 >= req2 and ok:
        direction = "etf_rich"
        execute_arb(session, logs, heat, tick, direction, edge2,
                    [(BULL, "BUY"), (BEAR, "BUY"), (RITC, "SELL")], snap)
        traded = True

    row = {
        "run_id": RUN_ID, "risk_model": RISK_MODEL, "heat": heat, "tick": tick, "ts": now_iso(),
        "nlv": nlv,
        "pos_bull": pos[BULL], "pos_bear": pos[BEAR], "pos_ritc": pos[RITC],
        "pos_usd": pos[USD],
        "gross": gross, "net": net, "within_limits": ok,
        "traded": traded, "direction": direction,
        "req_basket_rich": round(req1, 6), "req_etf_rich": round(req2, 6),
        "fx_hedge_action": fx_action or "", "fx_hedge_qty": fx_qty,
        "unwind_action": unwind_action or "", "unwind_qty": unwind_qty,
    }
    row.update(snap)
    logs.ticks.write(row)

    return traded, edge1, edge2, nlv, tenders_seen, tenders_accepted, req1, req2


# ============================================================================
# HEAT LOOP
# ============================================================================

def wait_for_active(session):
    """Block until the case is ACTIVE again. Returns the tick, or None if stopping."""
    announced = False
    while not shutdown:
        try:
            tick, status = get_tick_status(session)
        except ApiException as e:
            print(f"API error while waiting: {e}")
            sleep(WAIT_POLL)
            continue
        if status is None:
            return None
        if status == "ACTIVE":
            return tick
        if not announced:
            print(f"Case {status} - waiting for the next heat (polling every {WAIT_POLL}s)...")
            announced = True
        sleep(WAIT_POLL)
    return None


def load_prev_bases():
    """Seed the rate limiter from the last heat on disk.

    Without this the step limit only binds within a single run: a restart
    resets prev_bases to empty and the bar jumps straight to whatever the fit
    wants, which is exactly the unobserved extrapolation the limit exists to
    prevent.
    """
    path = LOG_DIR / "heats.csv"
    if not path.exists():
        return {}
    try:
        rows = list(csv.DictReader(path.open(newline="")))
    except OSError:
        return {}
    for r in reversed(rows):
        try:
            return {"basket_rich": float(r["base_basket_rich"]),
                    "etf_rich": float(r["base_etf_rich"])}
        except (KeyError, TypeError, ValueError):
            continue
    return {}


def calibrate_for_heat(logs, heat, margins, prev_bases):
    """Derive this heat's thresholds from every arb logged so far."""
    arbs = calibration.load_arbs(sorted(LOG_DIR.glob("trades*.csv")))
    # Anchor the margin controller on the most recent heat that actually traded,
    # including heats from earlier runs.
    last_heat = (arbs[-1].run_id, arbs[-1].heat) if arbs else None
    bases, margins, diag = calibration.calibrate(arbs, margins, last_heat, prev_bases)

    print(f"  calibrated on {len(arbs)} historical arbs:")
    for d, v in diag.items():
        print(f"    {d:12} slip {v['slip']:.4f} [{v['slip_src']}] "
              f"+ margin {v['margin']:.3f} -> base {v['base']:.4f}  ({v['action']})")
        logs.calib.write({
            "run_id": RUN_ID, "risk_model": RISK_MODEL, "heat": heat,
            "ts": now_iso(), "direction": d, "n_total": v["n_total"],
            "slip": v["slip"], "slip_src": v["slip_src"], "margin": v["margin"],
            "action": v["action"], "base": v["base"],
        })
    return bases, margins


def run_heat(session, logs, heat, margins, prev_bases):
    start_ts = now_iso()
    nlv_start = get_nlv(session)
    first_tick = None
    last_tick = None
    cycles = arbs = tenders_seen = tenders_accepted = 0
    nlv_end = nlv_start
    st = UnwindState()

    global tender_position, prehedge_position
    tender_position = 0   # positions are closed out at heat end
    prehedge_position = 0
    print(f"=== heat {heat} starting (NLV {nlv_start}) ===")
    bases, margins = calibrate_for_heat(logs, heat, margins, prev_bases)

    while not shutdown:
        try:
            tick, status = get_tick_status(session)
        except ApiException as e:
            print(f"API error reading case: {e}")
            sleep(1)
            continue

        if status != "ACTIVE":
            break
        # A tick going backwards means the next heat began without us catching
        # the STOPPED window; hand control back so it is recorded separately.
        if last_tick is not None and tick < last_tick:
            print(f"Tick reset ({last_tick} -> {tick}) - treating as a new heat.")
            break

        if first_tick is None:
            first_tick = tick
        last_tick = tick

        try:
            (traded, e1, e2, nlv, t_seen, t_acc,
             req1, req2) = step_once(session, logs, heat, tick, bases, cycles, st)
        except ApiException as e:
            print(f"API error: {e}")
            sleep(1)
            continue

        cycles += 1
        arbs += 1 if traded else 0
        tenders_seen += t_seen
        tenders_accepted += t_acc
        if nlv is not None:
            nlv_end = nlv

        nlv_str = f"{nlv:.2f}" if nlv is not None else "n/a"
        flag = " TRADE" if traded else ""
        print(f"heat={heat} tick={tick} e1={e1:+.4f}/{req1:.3f} "
              f"e2={e2:+.4f}/{req2:.3f} nlv={nlv_str}{flag}")

        sleep(LOOP_SLEEP)

    # The case wipes open orders on reset, but leaving one resting across the
    # gap would have it fill into the next heat's book at this heat's price.
    if st.resting.orders:
        try:
            print(f"  cancelled {st.resting.cancel_all(session)} resting order(s)")
        except ApiException as e:
            print(f"  cancel-all failed: {e}")

    pnl = None
    if nlv_start is not None and nlv_end is not None:
        pnl = round(nlv_end - nlv_start, 4)

    unwound = st.passive_qty + st.aggressive_qty
    share = st.passive_qty / unwound if unwound else 0.0
    # Rebate earned plus fee avoided: 0.03/share against having crossed.
    saved = st.passive_qty * (FEE_MKT + REBATE_LMT)

    logs.heats.write({
        "run_id": RUN_ID, "risk_model": RISK_MODEL, "heat": heat, "start_ts": start_ts, "end_ts": now_iso(),
        "first_tick": first_tick, "last_tick": last_tick,
        "cycles": cycles, "arbs_fired": arbs,
        "tenders_seen": tenders_seen, "tenders_accepted": tenders_accepted,
        "nlv_start": nlv_start, "nlv_end": nlv_end, "heat_pnl": pnl,
        "base_basket_rich": bases["basket_rich"], "base_etf_rich": bases["etf_rich"],
        "margin_basket_rich": margins["basket_rich"],
        "margin_etf_rich": margins["etf_rich"],
        "unwind_passive": round(st.passive_qty),
        "unwind_aggressive": round(st.aggressive_qty),
        "passive_share": round(share, 4),
        "fee_saved": round(saved, 2),
    })
    print(f"=== heat {heat} done: ticks {first_tick}-{last_tick}, {cycles} cycles, "
          f"{arbs} arbs, {tenders_seen} tenders, P&L {pnl} ===")
    print(f"    unwind {unwound:,.0f} units: {share:.0%} passive, "
          f"{saved:,.0f} CAD of fees saved vs crossing everything")
    return margins, bases


def main():
    logs = Logs()
    print("=" * 68)
    print(f" RISK MODEL: {RISK_MODEL}   ({RISK_MODEL_DESC})")
    print(f"   net   = BULL + BEAR + 2*RITC        cap {MAX_SHORT_NET}..{MAX_LONG_NET}")
    print(f"   gross = |BULL|+|BEAR|+2*|RITC|      cap {MAX_GROSS}")
    print(f"   threshold: adaptive per direction (fees + slippage + margin)")
    print(f"   order qty {ORDER_QTY}")
    if PASSIVE_UNWIND:
        print(f"   unwind: rest {PASSIVE_CHUNK:,} at the touch (+{REBATE_LMT}/sh rebate), "
              f"cross only when behind a {UNWIND_DEADLINE_CYCLES}-cycle glide")
    else:
        print(f"   unwind: market orders only ({FEE_MKT}/sh fee)")
    print(f" Thresholds recalibrated per heat; every row tagged risk_model={RISK_MODEL}")
    print("=" * 68)
    print(f"run_id={RUN_ID}  logging to {LOG_DIR}")
    heat = 0
    margins, bases = {}, load_prev_bases()
    if bases:
        print(f"seeded bars from previous run: {bases}")
    try:
        with requests.Session() as s:
            s.headers.update(authorization())
            while not shutdown:
                if wait_for_active(s) is None:
                    break
                heat += 1
                try:
                    margins, bases = run_heat(s, logs, heat, margins, bases)
                except ApiException as e:
                    print(f"Heat {heat} aborted: {e} - waiting for the next one.")
                except Exception as e:  # noqa: BLE001 - keep the run alive
                    print(f"Heat {heat} crashed ({type(e).__name__}: {e}) - continuing.")
    finally:
        logs.close()
        print(f"Logs closed. run_id={RUN_ID}, {heat} heat(s) recorded.")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    main()
