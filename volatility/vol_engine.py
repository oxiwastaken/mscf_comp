"""
Volatility maths for the RITCx Volatility Trading Case.

The central idea: under Black-Scholes with deterministic, piecewise-constant
volatility, a European option's value depends only on the INTEGRATED VARIANCE
over its remaining life, not on the volatility of any single week:

    sigma_effective = sqrt( (1 / (T - t)) * integral of sigma(s)^2 ds )

This case is built exactly that way - sigma is constant within each week and
shifts at known week boundaries - so the time-weighted root-mean-square blend
below IS the correct Black-Scholes input, not an approximation of it. Pricing
off the current week's sigma alone is simply the wrong number, and it is most
wrong early in the case when the position is largest.

A second, separate edge falls out of the case structure: all ten options share
a single expiry, and the market maker prices with Black-Scholes at a zero
variance risk premium and no smile. In equilibrium every strike must therefore
show the SAME implied vol. Any strike-to-strike dispersion is the market
maker's own inconsistency, and capturing it does not require our volatility
forecast to be right at all.
"""

import math
import re

from py_vollib.black_scholes import black_scholes
from py_vollib.black_scholes.greeks import analytical
from py_vollib.black_scholes.implied_volatility import implied_volatility

import config
from news_parser import week_of_tick

# Spec format is RTM48C / RTM48P. The alternate pattern (RTM1C48, used by some
# other Rotman option cases) is accepted too so a format change on the day
# degrades into a warning rather than an empty option chain.
_SUFFIX_RE = re.compile(r'^([A-Z]+?)(\d+(?:\.\d+)?)\s*([CP])$', re.IGNORECASE)
_INFIX_RE = re.compile(r'^([A-Z]+?)(\d*)\s*([CP])\s*(\d+(?:\.\d+)?)$', re.IGNORECASE)


def parse_option_ticker(ticker):
    """
    Pull (flag, strike) out of an option ticker, or return None for the
    underlying / anything unrecognised. flag is 'c' or 'p' for py_vollib.
    """
    ticker = ticker.strip().upper()
    match = _SUFFIX_RE.match(ticker)
    if match:
        return match.group(3).lower(), float(match.group(2))
    match = _INFIX_RE.match(ticker)
    if match:
        return match.group(3).lower(), float(match.group(4))
    return None


def time_to_expiry(tick, ticks_total):
    """
    Years remaining until the options expire at the end of the case.

    The case is one month of a 240-day year, so a full case is 1/12 of a year.
    At tick 0 of a 300-tick case this gives 300/3600 = 1/12, matching the
    provided base script.
    """
    if ticks_total <= 0:
        return 0.0
    remaining = max(ticks_total - tick, 0)
    return (remaining / ticks_total) * config.CASE_YEARS


def solve_implied_vol(price, spot, strike, t_years, flag):
    """
    Back the implied volatility out of an option price. Returns None when the
    price is below intrinsic, above maximum, or the solver cannot converge -
    all of which happen near expiry and must not be mistaken for low vol.
    """
    if price is None or price <= 0 or t_years <= 0 or spot <= 0:
        return None
    try:
        iv = implied_volatility(price, spot, strike, t_years,
                                config.RISK_FREE_RATE, flag)
    except Exception:
        return None
    if iv is None or not math.isfinite(iv):
        return None
    if not config.IV_MIN <= iv <= config.IV_MAX:
        return None
    return iv


def remaining_week_weights(tick, ticks_total, weeks=config.WEEKS_IN_CASE):
    """
    Ticks still to run in each remaining week, as {week_number: ticks_left}.

    This is the time weighting for the variance blend. Only the unexpired part
    of the current week counts, which is why the blend sharpens toward the
    current week's sigma as the case runs down.
    """
    if ticks_total <= 0:
        return {}
    per_week = ticks_total / weeks
    weights = {}
    for week in range(1, weeks + 1):
        start = (week - 1) * per_week
        end = week * per_week
        left = min(end, ticks_total) - max(start, tick)
        if left > 0:
            weights[week] = left
    return weights


def blended_sigma(tick, ticks_total, week_vols, market_iv=None,
                  mode=None, fixed=None):
    """
    The time-weighted RMS volatility over the option's remaining life.

    week_vols: {week_number: sigma} parsed from the news.
    market_iv: the market's own average implied vol, used as the prior for
               unannounced weeks when mode == 'market'.

    Returns (sigma, detail) where detail lists the per-week inputs used, so the
    dashboard can show exactly what the number was built from.
    """
    mode = mode or config.TAIL_VOL_MODE
    fixed = config.TAIL_VOL_FIXED if fixed is None else fixed

    weights = remaining_week_weights(tick, ticks_total)
    if not weights:
        return None, []

    # Prior for any week the news has not covered yet.
    if mode == 'market' and market_iv:
        prior = market_iv
    elif mode == 'persist' and week_vols:
        prior = week_vols[max(week_vols)]
    else:
        prior = fixed

    total_weight = 0.0
    total_var = 0.0
    detail = []
    for week, weight in sorted(weights.items()):
        sigma = week_vols.get(week)
        source = 'news'
        if sigma is None:
            sigma = prior
            source = f'prior:{mode}'
        if sigma is None:
            continue
        total_var += weight * sigma ** 2
        total_weight += weight
        detail.append({'week': week, 'sigma': sigma,
                       'weight': weight, 'source': source})

    if total_weight <= 0:
        return None, detail
    return math.sqrt(total_var / total_weight), detail


def build_chain(securities, tick, ticks_total, week_vols, mode=None):
    """
    Assemble the full analysis for one refresh.

    Returns a dict with the underlying, the option rows (each carrying market
    IV, fair value, edges and greeks), the blended forecast, and the portfolio
    delta. Pure computation - it sends no orders and mutates nothing.
    """
    by_ticker = {s['ticker'].upper(): s for s in securities}
    underlying = by_ticker.get(config.UNDERLYING.upper())
    if underlying is None:
        raise ValueError(
            f'{config.UNDERLYING} not found in securities. Got: '
            f'{sorted(by_ticker)}'
        )

    spot = _mid(underlying)
    live = spot is not None
    if not live:
        # No two-sided market: between heats, or the feed has dropped. Fall back
        # to `last` only so the spot can still be displayed, and flag it - every
        # downstream signal must treat this as unusable.
        spot = underlying.get('last') or 0.0
    t_years = time_to_expiry(tick, ticks_total)

    # Pass 1: market implied vols. These depend only on quoted prices, never on
    # our own forecast, so there is no circularity in using their average as
    # the prior for unannounced weeks.
    rows = []
    for ticker, sec in by_ticker.items():
        parsed = parse_option_ticker(ticker)
        if not parsed:
            continue
        flag, strike = parsed
        mid = _mid(sec)
        # Read the fee from the server rather than the case PDF. Measured live
        # on the practice server, options charge $1.00/contract and RTM
        # $0.01/share - exactly HALF the $2.00 and $0.02 the PDF states. Using
        # the PDF numbers made the edge hurdle twice too high and silently
        # rejected trades that were in fact profitable.
        fee_per_contract = sec.get('trading_fee')
        if fee_per_contract is None:
            fee_per_contract = config.FEE_PER_CONTRACT
        rows.append({
            'ticker': ticker,
            'flag': flag,
            'strike': strike,
            'bid': sec.get('bid'),
            'ask': sec.get('ask'),
            'last': sec.get('last'),
            'mid': mid,
            'position': sec.get('position', 0) or 0,
            'fee_per_contract': fee_per_contract,
            'fee_per_share': fee_per_contract / config.CONTRACT_MULTIPLIER,
            'market_iv': solve_implied_vol(mid, spot, strike, t_years, flag),
        })

    # How much implied-vol uncertainty the quote width alone creates. Vega here
    # is per vol POINT (py_vollib convention), so a 2-cent spread on an option
    # with vega 0.0319 pins IV to 0.63 vol points, while the same 2 cents on
    # vega 0.0006 leaves it undetermined across 30-plus. Anything wider than
    # MAX_IV_SPREAD carries no usable information and is excluded from both the
    # market average and the tradeable set.
    for row in rows:
        row['market_vega'] = None
        row['iv_spread'] = None
        row['tradeable'] = False
        if not row['market_iv'] or t_years <= 0:
            continue
        try:
            vega = analytical.vega(row['flag'], spot, row['strike'], t_years,
                                   config.RISK_FREE_RATE, row['market_iv'])
        except Exception:
            continue
        row['market_vega'] = vega
        width = (row['ask'] - row['bid']) if (row['bid'] and row['ask']) else None
        if vega and vega > 0 and width:
            row['iv_spread'] = (width / vega) / 100.0
            row['tradeable'] = row['iv_spread'] <= config.MAX_IV_SPREAD

    # Vega-weight the market average so a noisy wing cannot drag it. In
    # 'market' mode this average becomes the prior for unannounced weeks, so a
    # plain mean would leak quote-rounding noise straight into the forecast.
    weighted = [(r['market_iv'], r['market_vega']) for r in rows
                if r['tradeable'] and r['market_iv'] and r['market_vega']]
    if weighted:
        total = sum(w for _, w in weighted)
        market_avg_iv = sum(iv * w for iv, w in weighted) / total if total else None
    else:
        valid = [r['market_iv'] for r in rows if r['market_iv']]
        market_avg_iv = sum(valid) / len(valid) if valid else None

    # Pass 2: our forecast, then per-option fair value and edge.
    forecast, detail = blended_sigma(tick, ticks_total, week_vols,
                                     market_iv=market_avg_iv, mode=mode)

    for row in rows:
        row['fair'] = None
        row['buy_edge'] = None
        row['sell_edge'] = None
        row['delta'] = None
        row['gamma'] = None
        row['vega'] = None
        row['iv_gap'] = None
        row['dispersion'] = None

        # Dispersion is only meaningful where the quote actually pins the IV.
        if market_avg_iv and row['market_iv'] and row['tradeable']:
            row['dispersion'] = row['market_iv'] - market_avg_iv

        if not forecast or t_years <= 0:
            continue

        try:
            fair = black_scholes(row['flag'], spot, row['strike'], t_years,
                                 config.RISK_FREE_RATE, forecast)
            row['fair'] = fair
            row['delta'] = analytical.delta(row['flag'], spot, row['strike'],
                                            t_years, config.RISK_FREE_RATE, forecast)
            row['gamma'] = analytical.gamma(row['flag'], spot, row['strike'],
                                            t_years, config.RISK_FREE_RATE, forecast)
            row['vega'] = analytical.vega(row['flag'], spot, row['strike'],
                                          t_years, config.RISK_FREE_RATE, forecast)
        except Exception:
            continue

        if row['market_iv']:
            row['iv_gap'] = forecast - row['market_iv']

        # Edge after actually transacting: buy at the ask, sell at the bid, and
        # pay the real commission reported by the server, expressed per
        # underlying share so it is comparable with option premium.
        fee = row['fee_per_share']
        if row['ask']:
            row['buy_edge'] = fair - row['ask'] - fee
        if row['bid']:
            row['sell_edge'] = row['bid'] - fair - fee

    rows.sort(key=lambda r: (r['strike'], r['flag']))

    return {
        'spot': spot,
        'live': live,
        'tick': tick,
        'ticks_total': ticks_total,
        't_years': t_years,
        'week': week_of_tick(tick, ticks_total),
        'rows': rows,
        'forecast': forecast,
        'forecast_detail': detail,
        'market_avg_iv': market_avg_iv,
        'underlying_position': underlying.get('position', 0) or 0,
        'underlying_fee': (underlying.get('trading_fee')
                           if underlying.get('trading_fee') is not None
                           else config.FEE_PER_SHARE),
        'portfolio_delta': portfolio_delta(rows, underlying.get('position', 0) or 0),
    }


def portfolio_delta(rows, underlying_position):
    """
    Total delta in share-equivalents.

    Each option contract controls 100 shares, so an option's contribution is
    position * delta * 100. RTM itself has a delta of exactly 1 per share.
    """
    total = float(underlying_position)
    for row in rows:
        if row.get('delta') is None:
            continue
        total += row['position'] * row['delta'] * config.CONTRACT_MULTIPLIER
    return total


def hedge_suggestion(delta,
                     trigger=config.HEDGE_TRIGGER_DELTA,
                     target=config.HEDGE_TARGET_DELTA,
                     limit=config.DELTA_LIMIT,
                     rate=config.DELTA_PENALTY_RATE):
    """
    Suggest an RTM trade to pull delta back inside the band.

    Hedges into the band with a buffer rather than flattening to zero: a hedge
    costs $0.02/share every time, and a delta that drifts straight back turns
    repeated flattening into a steady leak. Nothing is suggested until delta
    approaches the limit.

    Returns a dict describing the suggested trade, the alternative full flatten,
    and the penalty currently accruing per second if already outside the limit.
    """
    magnitude = abs(delta)
    breach = max(magnitude - limit, 0.0)
    result = {
        'delta': delta,
        'over_limit': breach > 0,
        'penalty_per_second': breach * rate,
        'action': None,
        'quantity': 0,
        'flatten_quantity': int(round(magnitude)),
        'urgent': magnitude > limit,
    }
    if magnitude <= trigger:
        return result

    quantity = int(round(magnitude - target))
    if quantity <= 0:
        return result
    result['action'] = 'SELL' if delta > 0 else 'BUY'
    result['quantity'] = quantity
    return result


def _mid(security):
    """
    Midpoint of the quote, or None if there is no live two-sided market.

    The case guarantees a permanent 2-cent quote while a heat is running, so the
    mid is the market maker's own model price - a far better input than the last
    traded price, which can sit on either side of the spread or be stale.

    Deliberately does NOT fall back to `last`. Observed on the practice server:
    between heats the API reports bid=ask=0.0 while `last` still holds the
    previous heat's settlement prices, and falling back produced confident,
    entirely fictitious implied vols (RTM52C at 11.75%, a vega-weighted market
    average of 6.30%). A missing quote must read as missing, not as cheap.
    """
    bid, ask = security.get('bid'), security.get('ask')
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return None
