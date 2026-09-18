"""
The option book to hold for a given view, subject to BOTH position limits.

The case imposes two separate limits on options (PDF, "Trading/Position Limits"):

    gross  2,500 contracts   sum of the absolute values of long and short
    net    1,000 contracts   longs and shorts NETTED against each other

A straddle spends 1,000 of each, so it is capped by the net limit with 1,500
contracts of gross capacity left idle in every heat. That matters because the
view is expressed in vega, and vega is bought by the contract: at the money it
is worth roughly $5.76 per contract per vol point early in a heat, and the net
limit therefore caps the whole position at about $5,760 a vol point.

Short positions negate long ones against the net limit, so shorting the
LOWEST-vega legs on the board buys net room, which is then spent on the
highest-vega leg there is - the at-the-money straddle:

    long  875 ATM calls + 875 ATM puts            net +1,750   gross 1,750
    short 375 wing puts + 375 wing calls          net   -750   gross   750
                                                  net  1,000   gross 2,500

Worth  750 * (v_atm - v_wing)  of extra vega per vol point, against 1,500
extra contracts of fees and crossed spread - about $2 a contract each way.

So the structure is a bet on the vega RATIO between the wings and the money,
and that ratio is not a constant: it is near 0.6 early in a heat, when
sigma*sqrt(T) is large relative to the 48-52 strike range, and collapses toward
0 as expiry approaches. At 0.6 the extra 750 contracts buy 40% of their own
vega and need ~1.7 vol points of convergence to pay for opening; at 0.2 they
need ~0.9. `WING_MAX_VEGA_RATIO` is the gate that keeps the structure off the
board when the wings are not cheap enough in vega terms to be worth shorting.

Nothing here knows about orders or the API. It answers one question - what
should be held - so the backtest and the live trader can answer it identically.
"""

import config

# Contracts shorted into the wings. 750 is the maximum the limits allow
# alongside a full straddle: net 2A - W = 1,000 and gross 2A + W = 2,500 give
# W = 750, A = 875. Zero disables the structure entirely.
WING_CONTRACTS = 0

# Only short a leg whose vega is at most this fraction of the at-the-money
# leg's. 1.0 admits anything; the cost of the extra contracts is fixed, so the
# gate is what decides whether the vega bought is worth it.
WING_MAX_VEGA_RATIO = 1.0

# Minimum DOLLARS of extra vega per vol point the structure must buy before it
# is worth putting on. Zero disables the test and leaves the ratio gate alone.
#
# The ratio is the wrong thing to gate on by itself and the arithmetic says so.
# What the structure buys is  W * (v_atm - v_wing)  per vol point, and what it
# costs is about $2 a contract on 2W contracts - a fixed $2,250 at W = 562,
# plus the same again to unwind. Ratio alone cannot see that: a 0.6 ratio early
# in a heat (v_atm $4.87 a contract) buys $1,096 a vol point, while a 0.18
# ratio at tick 225 (v_atm $2.78) buys $1,282 - nearly the same trade, one of
# which a 0.5 ratio gate accepts and the other it rejects. Gate on the money.
WING_MIN_EXTRA_VEGA = 0.0

# Deploy the gross capacity only when the net limit is what is holding the
# position back - i.e. at full conviction. Below full size the position is
# limited by the view, not by the rules, and borrowing net room buys nothing.
WING_MIN_CONVICTION = 1.0

# Take one wing from each side of spot rather than the two cheapest outright.
#
# Cheapest-vega-first will happily put both legs on the same side: with RTM at
# 48.85 the two cheapest were RTM51C and RTM52C, which is 750 contracts of
# naked upside short gamma. It is also the side RTM has to travel to reach, so
# the position gets shorter exactly as it moves against us. A call above and a
# put below costs a little vega ratio and buys a structure whose two halves
# cannot both go wrong at once.
WING_SYMMETRIC = True


def leg_cap(net_limit=None):
    """Contracts per straddle leg allowed by the NET limit alone."""
    return (net_limit if net_limit is not None else config.LIMIT_OPT_NET) // 2


def wing_legs(rows, strike, spot, atm_vega, max_ratio=None, symmetric=None):
    """
    Candidate legs to short, cheapest vega first.

    One leg per strike, and the out-of-the-money one: a call and a put at the
    same strike carry identical vega, so the choice is free in vega terms and
    the OTM leg carries far less delta to hedge and far less premium to fund.

    Under `symmetric` at most one leg comes from each side of spot - see
    WING_SYMMETRIC.
    """
    max_ratio = WING_MAX_VEGA_RATIO if max_ratio is None else max_ratio
    symmetric = WING_SYMMETRIC if symmetric is None else symmetric
    if not atm_vega or atm_vega <= 0:
        return []
    by_strike = {}
    for row in rows:
        if row['strike'] == strike or row.get('vega') is None:
            continue
        if not row.get('bid') or not row.get('ask'):
            continue
        wanted = 'c' if row['strike'] >= spot else 'p'
        if row['flag'] != wanted:
            continue
        by_strike[row['strike']] = row
    candidates = sorted(
        (row for row in by_strike.values()
         if abs(row['vega']) / atm_vega <= max_ratio),
        key=lambda r: abs(r['vega']))
    if not symmetric:
        return candidates
    picked = []
    for above in (True, False):
        side = [r for r in candidates if (r['strike'] >= spot) == above]
        if side:
            picked.append(side[0])
    return sorted(picked, key=lambda r: abs(r['vega']))


def desired_book(chain, strike, target, held=None, net_limit=None,
                 gross_limit=None, wing_contracts=None, max_ratio=None,
                 min_conviction=None):
    """
    {ticker: signed contracts} to hold, given a signed per-leg straddle target.

    `target` is the straddle the NET limit alone would allow, which is what the
    sizing ramp produces. Any wing short added here buys net room back, and all
    of it is spent on the straddle, so the net limit stays exactly binding.

    `held` is what is on the book now. The wings are STICKY against it: the
    cheapest-vega leg changes as RTM drifts, and re-picking it every tick round
    trips 750 contracts at $2 each to move a short from one wing to a wing
    barely cheaper. A wing is kept while it still points the right way and
    still qualifies; only the shortfall is topped up. The structure is a claim
    on the net limit, not a view, so it should not be traded like one.
    """
    wing_contracts = (WING_CONTRACTS if wing_contracts is None
                      else wing_contracts)
    min_conviction = (WING_MIN_CONVICTION if min_conviction is None
                      else min_conviction)
    net_limit = config.LIMIT_OPT_NET if net_limit is None else net_limit
    gross_limit = config.LIMIT_OPT_GROSS if gross_limit is None else gross_limit

    by_strike = {}
    for row in chain['rows']:
        by_strike.setdefault(row['strike'], {})[row['flag']] = row
    legs = by_strike.get(strike, {})
    call, put = legs.get('c'), legs.get('p')
    if call is None or put is None:
        return {}

    book = {call['ticker']: target, put['ticker']: target}
    if not target or not wing_contracts:
        return book

    cap = leg_cap(net_limit)
    if abs(target) < min_conviction * cap:
        return book

    atm_vega = abs(call.get('vega') or 0.0)
    row_by_ticker = {row['ticker']: row for row in chain['rows']}
    candidates = wing_legs(chain['rows'], strike, chain['spot'], atm_vega,
                           max_ratio)
    if not candidates:
        return book
    symmetric = WING_SYMMETRIC

    # Room the limits leave for the structure. Every contract of wing short
    # buys half a contract on each straddle leg, so with A = |target| + W/2:
    #   net    2A - W = 2|target|      <= net_limit    (W drops out)
    #   gross  2A + W = 2|target| + 2W <= gross_limit
    # so the gross limit alone caps W, and it caps it at half the free gross.
    room = min(wing_contracts, (gross_limit - 2 * abs(target)) // 2)
    if room < 2:
        return book

    # Spread the short over the two cheapest legs where there are two, so the
    # delta the structure adds mostly cancels: shorting an OTM call is short
    # delta, shorting an OTM put is long it.
    # Half the structure per side. Where only one side qualifies, symmetric
    # mode takes half a structure rather than loading it all onto that side -
    # the concentration is the thing being avoided, not the leg count.
    per_leg = room // 2 if (symmetric or len(candidates) > 1) else room
    sign = -1 if target > 0 else 1
    placed = 0

    # Keep what is already on the board first, while it still qualifies.
    qualifying = {row['ticker'] for row in candidates}
    for ticker, quantity in sorted((held or {}).items()):
        if ticker not in qualifying or not quantity:
            continue
        if (quantity > 0) != (sign > 0):
            continue                       # points the wrong way for this view
        lot = min(abs(quantity), per_leg, room - placed)
        if lot <= 0:
            continue
        book[ticker] = sign * lot
        placed += lot

    for row in candidates:
        if placed >= room:
            break
        already = abs(book.get(row['ticker'], 0))
        lot = min(per_leg - already, room - placed)
        if lot <= 0:
            continue
        book[row['ticker']] = book.get(row['ticker'], 0) + sign * lot
        placed += lot

    if not placed:
        return book

    # What the structure actually buys, in dollars of vega per vol point, at
    # the legs it would really use. Below the threshold it is not worth the
    # contracts it costs, so hold the plain straddle instead.
    if WING_MIN_EXTRA_VEGA:
        shorted = sum(abs(q) * abs(row_by_ticker[t]['vega'])
                      for t, q in book.items()
                      if t in row_by_ticker and t not in
                      (call['ticker'], put['ticker']))
        extra = (placed * atm_vega - shorted) * config.CONTRACT_MULTIPLIER
        if extra < WING_MIN_EXTRA_VEGA:
            return {call['ticker']: target, put['ticker']: target}

    # Every contract of wing short is a contract of net room; spend it all on
    # the money, which is where the vega per contract is highest.
    extra = placed // 2
    book[call['ticker']] = target + (extra if target > 0 else -extra)
    book[put['ticker']] = target + (extra if target > 0 else -extra)
    return book


def limits_used(book):
    """(gross, net) contracts used by a {ticker: contracts} book."""
    gross = sum(abs(q) for q in book.values())
    net = abs(sum(book.values()))
    return gross, net
