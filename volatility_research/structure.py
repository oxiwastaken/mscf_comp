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
#
# Swept over 123 recordings, paired per heat (`volatility_research/wing_grid.py`).
# The surface is a broad plateau - every combination of 468-750 contracts with
# a $600-$1,200 gate scores between +$4,800 and +$8,200 a heat - so nothing
# here is fitted to a knife edge. 750 at a $1,000 gate is +$8,099 (t 3.7) with
# a worst heat of -$31,434; the $800 gate is worth $85 more on the mean and
# $16,000 worse on the tail, which is not a trade worth making over ten heats.
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

# Most extra GAMMA the structure may carry, in delta per $1 move of RTM.
#
# The mirror of the vega gate, and the live run is what motivated it. Build
# da50ee6, heat at 17:47: the book sat still for ten ticks while RTM moved 0.73
# and delta swung 40,763 - about 55,800 of gamma, of which the structure's
# extra contracts were roughly 43%. The hedge only acts once a tick, so that
# second cost $2,171 and the heat spent 29 ticks over the delta limit.
#
# Gating on the clock was tried first and the surface was not smooth enough to
# believe - a 5% shift in the cutoff swung the mean by $5k, and the tick it
# liked sits on the week-3 news boundary, which is calendar fitting rather than
# a gamma effect. This gates on the thing itself. Zero disables it.
WING_MAX_EXTRA_GAMMA = 0.0

# Deploy the gross capacity only when the net limit is what is holding the
# position back - i.e. at full conviction. Below full size the position is
# limited by the view, not by the rules, and borrowing net room buys nothing.
WING_MIN_CONVICTION = 1.0

# Take one wing from each side of spot rather than the two cheapest outright.
#
# OFF, and measured rather than assumed. Cheapest-vega-first will put both legs
# on the same side - with RTM at 48.85 the two cheapest were RTM51C and RTM52C,
# 750 contracts of one-sided short gamma - and the obvious fix is a call above
# and a put below. It costs almost the whole trade: +$1,058 a heat (t 0.8)
# symmetric against +$6,301 (t 2.5) cheapest-first, because forcing a leg onto
# the near side forces it onto a leg whose vega is close to the money's, and
# the vega SPREAD between what is shorted and what is bought is the entire
# point of the structure.
WING_SYMMETRIC = False


# A held wing is kept until its vega reaches this fraction of the money's, at
# which point it is not a wing any more - RTM has drifted onto it, and it is
# carrying nearly the vega we are trying to be long while costing us gamma.
# Only then is the structure stood down, and it is stood down rather than
# swapped, because a swap has no legal order sequence at full size.
WING_STALE_RATIO = 0.90

# Stand the structure down once the hedge is already using this much of the
# share limit.
#
# The incremental test below - can we hedge the delta this structure ADDS -
# is right in principle and insufficient in practice. A straddle's legs nearly
# cancel, so the increment measured at any one tick is small even while the
# 1.75x book behind it is what has been driving the share position up all heat.
# Replaying the trader through recorded heats, one heat in eight pinned RTM at
# 50,000 with 29,994 of delta outstanding - a state with no way back inside the
# band and a $0.10-per-delta-per-second meter running.
#
# So there is a second, blunter test: if the hedge has already spent this much
# of its room, stop adding to the book it has to hedge.
WING_MAX_SHARE_USAGE = 0.80

# How much of the entry test a structure already on the board has to keep
# clearing. Opening costs about $3,000 and that money is spent; only the cost
# of UNWINDING is still avoidable, so the bar to hold is a fraction of the bar
# to open. Without this the model pays to rebuild the same structure over and
# over as RTM drifts - see the gate below.
WING_KEEP_FRACTION = 0.34

# Net contracts deliberately left unused, so the structure can be ROTATED.
#
# This is the difference between a structure that works and one that does not,
# and it is pure order-sequencing arithmetic. Sitting exactly on both caps -
# net 1,000, gross 2,500 - there is no legal way to move a wing from one strike
# to another: opening the new leg first breaches gross, and closing the old one
# first breaches NET, because closing a short raises net. The server enforces
# both on submission, so the move that a backtest performs atomically simply
# cannot be sent.
#
# That mattered more than it sounds. Measured, free rotation is worth about
# $9,400 a heat of the structure's value; forbidding it leaves +$536 (t 0.2),
# which is nothing. The alternatives are worse: standing the structure down and
# rebuilding it next tick moves the same 1,500 contracts as a rotation but
# happens far more often, and at a 0.70 staleness trigger it costs $12,100 a
# heat.
#
# Holding back 50 net contracts makes the rotation legal in 50-lot steps:
#
#     hold    A=850 each leg, W=750     net   950   gross 2,450
#     close   50 of the old wing        net 1,000   gross 2,400   legal
#     open    50 of the new wing        net   950   gross 2,450   legal
#
# It costs 25 contracts a leg of at-the-money position - about 1.4% of the
# vega - to buy back a $9,400 lever. chunks() finds the sequence on its own,
# because it only ever sends a chunk that leaves the book inside both limits.
WING_NET_BUFFER = 50


def stale(row, atm_vega):
    """Has this wing drifted far enough into the money to stop being one?"""
    if not atm_vega or row.get('vega') is None:
        return True
    return abs(row['vega']) / atm_vega > WING_STALE_RATIO


def leg_cap(net_limit=None):
    """Contracts per straddle leg allowed by the NET limit alone."""
    return (net_limit if net_limit is not None else config.LIMIT_OPT_NET) // 2


def held_conviction(rows, strike, buffer=None):
    """
    The straddle the book is really expressing, with the structure taken out.

    The ratchet asks "how big is the position I already hold", and neither
    obvious answer is right once the structure is on. The at-the-money leg
    count is too big - it includes size borrowed from the wing shorts, so the
    ratchet treats its own structure as conviction and ratchets on top of it.
    Net over two is too small by half the rotation buffer, and that is worse
    than it sounds: the ratchet then pins the target below the conviction gate,
    the structure stands down, and the book decays a buffer at a time until it
    is flat. Measured, that decay was worth -$19,941 a heat.

    So undo the structure exactly: subtract the size the wings paid for.
    """
    buffer = WING_NET_BUFFER if buffer is None else buffer
    wings = sum(abs(int(row['position'])) for row in rows
                if row['strike'] != strike)
    extra = max(wings - buffer, 0) // 2
    legs = sum(int(row['position']) for row in rows
               if row['strike'] == strike) / 2.0
    if not legs:
        return 0.0
    return legs - extra if legs > 0 else legs + extra


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
                 min_conviction=None, underlying=None):
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

    # The straddle can never exceed what the net limit allows on its own, and
    # this is enforced here as well as by the caller because everything below
    # assumes it. The ratchet is what makes that worth defending: it returns
    # the LARGER of the new target and what is held, so one tick with an
    # over-large reading feeds the next, and the book runs away - seen at tick
    # 227 of a recording, 1,062 contracts a leg against a 500 cap, net 1,748.
    hard_cap = leg_cap(net_limit)
    target = max(-hard_cap, min(hard_cap, int(target)))

    book = {call['ticker']: target, put['ticker']: target}
    if not target or not wing_contracts:
        return book

    # Full conviction means "as big as the rules let us hold", and with the
    # rotation buffer in place that is half a buffer short of the raw net cap.
    # Measuring against the raw cap instead is a trap with teeth: the ratchet
    # reads conviction off the NET position, the buffer makes that 950 rather
    # than 1,000, so a target of 475 never reaches a 500 bar - the structure
    # stands down, rebuilds on the next tick that clears it, and thrashes. That
    # one off-by-a-buffer was worth -$19,941 a heat.
    cap = leg_cap(net_limit) - WING_NET_BUFFER // 2
    if abs(target) < min_conviction * cap:
        return book

    atm_vega = abs(call.get('vega') or 0.0)
    row_by_ticker = {row['ticker']: row for row in chain['rows']}
    # Anything off the working strike with a live quote can serve as a wing,
    # which is a wider set than the legs we would CHOOSE today. See the keep
    # loop below: a wing already held is kept, not swapped for a cheaper one.
    wingable = {row['ticker'] for row in chain['rows']
                if row['strike'] != strike and row.get('vega') is not None
                and row.get('bid') and row.get('ask')}
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
    kept = 0                      # contracts carried over from the live book

    # Keep what is already on the board first.
    #
    # A wing is kept while it points the right way and is still a reasonable
    # wing - NOT only while it is the cheapest one. That is not just to save
    # turnover: swapping a wing from one leg to another has no legal order
    # sequence at all. At full size the book is at both caps, so opening the
    # new leg first breaches gross (2,500) and closing the old one first
    # breaches net, because closing a short RAISES net. Found by replaying the
    # trader through heat_20260918_112434 - gross 2,575 at tick 36, an order
    # the server rejects.
    #
    # Standing the whole structure down is legal, because the at-the-money legs
    # come down with it and trimming those relieves both limits at once. So the
    # rule is: keep, or stand down. Never swap.
    for ticker, quantity in sorted((held or {}).items()):
        if ticker not in wingable or not quantity:
            continue
        if (quantity > 0) != (sign > 0):
            continue                       # points the wrong way for this view
        if stale(row_by_ticker[ticker], atm_vega):
            # Drifted into the money: not a wing any more. Drop it and let the
            # rotation buffer pay for moving to a better leg below.
            continue
        lot = min(abs(quantity), per_leg, room - placed)
        if lot <= 0:
            continue
        book[ticker] = sign * lot
        placed += lot
        kept += lot

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
    #
    # The bar to KEEP a structure is far lower than the bar to open one, and it
    # has to be: opening costs ~$3,000 in fees and crossed spread, and once
    # that is paid it is sunk. Charging the entry test against a position
    # already on the board tears it down the moment RTM drifts and a kept wing
    # loses a little vega, then rebuilds it the next tick from the best legs,
    # then tears it down again. Measured, that thrash turns +$7,830 a heat into
    # -$3,603 - it is worse than never having built the structure at all.
    if WING_MIN_EXTRA_VEGA:
        shorted = sum(abs(q) * abs(row_by_ticker[t]['vega'])
                      for t, q in book.items()
                      if t in row_by_ticker and t not in
                      (call['ticker'], put['ticker']))
        extra = (placed * atm_vega - shorted) * config.CONTRACT_MULTIPLIER
        gate = (WING_MIN_EXTRA_VEGA * WING_KEEP_FRACTION if kept
                else WING_MIN_EXTRA_VEGA)
        if extra < gate:
            return {call['ticker']: target, put['ticker']: target}

    # And the gamma it makes us carry, which is what the hedge has to chase
    # between ticks. See WING_MAX_EXTRA_GAMMA.
    if WING_MAX_EXTRA_GAMMA:
        atm_gamma = abs(call.get('gamma') or 0.0)
        wing_gamma = sum(abs(q) * abs(row_by_ticker[t].get('gamma') or 0.0)
                         for t, q in book.items()
                         if t in row_by_ticker and t not in
                         (call['ticker'], put['ticker']))
        added = (placed * atm_gamma - wing_gamma) * config.CONTRACT_MULTIPLIER
        if added > WING_MAX_EXTRA_GAMMA:
            return {call['ticker']: target, put['ticker']: target}

    # Every contract of wing short is a contract of net room; spend it on the
    # money, where the vega per contract is highest - all of it bar the
    # rotation buffer, which is what keeps the structure movable. See
    # WING_NET_BUFFER.
    extra = max(placed - WING_NET_BUFFER, 0) // 2
    book[call['ticker']] = target + (extra if target > 0 else -extra)
    book[put['ticker']] = target + (extra if target > 0 else -extra)

    if not hedgeable(book, target, call, put, row_by_ticker, underlying):
        return {call['ticker']: target, put['ticker']: target}
    return book


def hedgeable(book, target, call, put, row_by_ticker, underlying):
    """
    Can the shares still be there to hedge what the structure adds?

    The structure raises the at-the-money position by half a contract per wing
    contract while the RTM limit stays at 50,000 shares, and measured over 125
    recordings it takes peak share usage from 25,733 to 39,684, with a worst
    heat of 49,936 - 64 shares of margin. A hedge that cannot be completed is
    the one cost in this case that is unbounded: $0.10 per delta per second,
    for as long as it takes to get back inside.

    So the test is on the delta the structure actually ADDS at today's greeks,
    not on a worst case. A blanket "one contract needs a hundred shares" rule
    was tried first and is roughly ten times too conservative - a straddle pair
    nets about 0.04 delta near the money against a wing's 0.10 - which shut the
    structure off and gave back three quarters of what it earns.

    The straddle itself is never sacrificed to this. It is the view; the
    structure is a claim on a limit, and the claim is what gives way.
    """
    if underlying is None:
        return True
    if abs(underlying) > WING_MAX_SHARE_USAGE * config.LIMIT_RTM_GROSS:
        return False
    plain = {call['ticker']: target, put['ticker']: target}
    added = 0.0
    for ticker, quantity in book.items():
        row = row_by_ticker.get(ticker)
        if row is None or row.get('delta') is None:
            continue
        added += ((quantity - plain.get(ticker, 0)) * row['delta']
                  * config.CONTRACT_MULTIPLIER)
    # Hedging that delta means holding |added| MORE shares than are held now,
    # and the limit is on the share position, not on the delta. Crediting the
    # free delta band here was tried and is far too generous - it let the book
    # run to 45,000 shares with 5,000 of structure delta still to hedge and
    # call it fine. Replaying the trader through recorded heats, that is the
    # difference between peak share usage of 50,000 (pinned, nothing left to
    # hedge with) and a book that stands its structure down before it gets
    # there. The band is what absorbs drift; it is not spare hedging capacity.
    return abs(underlying) + abs(added) <= config.LIMIT_RTM_GROSS


def limits_used(book):
    """(gross, net) contracts used by a {ticker: contracts} book."""
    gross = sum(abs(q) for q in book.values())
    net = abs(sum(book.values()))
    return gross, net
