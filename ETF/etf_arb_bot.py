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
if not USERNAME or not PASSWORD:
    raise SystemExit(
        "Missing credentials. Copy .env.example to .env and fill in "
        "RIT_USER / RIT_PASS, or export them in your shell."
    )
AUTHORIZATION = {'Authorization': 'Basic ' + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()}

shutdown = False

# RITC acquired from tenders, signed, reset each heat. The unwind targets THIS
# rather than the raw RITC position: the arb's ETF leg is a hedge against its
# own BULL/BEAR legs, and selling it off turns a hedged arb into a naked short
# basket. That is exactly what happened in v10 heat 4 - the arb bought 5,000
# RITC per cycle, the unwind sold it straight back, RITC sat flat at 7,000
# while BULL/BEAR ran to -20,000 each and NLV fell ~9,000 in ten ticks.
tender_position = 0


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
RISK_MODEL = "v11_hedgesafe"
RISK_MODEL_DESC = "unwind targets tender inventory only, never the arb hedge leg"

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
    "order_status",
] + SNAP_FIELDS

TENDER_FIELDS = [
    "run_id", "risk_model", "heat", "tick", "ts", "tender_id", "ticker", "action",
    "quantity", "price", "is_fixed_bid", "expires",
    "caption", "mkt_bid", "mkt_ask", "chunk_vwap", "depth_visible",
    "usd_bid", "usd_ask",
    "edge_vs_mkt", "safe_ok", "safe_reason", "accepted", "accept_ok", "accept_err",
]

HEAT_FIELDS = [
    "run_id", "risk_model", "heat", "start_ts", "end_ts", "first_tick", "last_tick",
    "cycles", "arbs_fired", "tenders_seen", "tenders_accepted",
    "nlv_start", "nlv_end", "heat_pnl",
    "base_basket_rich", "base_etf_rich", "margin_basket_rich", "margin_etf_rich",
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


def unwind_ritc(session, pos, book, snap, logs, heat, tick):
    """Work an oversized RITC position back toward flat, one chunk per cycle.

    A tender leaves ~91k units of ETF on the book, which is 182k of the 300k
    gross limit. Hedging it with the basket would add another 182k and breach
    that cap, and the converters that would collapse it are human-only per the
    case rules, so the position has to be sold back into the market instead.
    Chunking at the 10k per-order maximum and spreading it over cycles is what
    keeps the price impact of that unwind survivable.
    """
    global tender_position
    # Only tender-derived inventory is unwound. Clamp to the live position so a
    # stale tracker can never drive trades in the wrong direction.
    ritc = int(pos.get(RITC, 0))
    target = tender_position
    if target > 0:
        target = min(target, max(ritc, 0))
    elif target < 0:
        target = max(target, min(ritc, 0))
    if abs(target) <= UNWIND_THRESHOLD:
        return None, 0
    action = "SELL" if target > 0 else "BUY"
    avail = depth_within(book, "bid" if action == "SELL" else "ask")
    ref_bid, ref_ask = top_of_book(book)
    ref = ref_bid if action == "SELL" else ref_ask

    remaining = abs(target) - UNWIND_THRESHOLD
    sent = 0
    for _ in range(UNWIND_CHUNKS_PER_CYCLE):
        if remaining <= 0:
            break
        # Take at most half of what is visible, so the chunk scales down on a
        # thin book. `avail` is decremented as we go: the book is only fetched
        # once per cycle, so later chunks must not re-spend the same depth.
        qty = int(min(remaining, UNWIND_CHUNK, MAX_SIZE_EQUITY,
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
        avail = max(avail - qty, 0)
        sent += qty
        tender_position -= qty if action == "SELL" else -qty
        if slip is not None and slip > MAX_UNWIND_SLIP:
            print(f"    slip {slip:+.4f} > {MAX_UNWIND_SLIP} - holding the rest for next cycle")
            break

    return (action, sent) if sent else (None, 0)


def _log_unwind(logs, heat, tick, action, qty, resp, ref, snap, ritc, avail):
    row = {
        "run_id": RUN_ID, "risk_model": RISK_MODEL, "heat": heat, "tick": tick,
        "ts": now_iso(), "direction": "unwind", "edge_at_decision": "",
        "leg": 0, "ticker": RITC, "action": action, "qty": qty,
        "ok": resp is not None,
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

def tender_safety(offer, pos, book, usd_bid, usd_ask):
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
    if action == "SELL":          # we sell to them, buy back through the asks
        ref, _ = vwap_for_qty(book.get("asks") or [], UNWIND_CHUNK)
        edge = (price - ref) if ref else None
    elif action == "BUY":         # we buy from them, sell out through the bids
        ref, _ = vwap_for_qty(book.get("bids") or [], UNWIND_CHUNK)
        edge = (ref - price) if ref else None
    else:
        return False, f"unknown action {action}", None
    if edge is None:
        return False, "no book depth", None

    # Unwind pays the market fee per unit; tender itself is fee-free.
    edge -= FEE_MKT
    if edge < MIN_TENDER_EDGE - EDGE_EPSILON:
        return False, f"edge {edge:+.5f} < {MIN_TENDER_EDGE}", edge

    # Projected exposure in underlying units (ETF counts double).
    signed = -qty if action == "SELL" else qty
    proj_net = pos[BULL] + pos[BEAR] + 2 * (pos[RITC] + signed)
    proj_gross = abs(pos[BULL]) + abs(pos[BEAR]) + 2 * abs(pos[RITC] + signed)
    if proj_gross > SERVER_GROSS_LIMIT - LIMIT_BUFFER:
        return False, f"gross would hit {proj_gross:,.0f}", edge
    if abs(proj_net) > SERVER_NET_LIMIT - LIMIT_BUFFER:
        return False, f"net would hit {proj_net:,.0f}", edge
    return True, "ok", edge


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
            offer, pos, book, snap["usd_bid"], snap["usd_ask"])
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
            "safe_ok": safe, "safe_reason": reason,
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
                tender_position += q if (offer.get("action") or "").upper() == "BUY" else -q
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

def step_once(session, logs, heat, tick, bases):
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
        session, pos, books[RITC], snap, logs, heat, tick)
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

    global tender_position
    tender_position = 0   # positions are closed out at heat end
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
             req1, req2) = step_once(session, logs, heat, tick, bases)
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

    pnl = None
    if nlv_start is not None and nlv_end is not None:
        pnl = round(nlv_end - nlv_start, 4)

    logs.heats.write({
        "run_id": RUN_ID, "risk_model": RISK_MODEL, "heat": heat, "start_ts": start_ts, "end_ts": now_iso(),
        "first_tick": first_tick, "last_tick": last_tick,
        "cycles": cycles, "arbs_fired": arbs,
        "tenders_seen": tenders_seen, "tenders_accepted": tenders_accepted,
        "nlv_start": nlv_start, "nlv_end": nlv_end, "heat_pnl": pnl,
        "base_basket_rich": bases["basket_rich"], "base_etf_rich": bases["etf_rich"],
        "margin_basket_rich": margins["basket_rich"],
        "margin_etf_rich": margins["etf_rich"],
    })
    print(f"=== heat {heat} done: ticks {first_tick}-{last_tick}, {cycles} cycles, "
          f"{arbs} arbs, {tenders_seen} tenders, P&L {pnl} ===")
    return margins, bases


def main():
    logs = Logs()
    print("=" * 68)
    print(f" RISK MODEL: {RISK_MODEL}   ({RISK_MODEL_DESC})")
    print(f"   net   = BULL + BEAR + 2*RITC        cap {MAX_SHORT_NET}..{MAX_LONG_NET}")
    print(f"   gross = |BULL|+|BEAR|+2*|RITC|      cap {MAX_GROSS}")
    print(f"   threshold: adaptive per direction (fees + slippage + margin)")
    print(f"   order qty {ORDER_QTY}")
    print(f" Thresholds recalibrated per heat; every row tagged risk_model={RISK_MODEL}")
    print("=" * 68)
    print(f"run_id={RUN_ID}  logging to {LOG_DIR}")
    heat = 0
    margins, bases = {}, load_prev_bases()
    if bases:
        print(f"seeded bars from previous run: {bases}")
    try:
        with requests.Session() as s:
            s.headers.update(AUTHORIZATION)
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
