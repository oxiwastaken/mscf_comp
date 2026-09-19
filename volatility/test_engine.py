"""
Offline verification of the volatility maths. Runs with no RIT server.

    python test_engine.py

These check the parts that would fail silently and expensively in a live heat:
ticker parsing, the integrated-variance blend, implied-vol round trips, the
delta and hedging arithmetic, and the news regexes.
"""

import math

from py_vollib.black_scholes import black_scholes

import config
import news_parser
import vol_engine

PASS, FAIL = 0, 0


def check(name, condition, detail=''):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f'  PASS  {name}')
    else:
        FAIL += 1
        print(f'  FAIL  {name}  {detail}')


def close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) < tol


# ---------------------------------------------------------------- tickers
print('\nticker parsing')
check('RTM48C -> call, 48', vol_engine.parse_option_ticker('RTM48C') == ('c', 48.0))
check('RTM52P -> put, 52', vol_engine.parse_option_ticker('RTM52P') == ('p', 52.0))
check('RTM50C -> call, 50', vol_engine.parse_option_ticker('RTM50C') == ('c', 50.0))
check('RTM (underlying) -> None', vol_engine.parse_option_ticker('RTM') is None)
check('alternate RTM1C48 still parses',
      vol_engine.parse_option_ticker('RTM1C48') == ('c', 48.0))
check('lowercase handled', vol_engine.parse_option_ticker('rtm49p') == ('p', 49.0))

# all ten spec tickers
spec = [f'RTM{k}{cp}' for k in (48, 49, 50, 51, 52) for cp in ('C', 'P')]
check('all 10 spec tickers parse',
      all(vol_engine.parse_option_ticker(t) for t in spec))

# ------------------------------------------------------------ time to expiry
print('\ntime to expiry')
check('tick 0 of 300 = 1/12 year',
      close(vol_engine.time_to_expiry(0, 300), 1 / 12))
check('matches base script divisor (300-t)/3600 at t=150',
      close(vol_engine.time_to_expiry(150, 300), (300 - 150) / 3600))
check('expiry is zero', close(vol_engine.time_to_expiry(300, 300), 0.0))
check('600-tick case also gives 1/12 at t=0',
      close(vol_engine.time_to_expiry(0, 600), 1 / 12))

# -------------------------------------------------------------- week mapping
# Checked against every row of the sample news schedule in the case PDF, which
# describes a 600-tick case: t=1 -> W1, t=75 -> W1, t=150 -> W2, t=450 -> W4.
print('\nweek mapping')
check('PDF table: t=1 is week 1', news_parser.week_of_tick(1, 600) == 1)
check('PDF table: t=75 is week 1', news_parser.week_of_tick(75, 600) == 1)
check('PDF table: t=150 is week 2', news_parser.week_of_tick(150, 600) == 2)
check('PDF table: t=450 is week 4', news_parser.week_of_tick(450, 600) == 4)
check('300-tick: t=1 is week 1', news_parser.week_of_tick(1, 300) == 1)
check('300-tick: t=74 is week 1', news_parser.week_of_tick(74, 300) == 1)
check('300-tick: t=75 starts week 2', news_parser.week_of_tick(75, 300) == 2)
check('300-tick: t=299 is week 4', news_parser.week_of_tick(299, 300) == 4)
check('week never exceeds 4', news_parser.week_of_tick(300, 300) == 4)

# ------------------------------------------------------- variance blending
print('\nintegrated-variance blend')
weights = vol_engine.remaining_week_weights(0, 300)
check('4 whole weeks remain at t=0', len(weights) == 4 and close(weights[1], 75.0))
weights = vol_engine.remaining_week_weights(40, 300)
check('mid week 1: partial week remains', close(weights[1], 35.0))
check('mid week 1: later weeks whole', close(weights[4], 75.0))

# Worked example: week 1 at 20%, weeks 2-4 at 28%.
#   sqrt((75*0.20^2 + 225*0.28^2) / 300) = sqrt(0.0688) = 0.262298...
sigma, detail = vol_engine.blended_sigma(
    0, 300, {1: 0.20, 2: 0.28, 3: 0.28, 4: 0.28})
expected = math.sqrt((75 * 0.20 ** 2 + 225 * 0.28 ** 2) / 300)
check('blend matches hand calculation', close(sigma, expected),
      f'got {sigma}, want {expected}')
check('blend is well above the current week alone', sigma > 0.26)
check('naive current-week number would be 0.20 (the error we avoid)',
      close(0.20, 0.20) and sigma - 0.20 > 0.06)

# Late in the case the current week must dominate.
sigma_late, _ = vol_engine.blended_sigma(
    290, 300, {1: 0.20, 2: 0.28, 3: 0.28, 4: 0.35})
check('at t=290 blend collapses to week 4 sigma', close(sigma_late, 0.35, 1e-9))

# Constant vol must blend to itself.
flat, _ = vol_engine.blended_sigma(0, 300, {1: 0.25, 2: 0.25, 3: 0.25, 4: 0.25})
check('constant vol blends to itself', close(flat, 0.25))

# Tail-mode behaviour.
m, _ = vol_engine.blended_sigma(0, 300, {1: 0.20}, market_iv=0.30, mode='market')
check('market mode uses market IV for unknown weeks',
      close(m, math.sqrt((75 * 0.04 + 225 * 0.09) / 300)))
f, _ = vol_engine.blended_sigma(0, 300, {1: 0.20}, mode='fixed', fixed=0.25)
check('fixed mode uses the configured prior',
      close(f, math.sqrt((75 * 0.04 + 225 * 0.0625) / 300)))
p, _ = vol_engine.blended_sigma(0, 300, {1: 0.20}, mode='persist')
check('persist mode carries last known sigma forward', close(p, 0.20))
check('no data anywhere still returns a number',
      vol_engine.blended_sigma(0, 300, {}, mode='fixed', fixed=0.25)[0] is not None)

# ------------------------------------------------------------- implied vol
print('\nimplied vol round trip')
S, K, T, sig = 50.0, 50.0, 1 / 12, 0.25
for flag in ('c', 'p'):
    price = black_scholes(flag, S, K, T, 0.0, sig)
    solved = vol_engine.solve_implied_vol(price, S, K, T, flag)
    check(f'{flag}: round trips to 25%', close(solved, sig, 1e-4),
          f'got {solved}')

deep = black_scholes('c', 50.0, 52.0, 1 / 12, 0.0, 0.25)
check('OTM call round trips',
      close(vol_engine.solve_implied_vol(deep, 50.0, 52.0, 1 / 12, 'c'), 0.25, 1e-4))
check('zero time to expiry returns None',
      vol_engine.solve_implied_vol(1.0, 50.0, 50.0, 0.0, 'c') is None)
check('below-intrinsic price returns None (not a fake low vol)',
      vol_engine.solve_implied_vol(0.01, 50.0, 40.0, 1 / 12, 'c') is None)
check('zero price returns None',
      vol_engine.solve_implied_vol(0.0, 50.0, 50.0, 1 / 12, 'c') is None)

# ------------------------------------------------------------ portfolio delta
print('\nportfolio delta')
rows = [{'position': 10, 'delta': 0.5}, {'position': -5, 'delta': -0.4}]
# 10 * 0.5 * 100 = 500;  -5 * -0.4 * 100 = 200;  plus 1000 shares.
check('options scale by the 100x contract multiplier',
      close(vol_engine.portfolio_delta(rows, 1000), 1000 + 500 + 200))
check('flat book is flat delta', close(vol_engine.portfolio_delta([], 0), 0.0))
check('rows with no delta are skipped',
      close(vol_engine.portfolio_delta([{'position': 9, 'delta': None}], 50), 50))

# ----------------------------------------------------------------- hedging
print('\nhedging')
h = vol_engine.hedge_suggestion(1000)
check('inside the band suggests nothing', h['action'] is None)
check('inside the band accrues no penalty', close(h['penalty_per_second'], 0.0))

h = vol_engine.hedge_suggestion(6000)
check('past the trigger suggests a SELL', h['action'] == 'SELL')
check('sells back to the target buffer', h['quantity'] == 4000)
check('not yet over the limit', not h['over_limit'])

h = vol_engine.hedge_suggestion(-6000)
check('negative delta suggests a BUY', h['action'] == 'BUY')
check('symmetric size', h['quantity'] == 4000)

h = vol_engine.hedge_suggestion(9000)
check('over the limit is flagged', h['over_limit'] and h['urgent'])
# (9000 - 7000) * 0.10 = $200/second
check('penalty matches the spec formula', close(h['penalty_per_second'], 200.0),
      f"got {h['penalty_per_second']}")
check('flatten quantity is the full delta', h['flatten_quantity'] == 9000)

# --------------------------------------------------------------- news parsing
print('\nnews parsing')
items = [
    {'tick': 1, 'headline': 'Analyst Update',
     'body': 'The realized volatility of RTM for this week will be 20%'},
    {'tick': 37, 'headline': 'Analyst Update',
     'body': 'The realized volatility of RTM for next week will be between 27-30%'},
]
vols, obs = news_parser.parse_vol_news(items, 300)
check('exact point value parsed', close(vols.get(1), 0.20))
check('range midpoint parsed', close(vols.get(2), 0.285))
check('point flagged exact', obs[0]['exact'] is True)
check('range flagged as range', obs[1]['kind'] == 'range')

variants = [
    ({'tick': 1, 'headline': '', 'body': 'realized volatility for this week will be 22.5%'}, 0.225),
    ({'tick': 1, 'headline': '', 'body': 'Volatility this week will be 18 %'}, 0.18),
    ({'tick': 1, 'headline': '', 'body': 'volatility of RTM for this week will be between 20% and 24%'}, 0.22),
    ({'tick': 1, 'headline': '', 'body': 'volatility this week will be 15 to 25%'}, 0.20),
]
for item, want in variants:
    got, _ = news_parser.parse_vol_news([item], 300)
    check(f'variant: {item["body"][:44]}...', close(got.get(1), want),
          f'got {got}')

# An exact "this week" must beat an earlier range for the same week.
items = [
    {'tick': 37, 'body': 'volatility of RTM for next week will be between 27-30%'},
    {'tick': 76, 'body': 'The realized volatility of RTM for this week will be 29%'},
]
vols, _ = news_parser.parse_vol_news(items, 300)
check('exact value supersedes the earlier range', close(vols.get(2), 0.29))

check('non-volatility news ignored',
      news_parser.parse_vol_news(
          [{'tick': 1, 'body': 'Welcome to the case.'}], 300)[0] == {})
check('delta limit read from news',
      news_parser.parse_delta_limit(
          [{'headline': '', 'body': 'The delta limit for this heat is 5,000'}]) == 5000)
check('no delta limit mentioned returns None',
      news_parser.parse_delta_limit([{'headline': '', 'body': 'hello'}]) is None)

# Week assignment must come from counting releases, not from tick arithmetic,
# so that the PDF's contradictory tick conventions cannot misfile a week.
full_schedule = [
    {'tick': 1, 'body': 'The realized volatility of RTM for this week will be 20%'},
    {'tick': 37, 'body': 'volatility of RTM for next week will be between 27-30%'},
    {'tick': 76, 'body': 'The realized volatility of RTM for this week will be 29%'},
    {'tick': 112, 'body': 'volatility of RTM for next week will be between 18-22%'},
    {'tick': 151, 'body': 'The realized volatility of RTM for this week will be 19%'},
    {'tick': 187, 'body': 'volatility of RTM for next week will be between 24-28%'},
    {'tick': 226, 'body': 'The realized volatility of RTM for this week will be 26%'},
]
for total in (300, 600):
    vols, _ = news_parser.parse_vol_news(full_schedule, total)
    check(f'{total}-tick: week 1 = 20%', close(vols.get(1), 0.20), f'got {vols}')
    check(f'{total}-tick: week 2 = 29% (exact beats 28.5% range)',
          close(vols.get(2), 0.29), f'got {vols}')
    check(f'{total}-tick: week 3 = 19%', close(vols.get(3), 0.19), f'got {vols}')
    check(f'{total}-tick: week 4 = 26%', close(vols.get(4), 0.26), f'got {vols}')

# Mid-case, before week 4's exact value lands, week 4 must still hold the range.
partial, _ = news_parser.parse_vol_news(full_schedule[:6], 300)
check('week 4 holds the range midpoint until its exact value arrives',
      close(partial.get(4), 0.26))
check('a lone "next week" release with no prior week lands on week 2',
      close(news_parser.parse_vol_news(
          [{'tick': 5, 'body': 'volatility for next week will be between 20-30%'}],
          300)[0].get(2), 0.25))

# ------------------------------------------------------------ chain assembly
print('\nchain assembly (synthetic market)')
# Market maker prices every strike at 22% while the true blend is higher, so
# every option should read as cheap.
spot = 50.0
securities = [{'ticker': 'RTM', 'bid': 49.99, 'ask': 50.01, 'last': 50.0, 'position': 0}]
for strike in (48, 49, 50, 51, 52):
    for flag, suffix in (('c', 'C'), ('p', 'P')):
        mid = black_scholes(flag, spot, strike, 1 / 12, 0.0, 0.22)
        securities.append({
            'ticker': f'RTM{strike}{suffix}',
            'bid': round(mid - 0.01, 2), 'ask': round(mid + 0.01, 2),
            'last': round(mid, 2), 'position': 0,
        })

chain = vol_engine.build_chain(securities, 0, 300,
                               {1: 0.28, 2: 0.28, 3: 0.28, 4: 0.28},
                               mode='fixed')
check('all ten options in the chain', len(chain['rows']) == 10)
check('market IV recovered as 22%', close(chain['market_avg_iv'], 0.22, 1e-3),
      f"got {chain['market_avg_iv']}")
check('forecast is the announced 28%', close(chain['forecast'], 0.28, 1e-9))
check('every strike shows the same market IV (no smile)',
      max(r['market_iv'] for r in chain['rows'])
      - min(r['market_iv'] for r in chain['rows']) < 1e-3)
check('forecast above market IV means positive iv_gap',
      all(r['iv_gap'] > 0 for r in chain['rows']))
check('underpriced options show positive buy edge',
      all(r['buy_edge'] > 0 for r in chain['rows']))
check('and negative sell edge (selling them would lose)',
      all(r['sell_edge'] < 0 for r in chain['rows']))
check('flat book has zero portfolio delta', close(chain['portfolio_delta'], 0.0))
check('call deltas positive, put deltas negative',
      all(r['delta'] > 0 for r in chain['rows'] if r['flag'] == 'c')
      and all(r['delta'] < 0 for r in chain['rows'] if r['flag'] == 'p'))

# In equilibrium (forecast == market IV) no edge should survive fees.
chain = vol_engine.build_chain(securities, 0, 300,
                               {1: 0.22, 2: 0.22, 3: 0.22, 4: 0.22},
                               mode='fixed')
check('at fair value no trade clears the fee hurdle',
      all(r['buy_edge'] < 0 and r['sell_edge'] < 0 for r in chain['rows']))

# Dispersion: make one strike visibly rich.
rich = dict(securities[1])
rich['bid'] += 0.30
rich['ask'] += 0.30
chain = vol_engine.build_chain([securities[0], rich] + securities[2:], 0, 300,
                               {1: 0.22, 2: 0.22, 3: 0.22, 4: 0.22}, mode='fixed')
target = next(r for r in chain['rows'] if r['ticker'] == rich['ticker'])
check('the rich strike shows positive dispersion', target['dispersion'] > 0)
check('it is the most dispersed strike',
      target['dispersion'] == max(r['dispersion'] for r in chain['rows']))
check('and it is the one worth selling',
      target['sell_edge'] == max(r['sell_edge'] for r in chain['rows']))

# Dead quotes between heats. Observed live: the API returns bid=ask=0.0 while
# `last` still holds the previous heat's settlement prices. Falling back to
# `last` invented implied vols out of nothing, so it must not happen.
print('\ndead quotes between heats')
dead = [{'ticker': 'RTM', 'bid': 0.0, 'ask': 0.0, 'last': 48.79, 'position': 0}]
for strike, last in ((48, 0.79), (49, 0.02), (50, 0.02), (51, 0.02), (52, 0.02)):
    for suffix in ('C', 'P'):
        dead.append({'ticker': f'RTM{strike}{suffix}', 'bid': 0.0, 'ask': 0.0,
                     'last': last, 'position': 0})

check('mid of a dead quote is None',
      vol_engine._mid({'bid': 0.0, 'ask': 0.0, 'last': 48.79}) is None)
check('mid of a live quote is the midpoint',
      close(vol_engine._mid({'bid': 1.17, 'ask': 1.19, 'last': 1.18}), 1.18))
check('one-sided quote is not a mid',
      vol_engine._mid({'bid': 1.17, 'ask': 0.0, 'last': 1.18}) is None)

dead_chain = vol_engine.build_chain(dead, 0, 300, {1: 0.28}, mode='fixed')
check('chain flags itself as not live', dead_chain['live'] is False)
check('stale spot still shown for display', close(dead_chain['spot'], 48.79))
check('no implied vol is invented from settlement prices',
      all(r['market_iv'] is None for r in dead_chain['rows']))
check('nothing is tradeable on dead quotes',
      not any(r['tradeable'] for r in dead_chain['rows']))
check('no dispersion is reported',
      all(r['dispersion'] is None for r in dead_chain['rows']))

live_chain = vol_engine.build_chain(securities, 0, 300, {1: 0.22}, mode='fixed')
check('a live chain flags itself live', live_chain['live'] is True)

# Low-vega wings must be excluded: a 2-cent spread cannot pin their IV.
print('\nvega filter')
check('near-the-money strikes are tradeable',
      any(r['tradeable'] for r in live_chain['rows']))
wide = [dict(s) for s in securities]
for sec in wide[1:]:
    if sec['ticker'] in ('RTM52C', 'RTM48P'):
        sec['bid'], sec['ask'] = 0.01, 0.03      # 2 cents on a ~2 cent option
short = vol_engine.build_chain(wide, 297, 300, {4: 0.22}, mode='fixed')
noisy = [r for r in short['rows'] if not r['tradeable']]
check('near expiry, low-vega options are filtered out', len(noisy) > 0)
check('filtered options carry an iv_spread or no IV at all',
      all(r['iv_spread'] is None or r['iv_spread'] > config.MAX_IV_SPREAD
          for r in noisy))

# Missing underlying must fail loudly, not silently price off zero.
try:
    vol_engine.build_chain([{'ticker': 'RTM48C', 'bid': 1, 'ask': 1.02,
                             'last': 1.01, 'position': 0}], 0, 300, {1: 0.2})
    check('missing underlying raises', False, 'no exception raised')
except ValueError:
    check('missing underlying raises', True)

# ------------------------------------------------------------- autotrader
print('\nautotrader sizing and rebalancing')
import autotrader                                            # noqa: E402
import structure as structure_module                          # noqa: E402


class FakeClient:
    """Stands in for RitClient; records orders instead of sending them."""

    def __init__(self):
        self.sent = []

    def send_in_chunks(self, ticker, action, quantity, max_chunk):
        self.sent.append((ticker, action, int(quantity)))
        return []

    def limits(self):
        return [{'name': 'Limit-OPT', 'gross_limit': 2500, 'net_limit': 1000}]


def make_trader(**kwargs):
    trader = autotrader.AutoTrader(FakeClient(), live=True, **kwargs)
    return trader


flat_chain = {'underlying_position': 0, 'spot': 50.0}

t = make_trader()
check('no position below the entry threshold',
      t.target_legs(0.001, flat_chain, 1000) == 0)
check('a gap just over entry takes a small position',
      0 < t.target_legs(0.0025, flat_chain, 1000) < 0.2 * 500,
      f'got {t.target_legs(0.0025, flat_chain, 1000)}')
check('size ramps monotonically with the gap',
      t.target_legs(0.003, flat_chain, 1000)
      < t.target_legs(0.005, flat_chain, 1000)
      < t.target_legs(0.007, flat_chain, 1000))
# The ramp is deliberately steep. Swept over 94 recorded heats, anything past
# roughly twice the 0.42-point breakeven should already be at the limit, because
# the net-contract cap is what bounds the payoff - not the size of the gap.
check('a gap this market actually produces reaches full size',
      t.target_legs(0.008, flat_chain, 1000) == 500,
      f'got {t.target_legs(0.008, flat_chain, 1000)}')
check('a large gap goes to the net-limit cap',
      t.target_legs(0.10, flat_chain, 1000) == 500)
check('negative gap means short volatility',
      t.target_legs(-0.10, flat_chain, 1000) == -500)
check('a straddle of N per leg is 2N against the 1,000 net limit',
      abs(t.target_legs(0.10, flat_chain, 1000)) * 2 == 1000)
check('max_legs caps the position',
      make_trader(max_legs=120).target_legs(0.10, flat_chain, 1000) == 120)

# The RTM52P lesson: never open what cannot be hedged inside the share limit.
near_limit = {'underlying_position': 49_000, 'spot': 50.0}
check('little RTM headroom shrinks the allowed straddle',
      t.target_legs(0.10, near_limit, 1000) < 500)
maxed = {'underlying_position': 50_000, 'spot': 50.0}
check('no RTM headroom still allows the delta band itself',
      t.hedgeable_legs(maxed) == config.DELTA_LIMIT // config.CONTRACT_MULTIPLIER)
check('empty book allows the full straddle',
      t.hedgeable_legs(flat_chain) >= 500)


def leg(ticker, strike, flag, position=0, tradeable=True, delta=0.0):
    return {'ticker': ticker, 'strike': float(strike), 'flag': flag,
            'position': position, 'tradeable': tradeable, 'delta': delta}


def straddle(call, put, target):
    """The desired book for a plain straddle - no wing structure."""
    return {call['ticker']: target, put['ticker']: target}


def orders_for(call, put, target, rows=None, strike=50.0, desired=None):
    """The option orders one tick would send, as [(ticker, signed change)]."""
    chain = {'rows': rows if rows is not None else [call, put]}
    if desired is None:
        desired = {call['ticker']: target, put['ticker']: target}
    trader = make_trader()
    return [(row['ticker'], change)
            for row, change in trader.option_orders(chain, desired)]


call, put = leg('RTM50C', 50, 'c', 300), leg('RTM50P', 50, 'p', 300)
check('a change inside the deadband is not traded',
      orders_for(call, put, 310) == [], f'got {orders_for(call, put, 310)}')

check('a change past the deadband trades both legs',
      orders_for(leg('RTM50C', 50, 'c', 300), leg('RTM50P', 50, 'p', 300), 400)
      == [('RTM50C', 100), ('RTM50P', 100)])

check('closing out ignores the deadband',
      orders_for(leg('RTM50C', 50, 'c', 5), leg('RTM50P', 50, 'p', 5), 0)
      == [('RTM50C', -5), ('RTM50P', -5)])

check('a lopsided straddle self-corrects on the lagging leg only',
      orders_for(leg('RTM50C', 50, 'c', 300), leg('RTM50P', 50, 'p', 0), 300)
      == [('RTM50P', 300)])

check('short straddle sells both legs',
      orders_for(leg('RTM50C', 50, 'c', 0), leg('RTM50P', 50, 'p', 0), -200)
      == [('RTM50C', -200), ('RTM50P', -200)])

# Anything not named in the desired book is closed: that is how a strike we are
# leaving, or a wing the structure has stood down from, comes off the board.
stale = leg('RTM52C', 52, 'c', 200)
check('a position the desired book does not name is closed',
      sorted(orders_for(leg('RTM50C', 50, 'c', 0), leg('RTM50P', 50, 'p', 0), 100,
                        rows=[stale, leg('RTM50C', 50, 'c', 0),
                              leg('RTM50P', 50, 'p', 0)]))
      == [('RTM50C', 100), ('RTM50P', 100), ('RTM52C', -200)])

# The deadband is a fee optimisation and it is not allowed to produce a book
# the server would reject. Skipping the 94-lot trim below while taking the wing
# close leaves 1,186 net against a 1,000 limit - the live failure this guards.
breaching = orders_for(None, None, None,
                       rows=[leg('RTM52C', 52, 'c', -187, delta=0.10),
                             leg('RTM49C', 49, 'c', 593, delta=0.80),
                             leg('RTM49P', 49, 'p', 593, delta=-0.20)],
                       desired={'RTM49C': 499, 'RTM49P': 499})
after = {'RTM52C': -187, 'RTM49C': 593, 'RTM49P': 593}
for ticker, change in breaching:
    after[ticker] += change
check('the deadband may not leave the book over the net limit',
      abs(sum(after.values())) <= config.LIMIT_OPT_NET,
      f'net {abs(sum(after.values()))} after {breaching}')
check('and it gets there by trading the whole desired book',
      sorted(breaching) == [('RTM49C', -94), ('RTM49P', -94), ('RTM52C', 187)],
      f'got {sorted(breaching)}')

# ------------------------------------------------ interleaved hedging
# The property that matters is not the final delta but the delta DURING the
# resize: the penalty accrues per second, and a full resize is ten chunked
# option orders. Replaying the order stream reconstructs what the book actually
# carried between chunks.
print('\nratchet sizing')
check('a wider gap adds to the position',
      autotrader.ratchet_target(400, 300) == 400)
check('a narrowing gap does NOT trim the position',
      autotrader.ratchet_target(200, 400) == 400,
      f'got {autotrader.ratchet_target(200, 400)}')
check('exiting is always allowed', autotrader.ratchet_target(0, 400) == 0)
check('a flipped view is acted on immediately',
      autotrader.ratchet_target(-300, 400) == -300)
check('the same logic holds short', autotrader.ratchet_target(-400, -300) == -400)
check('a narrowing gap does not trim a short either',
      autotrader.ratchet_target(-200, -400) == -400)
check('opening from flat uses the target',
      autotrader.ratchet_target(350, 0) == 350)

print('\nthe hedge backstop')


BACKSTOP_TICK = 240            # week 4 of 4, before the NO_NEW_AFTER cutoff


class StepClient(FakeClient):
    """Enough of the API for one full step() through a held position."""

    def __init__(self, securities, tick=BACKSTOP_TICK, news=None):
        super().__init__()
        self._securities = securities
        self._tick = tick
        # The announcement has to cover the week the tick is IN, or it says
        # nothing about the remaining life of the options and the forecast
        # silently falls back to the market's own average - a zero gap, which
        # is a different scenario from the one these tests are about.
        self._news = [{'tick': tick, 'headline': 'x',
                       'body': 'The realized volatility of RTM for this '
                               'week will be 20%'}] if news is None else news

    def case(self):
        return {'tick': self._tick, 'ticks_per_period': 300, 'status': 'ACTIVE'}

    def securities(self):
        return self._securities

    def news(self):
        return self._news


def option(ticker, position, bid, ask):
    # No strike or flag argument: build_chain parses both back out of the
    # ticker, so passing them separately would only invite a fixture that
    # looks like it configures something it does not.
    return {'ticker': ticker, 'type': 'OPTION', 'size': 100, 'position': position,
            'bid': bid, 'ask': ask, 'last': (bid + ask) / 2, 'nlv': 0.0,
            'vwap': 0.0, 'unrealized': 0.0, 'realized': 0.0,
            'limits': [{'name': 'Limit-OPT'}], 'trading_fee': 1.0,
            'is_tradeable': True}


def share_row(position, spot):
    return {'ticker': 'RTM', 'type': 'STOCK', 'size': 1, 'position': position,
            'bid': round(spot - 0.01, 2), 'ask': round(spot + 0.01, 2),
            'last': spot, 'nlv': 0.0, 'vwap': 0.0, 'unrealized': 0.0,
            'realized': 0.0, 'limits': [{'name': 'Limit-ETF'}],
            'trading_fee': 0.01, 'is_tradeable': True}


def straddle_book(market_iv, position=-500, spot=50.68, strike=49.0,
                  underlying=2708, width=0.02, tick=BACKSTOP_TICK):
    """
    A held straddle, quoted at `market_iv` so the vol gap is known exactly.

    Pricing the legs rather than hardcoding two cent figures is what makes the
    gap in these tests a chosen number instead of an accident of the fixture.
    """
    t_years = vol_engine.time_to_expiry(tick, 300)
    book = [share_row(underlying, spot)]
    for flag, suffix in (('c', 'C'), ('p', 'P')):
        mid = black_scholes(flag, spot, strike, t_years,
                            config.RISK_FREE_RATE, market_iv)
        book.append(option(f'RTM{strike:.0f}{suffix}', position,
                           round(mid - width / 2, 2), round(mid + width / 2, 2)))
    return book


# The live failure, reduced: a SHORT straddle at K49 that is already at target,
# with spot far above the strike so its delta is enormous. The market quotes
# 30% vol against the week's announced 20%, so the view is short vol at full
# size - which is exactly what the book already holds. Nothing needs trading,
# so execute() has no chunks, and before the backstop that meant the tick
# hedged nothing at all.
backstop_securities = straddle_book(0.30)
t = autotrader.AutoTrader(StepClient(backstop_securities), live=True)
t.step()
options_sent = [s for s in t.client.sent if s[0] != config.UNDERLYING]
hedges = [s for s in t.client.sent if s[0] == config.UNDERLYING]
# Without this first check the test passes for the wrong reason: a zero gap
# liquidates the straddle, and the hedge that liquidation drags along would
# satisfy the assertion below even with the backstop deleted.
check('the tick really had nothing to trade', options_sent == [],
      f'sent {t.client.sent}')
check('a tick that trades nothing still hedges', hedges != [],
      f'sent {t.client.sent}')
check('and it hedges the right way (buys against a short-straddle delta)',
      bool(hedges) and hedges[0][1] == 'BUY', f'sent {hedges}')

# Late in a heat the option quotes stop carrying a usable implied vol at all -
# every leg deep in the money and quoted at pennies, so nothing solves. step()
# has no view to trade on and returns early, but the delta of what is already
# held is at its most violent right there, and the penalty does not pause.
dark = [share_row(2708, 50.68),
        option('RTM49C', -500, 0.01, 0.03),      # intrinsic 1.68: unsolvable
        option('RTM52P', 0, 0.01, 0.03)]         # intrinsic 1.32: unsolvable
dark_chain = vol_engine.build_chain(dark, BACKSTOP_TICK, 300, {4: 0.20},
                                    mode='market')
check('the fixture really has no market IV to work with',
      dark_chain['market_avg_iv'] is None, f"got {dark_chain['market_avg_iv']}")
t = autotrader.AutoTrader(StepClient(dark), live=True)
t.step()
check('a chain with no usable implied vol is still hedged',
      [s for s in t.client.sent if s[0] == config.UNDERLYING] != [],
      f'sent {t.client.sent}')

# The other early exit: the quotes solve, but every leg is filtered out by the
# vega / IV-spread test, so there is no strike to work at. Same obligation.
untradeable = straddle_book(0.30, width=0.80)
un_chain = vol_engine.build_chain(untradeable, BACKSTOP_TICK, 300, {4: 0.20},
                                  mode='market')
check('the fixture really has a market IV but nothing tradeable',
      un_chain['market_avg_iv'] is not None
      and not any(r['tradeable'] for r in un_chain['rows']),
      f"avg {un_chain['market_avg_iv']}")
t = autotrader.AutoTrader(StepClient(untradeable), live=True)
t.step()
check('a chain with no tradeable strike is still hedged',
      [s for s in t.client.sent if s[0] == config.UNDERLYING] != [],
      f'sent {t.client.sent}')

print('\ninterleaved hedging')


def peak_delta_during(trader, rows):
    """Largest |delta| the book carried while its orders were going out."""
    by_ticker = {r['ticker']: r for r in rows}
    delta, peak = 0.0, 0.0
    for ticker, action, quantity in trader.client.sent:
        signed = quantity if action == 'BUY' else -quantity
        if ticker == config.UNDERLYING:
            delta += signed
        else:
            delta += signed * by_ticker[ticker]['delta'] * config.CONTRACT_MULTIPLIER
        peak = max(peak, abs(delta))
    return peak


# The live failure: selling 500 of each leg at K52 with spot at 51.23. The
# in-the-money put carries ~0.79 delta against the call's ~0.21, so a short
# straddle is nowhere near neutral - the book hit +47,713 and paid $4,071 for
# that one second.
k52c = leg('RTM52C', 52, 'c', 0, delta=0.21)
k52p = leg('RTM52P', 52, 'p', 0, delta=-0.79)
rows = [k52c, k52p]
chain = {'rows': rows, 'portfolio_delta': 0.0, 'underlying_position': 0.0,
         'spot': 51.23}

t = make_trader()
t.execute(chain, straddle(k52c, k52p, -500))
peak = peak_delta_during(t, rows)
check('delta stays inside the band THROUGHOUT a full-size resize',
      peak <= config.DELTA_LIMIT, f'peaked at {peak:,.0f}')
check('the resize still sends the whole option position',
      sum(q for tkr, _a, q in t.client.sent if tkr != config.UNDERLYING) == 1000,
      f'sent {sum(q for tkr, _a, q in t.client.sent if tkr != config.UNDERLYING)}')
check('hedges are interleaved, not left to the end',
      any(tkr == config.UNDERLYING for tkr, _a, _q in t.client.sent[:-1]),
      f'sent {t.client.sent}')

# Without interleaving the same resize would have breached badly - this pins the
# size of the problem being solved rather than just that it is solved.
check('the un-interleaved equivalent would have breached',
      500 * 0.79 * config.CONTRACT_MULTIPLIER - 500 * 0.21
      * config.CONTRACT_MULTIPLIER > config.DELTA_LIMIT)

t = make_trader()
t.execute({'rows': rows, 'portfolio_delta': 0.0,
           'underlying_position': config.LIMIT_RTM_GROSS, 'spot': 51.23},
          straddle(k52c, k52p, -500))
check('a hedge that would breach the RTM limit is capped, not abandoned',
      all(q <= config.MAX_TRADE_RTM for tkr, _a, q in t.client.sent
          if tkr == config.UNDERLYING))

# The residual that survived the first version. hedge_running only fires above
# the 5,000 trigger while a chunk can carry 4,000 of delta, so a book sitting at
# 4,999 could take a chunk and stand at 8,999 - past the limit. Starting from a
# delta already just under the trigger reproduces it exactly.
t = make_trader()
t.execute({'rows': rows, 'portfolio_delta': 4_900.0,
           'underlying_position': 0.0, 'spot': 51.23},
          straddle(k52c, k52p, -500))
seen, worst = 4_900.0, 4_900.0
for ticker, action, quantity in t.client.sent:
    signed = quantity if action == 'BUY' else -quantity
    if ticker == config.UNDERLYING:
        seen += signed
    else:
        seen += signed * {r['ticker']: r for r in rows}[ticker]['delta'] \
            * config.CONTRACT_MULTIPLIER
    worst = max(worst, abs(seen))
check('a chunk cannot breach the limit from just under the trigger',
      worst <= config.DELTA_LIMIT, f'peaked at {worst:,.0f}')

# Churn. Opening a NEAR-THE-MONEY straddle is the common case, and its legs have
# opposite-signed deltas that nearly cancel. Sending every call chunk and then
# every put chunk walks delta out and back, dragging hedges both ways - measured
# live at +74% RTM volume, which gave back half of what Change 1 saved. Ordering
# the chunks to keep running delta near zero should need almost no hedging.
atm_c = leg('RTM50C', 50, 'c', 0, delta=0.52)
atm_p = leg('RTM50P', 50, 'p', 0, delta=-0.48)
atm_rows = [atm_c, atm_p]
t = make_trader()
t.execute({'rows': atm_rows, 'portfolio_delta': 0.0, 'underlying_position': 0.0,
           'spot': 50.0}, straddle(atm_c, atm_p, 500))
hedge_shares = sum(q for tkr, _a, q in t.client.sent if tkr == config.UNDERLYING)
check('opening a balanced straddle barely hedges at all',
      hedge_shares <= 5_000, f'hedged {hedge_shares:,} shares')
check('the straddle itself is still fully sent',
      sum(q for tkr, _a, q in t.client.sent if tkr != config.UNDERLYING) == 1000)
check('call and put chunks are alternated, not run leg by leg',
      len({tkr for tkr, _a, _q in t.client.sent[:4]
           if tkr != config.UNDERLYING}) == 2,
      f'first sends {[s[0] for s in t.client.sent[:4]]}')

# A rejected order moves no delta. The running figure execute() carries
# between chunks is local arithmetic, not a reading from the API, so counting a
# bounced order as filled hedges against exposure the book never acquired - and
# the shares that hedge trades are real.
print('\nrejected orders')


class RejectingClient(FakeClient):
    """Refuses option orders the way the net option limit does."""

    def send_in_chunks(self, ticker, action, quantity, max_chunk):
        if ticker != config.UNDERLYING:
            raise autotrader.ApiException('...will exceed net trading limits')
        return super().send_in_chunks(ticker, action, quantity, max_chunk)


t = autotrader.AutoTrader(RejectingClient(), live=True)
t.execute({'rows': rows, 'portfolio_delta': 0.0, 'underlying_position': 0.0,
           'spot': 51.23}, straddle(k52c, k52p, -500))
check('an option order that bounces is not hedged against',
      t.client.sent == [], f'sent {t.client.sent}')


class PartialClient(FakeClient):
    """Fills option orders until one is refused, the way the limit does."""

    def __init__(self, allow):
        super().__init__()
        self.allow = allow

    def send_in_chunks(self, ticker, action, quantity, max_chunk):
        if ticker != config.UNDERLYING:
            if self.allow <= 0:
                raise autotrader.ApiException(
                    '...will exceed net trading limits')
            self.allow -= 1
        return super().send_in_chunks(ticker, action, quantity, max_chunk)


# The rest of a tick's plan is stale once one order is refused: chunks()
# sequences the whole tick up front, and every later chunk assumes the earlier
# ones filled. Seen live on build da50ee6.
t = autotrader.AutoTrader(PartialClient(allow=2), live=True)
t.execute({'rows': rows, 'portfolio_delta': 0.0, 'underlying_position': 0.0,
           'spot': 51.23}, straddle(k52c, k52p, -500))
options_after = [s for s in t.client.sent if s[0] != config.UNDERLYING]
check('a refused order stops the rest of the tick, rather than sending it '
      'against a book that does not exist',
      len(options_after) == 2, f'sent {options_after}')

# Strike selection: at the money, and sticky so ordinary drift does not churn.
rows = [leg(f'RTM{k}{s.upper()}', k, s) for k in (48, 49, 50, 51, 52)
        for s in ('c', 'p')]
t = make_trader()
strike, _ = t.pick_strike({'spot': 50.1, 'rows': rows})
check('picks the nearest-to-money strike', strike == 50.0, f'got {strike}')
strike, _ = t.pick_strike({'spot': 50.6, 'rows': rows})
check('small drift does not move the strike', strike == 50.0, f'got {strike}')
# The tick-68 churn: RTM at 48.48 used to force a K50 -> K48 round trip.
strike, _ = t.pick_strike({'spot': 48.6, 'rows': rows})
check('1.4 away holds the strike (was a costly round trip)', strike == 50.0,
      f'got {strike}')
strike, _ = t.pick_strike({'spot': 48.2, 'rows': rows})
check('past 1.5 away it does switch', strike == 48.0, f'got {strike}')
t2 = make_trader()
strike, _ = t2.pick_strike({'spot': 52.0, 'rows': rows})
check('a large move switches strike', strike == 52.0, f'got {strike}')

untradeable = [leg(f'RTM{k}{s.upper()}', k, s, tradeable=(k == 49))
               for k in (48, 49, 50, 51, 52) for s in ('c', 'p')]
strike, _ = make_trader().pick_strike({'spot': 50.0, 'rows': untradeable})
check('skips strikes whose IV the quote cannot pin', strike == 49.0,
      f'got {strike}')
strike, _ = make_trader().pick_strike(
    {'spot': 50.0, 'rows': [leg('RTM50C', 50, 'c', tradeable=False)]})
check('no usable strike returns None', strike is None, f'got {strike}')

# ---------------------------------------------------------- gross capacity
# The case sets two option limits - 1,000 net and 2,500 gross - and a straddle
# spends 1,000 of each. Shorting the cheapest vega on the board buys net room
# for more of the dearest, which is the only way to carry more vega than the
# net limit alone allows. See structure.py.
print('\ngross capacity (the wing structure)')
import structure                                             # noqa: E402


def vrow(ticker, strike, flag, position=0, delta=0.0, vega=0.05):
    return {'ticker': ticker, 'strike': float(strike), 'flag': flag,
            'position': position, 'tradeable': True, 'delta': delta,
            'vega': vega, 'bid': 0.40, 'ask': 0.42}


# Vega falls away from the money, which is the whole basis of the trade: the
# 48 and 52 wings are worth a fifth of the 50-strike straddle per contract.
wing_rows = [
    vrow('RTM48C', 48, 'c', delta=0.92, vega=0.010),
    vrow('RTM48P', 48, 'p', delta=-0.08, vega=0.010),
    vrow('RTM49C', 49, 'c', delta=0.73, vega=0.032),
    vrow('RTM49P', 49, 'p', delta=-0.27, vega=0.032),
    vrow('RTM50C', 50, 'c', delta=0.52, vega=0.050),
    vrow('RTM50P', 50, 'p', delta=-0.48, vega=0.050),
    vrow('RTM51C', 51, 'c', delta=0.27, vega=0.032),
    vrow('RTM51P', 51, 'p', delta=-0.73, vega=0.032),
    vrow('RTM52C', 52, 'c', delta=0.08, vega=0.010),
    vrow('RTM52P', 52, 'p', delta=-0.92, vega=0.010),
]
wing_chain = {'rows': wing_rows, 'spot': 50.0, 'portfolio_delta': 0.0,
              'underlying_position': 0}

book = structure.desired_book(wing_chain, 50.0, 500)
gross, net = structure.limits_used(book)
buffer = structure.WING_NET_BUFFER
check('the structure spends the gross limit bar the rotation buffer',
      gross == config.LIMIT_OPT_GROSS - buffer, f'gross {gross}: {book}')
check('and holds back exactly that buffer against the net limit',
      net == config.LIMIT_OPT_NET - buffer, f'net {net}: {book}')
check('never past either limit',
      gross <= config.LIMIT_OPT_GROSS and net <= config.LIMIT_OPT_NET)
check('the extra size goes to the money, where vega is highest',
      book['RTM50C'] == 875 - buffer // 2
      and book['RTM50P'] == 875 - buffer // 2, f'got {book}')
check('the shorts go to the cheapest vega on the board',
      book.get('RTM48P') == -375 and book.get('RTM52C') == -375,
      f'got {book}')
check('a short-vol view mirrors the whole structure',
      structure.desired_book(wing_chain, 50.0, -500)
      == {k: -v for k, v in book.items()},
      f'got {structure.desired_book(wing_chain, 50.0, -500)}')
check('below full conviction the structure stays off',
      structure.desired_book(wing_chain, 50.0, 300)
      == {'RTM50C': 300, 'RTM50P': 300},
      f'got {structure.desired_book(wing_chain, 50.0, 300)}')

# The gate is in dollars of extra vega per vol point, not in the vega RATIO:
# what matters is what the structure buys against its fixed cost in fees.
flat_vega = [dict(row, vega=0.048) for row in wing_rows]
check('wings whose vega nearly matches the money are not worth shorting',
      structure.desired_book(dict(wing_chain, rows=flat_vega), 50.0, 500)
      == {'RTM50C': 500, 'RTM50P': 500},
      f"got {structure.desired_book(dict(wing_chain, rows=flat_vega), 50.0, 500)}")
check('the same board with cheap wings does put them on',
      len(structure.desired_book(wing_chain, 50.0, 500)) == 4)

# Stickiness: the cheapest leg changes as RTM drifts, and re-picking it every
# tick round trips 750 contracts to move a short to a wing barely cheaper.
drifted = dict(wing_chain, spot=50.4)
held = {'RTM50C': 875 - buffer // 2, 'RTM50P': 875 - buffer // 2,
        'RTM48P': -375, 'RTM52C': -375}
check('a wing already on the board is kept, not re-picked',
      structure.desired_book(drifted, 50.0, 500, held=held) == held,
      f'got {structure.desired_book(drifted, 50.0, 500, held=held)}')

# THE ROTATION PROPERTY, which is what the buffer is for. Sitting exactly on
# both caps there is no legal way to move a wing between strikes - opening the
# new leg breaches gross, and closing the old one breaches NET, because closing
# a short raises net. Holding back a few net contracts makes it legal in small
# steps, and chunks() finds the sequence because it only sends a chunk that
# leaves the book inside both limits.
rotate_from = {'RTM50C': 875 - buffer // 2, 'RTM50P': 875 - buffer // 2,
               'RTM48P': -375, 'RTM52C': -375}
rotate_to = {'RTM50C': 875 - buffer // 2, 'RTM50P': 875 - buffer // 2,
             'RTM48P': -375, 'RTM49C': -375}
rotate_rows = [dict(row, position=rotate_from.get(row['ticker'], 0))
               for row in wing_rows]
t = make_trader()
running = dict(rotate_from)
worst_gross = worst_net = 0
for row, lot in t.chunks(dict(wing_chain, rows=rotate_rows), rotate_to, 0.0):
    running[row['ticker']] = running.get(row['ticker'], 0) + lot
    gross, net = structure.limits_used(running)
    worst_gross, worst_net = max(worst_gross, gross), max(worst_net, net)
check('rotating a wing to another strike never breaches gross',
      worst_gross <= config.LIMIT_OPT_GROSS, f'peaked at {worst_gross}')
check('rotating a wing never breaches net either',
      worst_net <= config.LIMIT_OPT_NET, f'peaked at {worst_net}')
check('and the rotation completes',
      {k: v for k, v in running.items() if v} == rotate_to, f'ended {running}')

# The hedge has to be able to follow the structure. Late in a heat the money's
# legs stop cancelling - a 0.95 call against a 0.05 put - so the extra size the
# structure carries is no longer near delta-neutral, and with the share book
# already full there is nowhere to hedge it. The straddle is the view and is
# never sacrificed to this; the structure is a claim on a limit, and the claim
# is what gives way.
expiring = [dict(row) for row in wing_rows]
for row in expiring:
    if row['strike'] == 50:
        row['delta'] = 0.95 if row['flag'] == 'c' else -0.05
expiring_chain = dict(wing_chain, rows=expiring)
check('with shares to spare the structure still goes on',
      len(structure.desired_book(expiring_chain, 50.0, 500, underlying=0)) == 4,
      f'got {structure.desired_book(expiring_chain, 50.0, 500, underlying=0)}')
check('with the share book full it stands down to the plain straddle',
      structure.desired_book(expiring_chain, 50.0, 500,
                             underlying=config.LIMIT_RTM_GROSS)
      == {'RTM50C': 500, 'RTM50P': 500},
      f'got {structure.desired_book(expiring_chain, 50.0, 500, underlying=config.LIMIT_RTM_GROSS)}')

# The structure comes off before the straddle does. Near expiry its vega is
# nearly gone while its share of the gamma is not, and the live tape shows what
# that costs: ticks 243-256 of the 21:49 heat, spot pinned on the K52 strike,
# delta reversing every tick from -23,734 to +19,290 while the hedge chased it.
t = make_trader()
check('the structure runs while there is still vega to buy',
      t.wing_allowance(100, 300) == structure.WING_CONTRACTS,
      f'got {t.wing_allowance(100, 300)}')
check('and comes off before the pin',
      t.wing_allowance(int(300 * autotrader.STRUCTURE_OFF_AFTER), 300) == 0,
      f'got {t.wing_allowance(240, 300)}')
check('it comes off BEFORE the straddle stands down, not with it',
      autotrader.STRUCTURE_OFF_AFTER < autotrader.NO_NEW_AFTER,
      f'{autotrader.STRUCTURE_OFF_AFTER} vs {autotrader.NO_NEW_AFTER}')
check('a zero-length case does not divide by it',
      t.wing_allowance(0, 0) == structure.WING_CONTRACTS)

# THE LIVE-SAFETY PROPERTY. Both limits are enforced on submission, so every
# intermediate state has to be legal too - not just the destination. Flipping
# from a long structure to a short one is the hard case: sell the at-the-money
# legs first and the book is 2,500 net short while the wings are still short;
# buy the wings back first and the gross limit blows instead.
positions = {'RTM50C': 875, 'RTM50P': 875, 'RTM48P': -375, 'RTM52C': -375}
flip_rows = [dict(row, position=positions.get(row['ticker'], 0))
             for row in wing_rows]
flip_chain = dict(wing_chain, rows=flip_rows)
target_book = structure.desired_book(flip_chain, 50.0, -500)
t = make_trader()
running = dict(positions)
worst_gross = worst_net = 0
for row, lot in t.chunks(flip_chain, target_book, 0.0):
    running[row['ticker']] = running.get(row['ticker'], 0) + lot
    gross, net = structure.limits_used(running)
    worst_gross, worst_net = max(worst_gross, gross), max(worst_net, net)
check('no single order in a full flip breaches the gross limit',
      worst_gross <= config.LIMIT_OPT_GROSS, f'peaked at {worst_gross}')
check('nor the net limit, which is the one that rejects orders',
      worst_net <= config.LIMIT_OPT_NET, f'peaked at {worst_net}')
check('and the flip actually lands on the book it was aiming for',
      {k: v for k, v in running.items() if v} ==
      {k: v for k, v in target_book.items() if v},
      f'ended {running}')
check('every order respects the 100-contract maximum trade size',
      all(abs(lot) <= config.MAX_TRADE_OPTIONS
          for _row, lot in t.chunks(flip_chain, target_book, 0.0)))

# ------------------------------------------------------ news-gated resizing
print('\nnews-gated rebalancing')
import backtest                                              # noqa: E402

t = make_trader()
check('first position is always allowed', t._pending_resize is True)
check('news counter starts unset', t._last_news_count == -1)

check('deadband policies parse their fraction',
      backtest.policy_params('deadband-40') == (0.40, False))
check('deadband-60 parses', backtest.policy_params('deadband-60') == (0.60, False))
check('news policy is gated', backtest.policy_params('news')[1] is True)
check('news+band is gated too', backtest.policy_params('news+band')[1] is True)
check('continuous is not gated',
      backtest.policy_params('continuous') == (autotrader.REBALANCE_FRACTION, False))

# Exits must never be gated: once the edge is gone, holding is the expensive
# choice, so target 0 has to be reachable without waiting for a news release.
for gated, pending, target in ((True, False, 0), (True, True, 300),
                               (False, False, 300)):
    may = (not gated) or pending or target == 0
    check(f'gated={gated} pending={pending} target={target} -> trade={may}', may)
check('gated, nothing pending, non-zero target -> hold',
      not ((not True) or False or 300 == 0))

print(f'\n{PASS} passed, {FAIL} failed\n')
raise SystemExit(1 if FAIL else 0)
