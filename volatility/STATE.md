# STATE — Volatility Trading Case

Work tracker for the RITCx Volatility Trading Case build.
**Competition: Saturday 19 September 2026, Carnegie Mellon.** Last updated 17 Sep 2026.

---

## THE GOAL — read this first

**Average P&L across all 10 competition heats of $250,000–$300,000.**
**This is non-negotiable.** Guiding theme: **"go big or go home (within
reason)."** Optimise for expected P&L, not for safety — but stay inside the
case's hard limits, which are enforced server-side and reject offending orders
outright.

Current standing: **+$111,054 average** over 83 clean live heats from the
overnight run, best +$251,691. With the recalibrated sizing ramp (18 Sep) the
backtest mean is **$134,980**, i.e. roughly **$125,000 live-equivalent**.

### SCORING: the goal is a TWO-TRADER number

Confirmed 18 Sep from the official RITCx CMU page. The volatility case requires
**two students per team**, and *"the penalty-adjusted final P&L of all
participating members of a team are **summed** to form a dollar value of the
team P&L."* Teams are then **ranked** per heat and the ranks averaged; the two
cases are weighted 50/50.

So **any headline figure for this case is a two-trader total.** Running the bot
on both team members' accounts doubles the team number for zero extra work, and
is by far the cheapest lever available. At $125k live-equivalent per trader that
is ~$250k of team P&L. Absolute P&L only matters relative to other teams.

Revised down from an initial $400k after the ceiling arithmetic below. The
gross-capacity lever that was supposed to supply 1.55x of it **was built and
tested and does not work** — see below.

### The ceiling, and why it is where it is

The binding constraint is the **1,000-contract NET option limit**. A full-size
ATM straddle is exactly 1,000 net contracts. Its vega:

```
vega per share per vol point  = S · φ(d₁) · √T
                              ≈ 50 × 0.399 × 0.289  ≈ 0.0576   (T = 1/12)
per contract                  ≈ $5.76
at the 1,000-contract limit   ≈ $5,760 per volatility point
```

So **$400,000 requires capturing ~69 volatility points of convergence per
heat.** Observed gaps have been 7–11 points across four news releases. Even
capturing every gap perfectly and in full lands nearer **$150–200k**.

That calculation was once thought conservative, on the grounds that it omits
**gamma scalping** — a delta-hedged straddle earns the integral of gamma against
(realised − implied) *variance*, not just vega × the vol gap. **That has now
been tested and there is no gamma edge in this case.** Tightening the hedge band
to harvest it loses money monotonically, down to −$13,596 (t −7.7) hedging flat
every tick. The reason is in the case design: the maker prices at a **zero
variance risk premium**, so realised volatility equals implied in expectation.
Only the information asymmetry pays, and that is the vega trade.

So the $5,760-per-vol-point ceiling is the real one, and it binds. The route to
$250–300k is **not** a stack of multipliers on this model — every multiplier
that was supposed to supply them has been measured and is absent. It is:

```
per-trader, tuned model            ~$125,000 live-equivalent
x2  both team members run the bot  ~$250,000 TEAM P&L   <- the actual lever
```

That is the whole route, and it is a logistics step rather than an engineering
one. Anything beyond it needs a **materially different model**, not a better
tuned one.

Hard constraint regardless: **do not breach position limits reaching for this.**
Offending orders are rejected outright, and heat 1 showed an unhedgeable book
costs $2,002/second.

### Levers — ALL TESTED 18 Sep over 95 recorded heats (`sweep.py`)

Only one of these was real. The others are settled; do not spend time on them
again without new information.

1. ~~**Use gross capacity, not just net.**~~ **BUILT, TESTED, DEAD.** Long 1,750
   ATM against short 750 wings lost roughly half the P&L on the first heat. The
   1.55x came from one observation at tick ~190, late in a heat, when vega has
   collapsed toward the money. Early in a heat — when positions are largest and
   gaps widest — `sigma*sqrt(T)` is large relative to the 48–52 strike range and
   the wings are **not** low-vega. Measured min/max vega by tick: 0.59 at t=25,
   0.61 at t=100, 0.38 at t=150, 0.17 at t=200, 0.03 at t=275. At a 0.6 ratio
   the structure trades 75% more contracts for ~30% more vega and needs ~2 vol
   points just to cover the extra fees; at the 0.93 ratio seen live early in a
   heat it needs ~10. Observed gaps are 1–5. **Do not rebuild this.**
2. **Reach full size sooner.** ✅ **THE ONE THAT WORKED.** `FULL_SIZE_VOL_EDGE`
   0.025 → 0.008 and `ENTRY_VOL_EDGE` 0.008 → 0.002 is worth **+$19,554 a heat,
   paired t = 9.5**, and lifts the worst heat from $715 to $14,940. Broad
   plateau — everything with `FULL_SIZE <= 0.008` scores within $700.
3. **Gamma-aware hedge band.** Marginal. A wider band (trigger 6,900 → target
   6,500) is worth **+$4,307, t = 3.3** — mostly saved commission, since delta
   inside ±7,000 is free anyway. Not yet applied to this model.
4. **Trade later into the heat.** Nothing there. `NO_NEW_AFTER` 0.90 → 1.00 is
   −$2,069 (t = −1.0).
5. ~~**Forecast quality.**~~ **DEAD, and this was the big surprise.** See the
   oracle results below: we already capture **94%** of what a trader who knew
   the maker's future quotes would earn. There is essentially nothing left here.

### The oracle — how much money is actually in a heat (`volatility_research/oracle.py`)

Two perfect-foresight traders, same fills, fees, hedging and limits, 95 heats:

| | mean | best heat | heats ≥ $500k |
|---|---|---|---|
| tuned model | $132,377 | $326,764 | 0 / 95 |
| knows every weekly sigma at t=1 | $101,618 | $266,254 | 0 / 95 |
| **knows the maker's future quote** | $141,527 | $318,087 | **0 / 95** |

Two conclusions, both important:

- **This strategy's ceiling is ~$320k on the best heat ever recorded**, and we
  are already at 94% of it. Tuning and forecasting are exhausted.
- **Knowing the true volatility is WORSE than what we do now.** Not a bug. The
  maker never converges to true integrated variance because it does not know
  future weeks either. The edge is predicting **where the maker's quote is
  going**, not what vol truly is — which is exactly what `market` mode does, and
  why it beats both `fixed` and a perfect sigma oracle.

Ruled out cheaply on the same day: `limit_order_rebate` is **0.0**, so there is
no liquidity rebate and passive market-making earns nothing. Limits are hard —
the API reports `is_enforce_trading_limits: true` and the PDF says
*"participants will not be able to exceed them."*

**Other teams reportedly clear $1m in a heat (≈$500k per trader), which is above
this strategy's perfect-foresight ceiling. They run a materially different
model. Finding it is the open problem** — not tuning this one.

**Test every one of these with `backtest.py` on the recorded heats before
trading it live.** Seven recordings exist. Same-heat comparison is the only
valid test; NLV across different heats is meaningless (identical code produced
−$15k and +$113k).

## Topology phase

`VERIFIED AGAINST A LIVE PRACTICE HEAT.` 99/99 offline tests pass.

Read-path confirmed live on 17 Sep against a running heat: case, securities,
news, implied vols, greeks, blend and hedge arithmetic all correct.
**The order-submission path has still never been exercised** — every live test
so far has been read-only or `--dry-run`.

---

## Verified intent

Decision-support tooling for the Volatility Trading Case. Reads market data,
identifies mispriced options, and manages the delta hedge.

**Option entry is manual** (keypress). **The delta hedge is automatic.** That
asymmetry is the point: sizing and strike choice are judgement calls, hedging
is a closed-form number on a per-second penalty clock.

Out of scope, by instruction: **the Algorithmic ETF Arbitrage Case.** Nothing in
this build touches it.

---

## Case facts (verified from the official PDF, not assumed)

Source: `RITCx-Volatility Trading Case.pdf`, downloaded from
`rotmanfrtl.github.io` via the RITCx CMU event page.

| Item | Value |
|---|---|
| Underlying | RTM, non-dividend ETF |
| Options | `RTM48C/P` … `RTM52C/P` — strikes 48, 49, 50, 51, 52 |
| Style | European, cash-settled at `max(0, S−K)` / `max(0, K−S)` |
| Case length | 5 minutes = 300s = 1 month = 20 days; year = 240 days |
| Time to expiry | `(ticks_left / ticks_total) × 1/12` years |
| Risk-free rate | 0% (instructor may change) |
| Multiplier | 100 shares per contract |
| Spread | **always exactly 2 cents**, unlimited depth, no price impact |
| Fees | $0.02/share RTM, $2.00/contract options |
| Delta limit | ±7,000; penalty `(|delta| − 7000) × $0.10` **per second** |
| Position limits | RTM 50,000 gross/net; options 2,500 gross / 1,000 net |
| Max trade size | 10,000 shares / 100 contracts |
| News | week start: **exact** sigma for the running week; mid-week: **range** for next week |

### Why the mispricing exists

The market maker prices with Black-Scholes at a **zero variance risk premium**,
so in equilibrium implied vol must equal realized vol. The maker is *less
informed than you* and only learns the new sigma gradually after each weekly
shock. **The edge is a known information asymmetry with a decay clock — speed is
the whole game.** The news is not adversarial; it is accurate by construction.

---

## Decisions taken, and why

| Decision | Choice | Rationale |
|---|---|---|
| Option entry | **Automated** (revised twice) | Started manual. A human cannot fill 1,000 contracts in 100-lot orders inside a decaying edge window, and the edge appears in bursts right after each weekly vol shock. `autotrader.py` |
| Structure | **ATM straddles, not single options** | A straddle is ~delta-neutral by construction and ATM carries the most vega, so it is pure vol exposure that cannot trap itself outside the delta band. See the RTM52P episode below |
| **Delta hedge** | **Automatic** (revised) | Penalty is $0.10/delta/**second**. One 100-lot of a 0.50-delta option = 5,000 delta instantly; 3,000 over the limit = **$300/sec**. Gamma drifts delta even when idle. Hedging needs no judgement, so a human in the loop adds nothing and costs seconds. Hysteresis (5,000→2,000) bounds churn |
| Pricing signal | Implied vol vs blended forecast | The case is built to reward exactly this |
| Market price input | **Mid**, execute at touch, net of fees | 2-cent spread is symmetric around the maker's model price; `last` is stale/one-sided |
| Vol forecast | **Time-weighted RMS blend over remaining life** | Under BS with deterministic piecewise-constant vol, price depends only on *integrated variance*. Exact result, not an approximation |
| Hedging | Into the band with a buffer | Trigger 5,000 → target 2,000. Penalty only bites outside ±7,000; flattening to zero burns $0.02/share repeatedly |
| Tail-week prior | **UNRESOLVED — to be measured** | Genuinely empirical. Three modes behind a config switch; decide with `record.py` + `replay.py` on real heats |

### The blend, concretely

At t=1 with week 1 announced at 20% and weeks 2–4 unknown at ~28%:

```
naive (current week only): 20.0%
correct (integrated var):  sqrt((75·0.20² + 225·0.28²)/300) = 26.2%
```

A 6-point error, in the direction that makes you sell cheap vol right before
three high-vol weeks — and it's worst at t=1 when the position is largest.

### Two independent edges

1. **Level** — whole IV curve vs forecast. Needs the forecast to be right.
2. **Dispersion** — all ten options share one expiry and one pricing model with
   no smile, so **every strike must show identical IV**. Any spread between
   strikes is the maker's own inconsistency. **This does not require the
   forecast to be right at all** — it's the lower-risk money. Bound to `d`.

---

## Live findings (17 Sep, practice server)

Confirmed against a running heat: tickers are `RTM48C`…`RTM52P`, case length is
**300 ticks** (the PDF *prose* was right, the sample news table was not),
r = 0%, spread is exactly 2 cents on every option.

**Put-call parity holds exactly** — at each strike the call and put solve to the
identical IV (48C/48P both 24.75%, 50C/50P both 26.67%). Independent
confirmation the IV solver and time-to-expiry are right.

### Three real bugs, all found only by running live

**1. The opening news release has a different format and was silently dropped.**
At tick 1 the case says:

> "The current risk free rate is 0%. RTM … its **current annualized realized
> volatility is 28%**. This simulation consists of 20 trading days that are each
> 15 ticks in length."

It never says "this week", so the parser skipped it — losing week 1 entirely and
shifting every later release into the wrong week. Now parsed explicitly, before
the generic matcher (that same sentence contains "rate is 0%" and "15 ticks",
which the generic patterns would happily misread as a volatility).

Verified afterwards: weeks parse as 28% / 22% / 20%, and the tick-35 range
(22.5% midpoint) correctly predicted the tick-74 exact value of 22% for week 2.

Also confirms 20 days × 15 ticks = 300, so a week is 75 ticks — as assumed.

**2. Implied-vol dispersion was mostly penny-rounding, not signal.**
The 2-cent spread means wildly different things depending on vega. Measured live:

| Option | Vega | IV noise from the 2¢ quote |
|---|---|---|
| RTM48C | 0.0319 | 0.63% |
| RTM50C | 0.0126 | 1.10% |
| RTM51C | 0.0035 | 2.63% |
| RTM52C | 0.0006 | 4.08% |
| RTM52P | 0.0006 | **17.54%** |

Cross-strike dispersion read 3.62% — *smaller than the noise band of the options
generating it*. The high-vega strikes clustered within 0.18%, i.e. the maker
really does quote one flat IV, exactly as the case says. The `d` key would have
sold RTM52C, a 2-cent option whose $2/contract fee is 100% of its premium.

Fixed with `MAX_IV_SPREAD`: options whose quote width cannot pin IV to better
than 1.5 vol points are excluded from trading, from dispersion, and from the
market average. **The market average is now vega-weighted** — it was a plain
mean, and in `market` mode it is the prior for unannounced weeks, so the noise
was leaking directly into the forecast.

**3. Dead quotes between heats produced confident fiction.**
With the case `STOPPED`, the API returns `bid=ask=0.0` while `last` still holds
the previous heat's settlement prices. `_mid()` fell back to `last` and invented
a full IV surface — RTM52C at 11.75%, vega-weighted market average 6.30%.

`_mid()` now returns `None` without a two-sided quote, `build_chain` reports a
`live` flag, and both the dashboard and overlay refuse to show any signal or
hedge while quotes are dead.

### Live fees are HALF the published ones

The API reports `trading_fee` of **$0.01/share** on RTM and **$1.00/contract**
on options. The case PDF states $0.02 and $2.00. Using the PDF numbers set the
edge hurdle twice too high and silently rejected profitable trades. Fees are now
read per-security from the API, with the PDF values only as a fallback.

Also read live: `api_orders_per_second` 50, `execution_delay_ms` 0,
`is_enforce_trading_limits` true. Rate is a non-constraint; fills are instant;
orders breaching a limit are rejected outright rather than partially filled.

### The RTM52P episode — why the trader uses straddles

Observed on a live practice heat: a 500-lot of `RTM52P` (delta **−0.987**)
contributed **−49,342** portfolio delta by itself, on an option with almost no
vega. Combined with 45,794 RTM shares held against a 50,000 limit, there was
**not enough headroom left to hedge it** — the account sat at −27,016 delta,
paying **$2,002/second**, with no legal trade back inside the band.

Two rules came out of it, both now enforced in code:

1. **Choose options for vega, not dollar edge.** A deep ITM option is nearly
   pure stock exposure. The `MAX_IV_SPREAD` filter already excludes these.
2. **Never open a position you could not hedge.** `hedgeable_legs()` caps size
   by the remaining RTM headroom plus the delta band.

Straddles satisfy both structurally, which is why the trader uses them rather
than picking whichever single option shows the largest edge.

## First live automated heat (17 Sep, `logs/auto_233406.log`)

Ran `autotrader.py --live` for a full 300-tick practice heat. **Final NLV
−$15,154** on 152 order batches. The order path works — fills land, positions
update — but the result decomposes damningly:

| | |
|---|---|
| The volatility bet itself | **+$10,236** |
| Friction (commission + spread) | **−$25,390** |
| **Net** | **−$15,154** |

**The view was right and was traded to death.** 10,540 option contracts and
215,495 RTM shares churned, against a position that never exceeded ~500 lots.

### Three bugs, all found only by trading live

**1. Hedged a stale delta — this is where the money went.**
`hedge()` used `chain['portfolio_delta']`, computed *before* the same tick's
option trades. It hedged the pre-trade figure, then corrected a tick later:

```
23:37:35  SENT BUY  12,369 RTM
23:37:36  SENT SELL 12,381 RTM     <- undone one second later
23:37:37  SENT BUY   9,088 RTM
```

That one-tick lag accounts for essentially all 215,495 shares. Fixed at the time
with `projected_delta()`, which hedged the position the tick was about to *have*.

**Superseded.** Change 1 replaced the projection with `hedge_running()` inside
`execute()`: the hedge now goes out *between* the option chunks, so there is
nothing left to project, and every tick ends with an unconditional backstop
hedge. `projected_delta()` and the old `hedge()` were deleted on 18 Sep once the
last caller went away — one hedging entry point, no parallel implementation to
drift.

**2. Opened the new strike before closing the old one.** The net option limit is
enforced on submission, so carrying both straddles at once got the new one
rejected outright:

```
tick 236  K52  target +303
  REJECTED BUY 303 RTM52C: ...will exceed net trading limits...
  SENT SELL 404 RTM50C      <- only closes after the rejection
```

`close_other_strikes()` now runs before `rebalance()`.

**3. Sizing ramp unreachable, strike switching too eager.** Full size needed a
4.0-point gap; the heat produced 1.0–1.8 in `market` mode, so it sized at ~6% of
capacity all heat. Recalibrated from the actual economics — opening a straddle
costs ~$4 against ~$9.60/vol-point of vega, so breakeven is 0.42 points held to
settlement (options cash-settle, no exit cost) and 0.84 closed early. Entry now
0.8, full size 2.5. Strike switch distance widened to 1.5.

### Replay independently confirms the hedge fix

`replay.py` hedges *after* updating positions — i.e. correctly — and every mode
is profitable on the same recorded ticks, where the live run lost money. The
difference between +$34k–89k in replay and −$15k live is almost entirely that
one-tick lag.

## Seven live heats — six consecutive profitable

| Heat | NLV | Friction | Opt contracts | RTM shares | Config |
|---|---|---|---|---|---|
| 1 | −$15,154 | $25,390 | 10,540 | 215,495 | stale-delta hedge (broken) |
| 2 | +$16,734 | $67,130 | 28,444 | 512,117 | hedge fixed, sizing recalibrated |
| 3 | +$40,494 | $28,543 | 11,340 | 293,167 | + anti-churn |
| 4 | +$112,634 | $29,255 | 12,264 | 236,364 | same |
| 5 | +$109,378 | $22,456 | 8,044 | 318,377 | same |
| 6 | +$81,126 | $21,696 | 8,952 | 189,576 | **+ news-gated** |
| 7 | +$95,650 | **$15,397** | **5,092** | 260,639 | news-gated |
| 8 | **+$162,011** | $20,487 | 8,036 | 220,762 | news-gated |
| 9 | +$33,437 | $16,290 | 5,984 | 216,120 | news-gated |
| 11 | +$60,938 | $15,577 | 6,812 | **97,640** | news-gated |
| 12 | +$93,248 | **$13,777** | **4,888** | 200,062 | news-gated |

Mean of the ten since the hedge fix: **+$80,552**.
Mean of the six news-gated heats: **+$87,735**.

(Heat 10 excluded — see below.)

Heat 11 shows the news-gating benefit compounding into the hedge: RTM volume
fell to 97,640 shares on 14 sends, against 200–300k on 30–50 sends previously.
A position that is not being resized every tick does not need re-hedging every
tick either, so share friction dropped to $1,952 — the lowest recorded.

**Variance between heats is enormous and is the dominant fact here.** Heats 8
and 9 ran byte-identical code back to back and returned +$162,011 and +$33,437 —
a 5x spread driven purely by how badly the maker mispriced vol that day. Any
conclusion drawn from a single heat is noise. This is why `backtest.py`
same-heat comparison is the only valid way to evaluate a change, and why the
competition target is framed as a 10-heat average.

### Heat 10 — excluded, but a useful accident

+$6,924, and **not comparable**: the trader was started at tick 233 of 300, past
the tick-270 cutoff for opening new positions, and it **inherited a −23,336
delta book it did not create** (a stale `overlay.py --trade` was left running
with hotkeys armed). Do not average it in.

It did double as an unplanned test of recovery from a broken book, which had
never been checked. The trader hedged the inherited delta immediately, refused
to open anything past the late cutoff, and wound the options flat — going from
−$23k of delta exposure and −$1,634/second in penalties to +$10,715 inside about
forty seconds. `hedgeable_legs` and the late-exit logic both behaved.

## PRE-FLIGHT CHECKLIST — run before every competition heat

```
1. No stray processes. ONLY autotrader.py and record.py should be running.
      Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'"
   Kill any overlay.py --trade or dashboard.py. Two actors on one book is how
   an unhedgeable position happens, and that costs up to $2,002/second.

2. Account flat, NLV 0.

3. python test_engine.py            -> expect 139 passed, 0 failed

4. python autotrader.py --live --verbose

5. Start BEFORE the heat does. It waits for ACTIVE by itself. Joining late
   forfeits the early news releases, which is where the largest gaps appear.

Afterwards:
   python analyze.py logs/<file>.log --nlv <final>
```

Progression: heat 1→2 was the stale-delta hedge fix plus recalibrated sizing.
2→3 was anti-churn (option churn −60%, friction −57%). 5→6 was news-gating,
which cut option *sends* from 82 to 30 and RTM sends from 53 to 21. Heat 7 hit
the lowest friction of any heat at $15,397 on just 18 option sends.

**Do not compare NLV across heats to judge a change.** Identical code produced
−$15k and +$113k on different heats; opportunity varies enormously. Only the
same-heat backtest comparison is valid.

## Rebalancing policy — settled on seven heats

Hypothesis (from watching live runs): the money is made at each news release and
the tick-by-tick trading in between only bleeds. Tested with `backtest.py`,
which replays the real straddle strategy over recorded heats:

| Policy | Mean P&L | Worst heat |
|---|---|---|
| `continuous` | $77,544 | $18,981 |
| `deadband-40` | $86,366 | $19,002 |
| `deadband-60` | $84,458 | $15,845 |
| `deadband-80` | $65,472 | $13,321 |
| **`news`** | **$94,688** | **$22,990** |
| `news+band` | $92,937 | $20,835 |

`news` won in **every individual heat**, and has the best worst case. Raising the
deadband instead captures roughly a third of the benefit; 80% goes backwards.

**Only ~$5k of the gain is saved commission.** The larger part: as the maker
converges toward our forecast the gap shrinks, and continuous rebalancing reacts
by *trimming* — selling out of the very convergence that pays us. Holding to the
next release captures the whole move.

Current configuration, after the 18 Sep sweep over 95 heats (do not change
without a same-heat backtest showing why):

```
TAIL_VOL_MODE       market
ENTRY_VOL_EDGE      0.002     FULL_SIZE_VOL_EDGE  0.008
EXIT_VOL_EDGE       0.003     GAP_SMOOTHING       0.15
REBALANCE_MIN       25        REBALANCE_FRACTION  0.25
STRIKE_SWITCH_DISTANCE 1.5    NO_NEW_AFTER        0.90
```

Everything except the two sizing values was swept and left alone because it was
already optimal to within noise: exit edge, gap smoothing, strike-switch
distance, rebalance fraction and the hedge band all moved the mean by less than
$1.5k. The earlier tuning was at its local optimum.

First two live heats on the new ramp: **+$156,776** and **+$27,304** — the same
enormous heat-to-heat variance as always, which is why the 95-heat paired
comparison is the evidence and two live heats are not.

### Anti-churn changes that produced heat 3

- **Gap smoothing** (`GAP_SMOOTHING = 0.15`). The weekly vol shock is a step
  change and passes through; the cent-rounding jitter that was being traded does
  not. Reaches ~80% of a step in ~10 ticks, far faster than the maker converges.
- **Proportional deadband** (`REBALANCE_FRACTION = 0.25`). A flat 25-contract
  threshold is 5% of a 500-lot book — hair-trigger. The ramp maps 1.7 vol points
  onto 0–500 contracts, so a half-point flicker was resizing by ~150 lots.

### The signal is genuinely predictive, not a directional artifact

Heat 3 had volatility **falling** (30 → 24 → 11 → 20), `mean truth` −3.10%: the
market was over-pricing vol, so selling was correct — the opposite of heats 1
and 2, where vol rose monotonically.

| Mode | Agreement | Correlation (heat 3, falling vol) |
|---|---|---|
| `market` | 65% | +0.82 |
| `fixed` | 72% | +0.93 |
| `persist` | 65% | +0.78 |

All strongly positive in *both* directions of vol regime. This retires the
earlier worry that a permanently-long-vol bias was being flattered by one-sided
heats. **Inverting the trades would lose money** — checked directly after that
hypothesis was raised.

Volatility scale confirmed twice (`diagnose.py`): announced vs realised ratios
average 1.001 and 0.972. The news numbers are annualised exactly as assumed.
Per-week deviation (e.g. 24% announced vs 17% realised) is sampling error on a
75-observation vol estimate, not model error.

## Tail-week mode — `market` stays, and here is why

Replayed across all three recorded heats (871 ticks):

| Mode | Mean P&L | Worst heat | Mean RMSE |
|---|---|---|---|
| `market` | **$71,627** | $39,538 | 5.57% |
| `fixed` | $49,321 | $34,387 | **3.73%** |
| `persist` | $59,963 | **$41,510** | 6.06% |

The measures still split: `fixed` forecasts most accurately, `market` earns
most. Keeping `market`, for three reasons:

1. **`fixed` depends on an arbitrary constant.** Its 25% prior happens to sit
   near the average vol of all four heats (~20–21%). If Saturday's heat runs
   unusually quiet or wild, `fixed` breaks and `market` adapts automatically.
2. **`market` is the principled construction** — it deviates from the maker only
   where we genuinely know more, which is exactly the case's stated edge.
3. **It is the only mode validated live**, across four heats, most recently
   +$112,634.

Note the replay P&L column does **not** test the straddle strategy — `replay_pnl`
still trades single options on dollar edge. Treat RMSE and the correlation
figures as the strategy-independent measures; treat replay P&L as indicative.

| Mode | Replay P&L | Forecast RMSE | Bias | Trades | Fees |
|---|---|---|---|---|---|
| `market` | **$89,148** | 8.11% | −6.83% | 408 | $19,684 |
| `fixed` | $34,387 | **4.19%** | −3.02% | 94 | $7,620 |
| `persist` | $47,381 | 8.40% | −7.09% | 550 | $26,182 |

The two measures disagree — P&L favours `market`, accuracy favours `fixed` — so
**this is not settled.** Three cautions before reading anything into it:

- **`fixed` is the mode most at risk of being flattered by one heat.** It assumes
  25% for unknown weeks, and this heat ran 11% → 16% → … → 35%, averaging near
  25%. A quiet heat would punish it badly.
- **All three show negative bias** (forecast below realised). This heat's vol rose
  monotonically, so any forecast lags it. Probably heat-specific, not a model flaw.
- **Replay's P&L does not test the straddle strategy.** `replay_pnl` still trades
  individual options on dollar edge, which is not what `autotrader.py` now does.
  The RMSE column is strategy-independent and therefore the more meaningful one.

**Do not lock `TAIL_VOL_MODE` until 3–4 more heats are recorded.**

## Contradiction found in the official PDF

The prose says 300 ticks with 75-tick weeks numbered from t=1. The sample news
table puts t=150 in week 2 and t=450 in week 4 — only possible for a 600-tick
case indexed from t=0. Checked all four table rows: only the 0-indexed reading
fits, so `week_of_tick` uses it.

**Mitigation:** news is *not* assigned to weeks by tick arithmetic at all.
There's exactly one "this week" release per week, so the Nth release *is* week N
by construction. The first release anchors the counter by tick (safe — the two
readings only disagree *at* a boundary, and agree everywhere else). Verified to
give identical week assignments for both 300- and 600-tick cases.

Case length is read live from `ticks_per_period`, never hardcoded.

---

## Files

| File | Role |
|---|---|
| `config.py` | All tunables. SPEC values vs TUNABLE values marked separately |
| `rit_api.py` | REST wrapper. Fails loudly and named; never silently returns a default |
| `news_parser.py` | Vol announcements → `{week: sigma}`. Permissive regex by design |
| `vol_engine.py` | IV solve, greeks, integrated-variance blend, edges, hedging. Pure — sends nothing |
| `dashboard.py` | Live console. Manual execute on keypress |
| `autotrader.py` | **Automated trader.** ATM straddles sized by the vol gap, auto-hedged. Dry run unless `--live` |
| `overlay.py` | Always-on-top panel that floats over the RIT client. Display-only unless `--trade` |
| `api_key.txt` | RIT API key. **Gitignored** — per-machine credential |
| `record.py` | Read-only capture of a live heat → `heats/*.jsonl` |
| `replay.py` | Scores every mode on identical recorded ticks |
| `sweep.py` | Parameter sweeps over the whole recording library. Paired per-heat comparison, bootstrapped 10-heat averages |
| `test_engine.py` | 139 offline checks, no server needed |

`../volatility_research/` is a separate fork for higher-risk experiments, so this
model is never put at risk. It shares these recordings rather than copying
them. `volatility_research/FINDINGS.md` records why no punt dial exists: even pinned at
the full 1,000-contract limit for every tick of every heat, the per-heat
standard deviation is $58,463 against this model's $58,854. The limits cap
variance as hard as they cap the mean.

`RITCx Volatility Trading Case base script-REST API.py` is Rotman's original
template, kept for reference. It is **not** on the execution path.

### Bugs in Rotman's base script (reference only, not inherited)

- `helper['required_hedge']/position - position` — the hedge size is
  dimensionally wrong. RTM delta is 1/share, so it's `required_hedge − position`.
- Prices off `last`, not mid, in a market with a guaranteed live two-sided quote.
- Fixed `vol = 0.25`; ignores the news entirely.
- Chained assignment (`df['col'].iloc[i] = …`) — silently stops writing under
  pandas copy-on-write.
- Assumes RTM is row 0.
- Its `ticker[3:5]` strike parse *is* correct for `RTM48C`. I wrongly called this
  a bug earlier; retracted.

---

## Blast radius

Self-contained in `volatility/`. Touches no other case.

Outward effects come only from `dashboard.py`, and there are two kinds:

- **Option orders** — require a keypress.
- **Hedge orders in RTM** — sent automatically, once per tick, only when
  `|delta| > 5,000`. Bounded by the 50,000-share RTM limit and by the
  hysteresis band. Never touches options.

Both are disabled entirely under `--dry-run`; auto-hedging alone is disabled
with `--manual-hedge` or the `a` key. `record.py` and `replay.py` are read-only.

---

## THE PLAN — target $150,000 per trader, agreed 18 Sep

**Goal, set by Hayden: no less than $150,000 average P&L per trader across 10
heats.** Not a team figure — per trader. Team P&L is the sum of both members, so
this is $300k of team P&L if both run the bot.

Why that number: last year's RITCxCMU results, filtered to the teams that
actually built a model (`volatility_research/compete.py`). The field as a whole
is not the benchmark — 27% of team-heats lost money and the median team-heat was
$40,555. The teams that built something:

| rank | team | per trader / heat |
|---|---|---|
| 1 | JNID | $263,468 — outlier, 1.8x the next team |
| 2 | TGWK | $132,566 |
| 3 | HUBT | $143,404 |
| 4 | XIAN | $131,286 |
| 5 | ANSC | $128,104 |

We are at roughly **$125,000 live-equivalent**, i.e. rank 5–6. **$150k is the
rank 2–3 threshold.** Two changes close the gap.

### Change 1 — interleave the hedge with the option orders

**Problem.** The trader sends every option order, then the RTM hedge. The delta
penalty accrues per **second**, and a full resize is ten chunked option orders,
so the book carries the entire un-hedged delta for about a second each time.
Measured live: **$86,156 of penalty**, peak |delta| **47,713**. A straddle is
only delta-neutral *at the money*, so a resize at a strike that spot has drifted
away from spikes hard — tick 225 sold 500 RTM52C and 500 RTM52P with spot at
51.23, hit +47,713 delta, and paid $4,071 for that one second.

**Fix.** Send the option orders in chunks and hedge between them, so delta never
accumulates. Recompute the running delta after each chunk rather than once at
the end.

**DONE and CONFIRMED LIVE, 18 Sep — tagged `change1-confirmed`.**

Measured from the trader logs, per heat:

| | original | Change 1 |
|---|---|---|
| delta penalty | $8,381 | **$3,749** |
| share cost | $4,892 | $5,243 |
| peak abs delta | 47,713 | 21,135 |
| hedge shares per contract | 18.4 | **19.0** |

**Net +$4,281 a heat**, not the $7,600 first projected. Residual delta spikes
remain — peak 21,135 still breaches the band — so there is perhaps $3,700 a
heat still on the table here. Left deliberately: Change 2 is worth an order of
magnitude more.

**It got worse before it got better, and the reason is worth remembering.**
The first version saved the penalty and gave half of it straight back, because
`option_orders` emitted every call chunk then every put chunk — so delta walked
out across the call leg dragging hedges with it, then walked back across the
put leg dragging the opposite ones. Hedge volume rose 74%. A straddle's legs
carry opposite-signed deltas that nearly cancel, so the churn was entirely
self-inflicted by ordering. `chunks()` now picks whichever remaining chunk
leaves |delta| smallest.

**The backtest could not see any of this** — it models one hedge per tick, not
the order stream inside a tick. It confidently predicted +$7,591. Only the live
logs showed the regression. `track.py --diagnose` exists so that this class of
bug shows up in ONE heat: hedge volume and delta breaches follow almost
deterministically from the code, while single-heat P&L is worthless at a $58k
standard deviation.

Also fixed on the way: `hedge()` after `execute()` re-hedged the same delta,
because it reads `chain['underlying_position']` — captured before the tick's
share trades and blind to every hedge `execute()` had just sent.

### Change 2 — react faster, and rebalance continuously

**Problem.** Two settings deliberately slow the trader down:
`REBALANCE_ON_NEWS_ONLY = True` holds the position between news releases, and
`GAP_SMOOTHING = 0.15` averages the signal over ~10 ticks. Both were added
because the signal was noisy, and both were correct *for that signal*.

**Evidence they now cost us** (`volatility_research/ceiling.py`) — a perfect
forecast of where the maker's quote is heading, with the execution handicaps
removed:

| policy | horizon | mean |
|---|---|---|
| news-gated | 25 | $153,634 |
| **continuous** | **10** | **$187,029** |
| shipped model | — | $132,377 = **71%** |

So there is **29% of headroom**, short horizons beat long ones, and continuous
beats news-gating *once the signal is good enough*. The earlier claim that we
captured 94% of the ceiling was wrong — that oracle was handed the shipped
configuration's handicaps, so it measured the ceiling of the config, not of the
structure.

**DONE 18 Sep. Outcome: +$1,815 a heat, not the ~$22,000 hoped for.** The
premise was half right — the capture rate really is worse between releases —
but every way of trading more between them loses money. What worked was
changing *how size responds*, not *when we act*.

**Shipped: ratchet sizing.** Size is proportional to the gap, and between news
releases the gap shrinks for one reason only — the maker converging toward our
forecast, i.e. the trade working. Re-sizing on that sells out of the very
convergence that pays us. The gap now sets a **floor** on conviction: add when
it widens, hold when it narrows, leave only when the edge is gone or reversed.
**+$1,815 a heat, paired t = 3.2**, confirmed independently in both backtests.

### Everything else tested and rejected

| idea | result |
|---|---|
| continuous rebalancing | −$4,059 (t −1.9) |
| deadband 40 / 60 | −$4,887 / −$7,293 |
| momentum added to the signal, weight 0.25 | −$8,745 (t −3.9) |
| momentum under ratchet, weight 0.25 | −$8,702 (t −3.8) |
| exit threshold 0.001 / 0.0 (stay in longer) | −$2,175 / −$3,662 |
| carry into expiry | −$5,516 |

**The momentum result is the instructive one.** Over 30,932 tick observations,
the maker's own recent drift is by far the best predictor of its next move —
correlation **0.271**, against **0.124** for the EMA gap we actually trade and
0.031 for the raw gap. It still loses money, and the reason is structural:
momentum rises as the maker converges toward our forecast, so it says "add"
exactly when the *remaining* edge is smallest. **Correlation with the next move
is not the same as profitability.**

The same study killed the idea that smoothing is a lag worth removing: in the
ticks right after a release the EMA predicts far better than the raw gap (0.766
against 0.601), because that is where cent-rounding noise is worst.

### Why there is so little left — size was never the constraint

`sizing_study.py`, over 108 heats:

```
ticks at the full contract limit      60.9%
ticks holding anything                71.1%   -> 86% of in-market ticks are maxed
ticks the sign was right              84.2%
sign right when the move was big      86.7%
```

**We are already pinned at the 1,000-contract limit whenever we hold anything.**
So any "better signal" that merely rescales the gap changes nothing — which is
why the sizing ramp (a sign/threshold change) was worth $19,554 and momentum (a
magnitude change) was worth nothing. Only the *sign* can be improved, and it is
already right 84% of the time.

Nor is the 28.9% of ticks spent flat a mistake: every variant that stayed in
longer lost money. Those ticks genuinely have no edge.

### What $150k would actually require

`reachable.py` blends our forecast toward a perfect 10-tick view of the maker's
next move and reads P&L off the curve:

| blend | sign right | mean P&L |
|---|---|---|
| 0.00 (today) | 69.5% | $128,515 |
| 0.20 | 74.7% | $137,094 |
| 0.35 | 78.6% | $141,126 |
| **0.50** | **81.8%** | **$144,877** |
| 0.75 | 87.7% | $139,830 |
| 1.00 | 100.0% | $135,315 |

Two things worth keeping. **P&L peaks at a 50/50 blend and falls with perfect
foresight** — a pure 10-tick predictor flips the position too often, and our
forecast supplies the stability; the best signal is both. And **even a 50%
perfect signal only reaches $144,877**, short of $150k, on a policy that also
gets the ratchet.

So **$150,000 a heat is not reachable through the signal.** Half of a literally
perfect short-horizon forecast does not get there. The remaining routes are the
residual delta transient (~$3,700 a heat, Change 1 leftovers) and structural
ideas not yet found.

### How results get tracked

Both changes are judged on paired same-heat backtests over the whole recording
library, and on as many live practice heats as can be run before Saturday.
Single live heats prove nothing here — identical code has returned −$15k and
+$162k. `record.py` must be running in a loop; it exits at the end of each heat
and it silently overwrites when given a fixed `--name`.

## Change 3 — spend the gross limit (18 Sep, SHIPPED)

**The idle capacity.** The case sets two option limits and they are not the
same number: 2,500 contracts gross, 1,000 net, with "short positions negate any
long positions" for the net one. A straddle spends 1,000 of each, so **1,500
contracts of gross capacity sat unused in every heat we have ever run.**

Vega is bought by the contract, so that cap is the whole ceiling: at roughly
$5.76 of vega per ATM contract per vol point early in a heat, the net limit
pins the position at about $5,760 a vol point however right we are.

**The structure.** Shorting the cheapest vega on the board buys net room back,
and it is spent on the dearest leg there is — the at-the-money straddle:

```
long  875 ATM calls + 875 ATM puts        net +1,750   gross 1,750
short 375 + 375 of the two cheapest legs  net   -750   gross   750
                                          net  1,000   gross 2,500
```

**+$7,829 a heat over 125 recordings, paired, t 3.7** ($131,300 → $139,129).
P(ten-heat average ≥ $150k) goes from 17.6% to 32.1%.

### Why this was worth re-opening

`volatility_research/FINDINGS.md` had recorded the idea as dead: "It was built
and tested. It lost roughly half the P&L on the first heat." That test was one
heat against a $60k per-heat standard deviation — it could not have resolved an
$8k effect in either direction. The finding was not wrong so much as unmeasured.

### The gate is the finding

Gating on the wing/ATM vega RATIO is gating on the wrong quantity. What the
structure buys is `W * (v_atm - v_wing)` per vol point and what it costs is
about $2 a contract on 2W contracts, fixed. A 0.60 ratio early in a heat buys
$1,096 a vol point; a 0.18 ratio at tick 225 buys $1,282 — so a ratio gate
takes the worse trade and refuses the better one. Gating on the dollars took
t from 3.0 to 4.8 and lifted the worst heat by $38k.

### Three things that did not work

| idea | result |
|---|---|
| symmetric wings, one leg each side of spot | +$1,058 (t 0.8) vs +$6,301 cheapest-first |
| never switch strike, to save the round trip | −$10,669 alone, −$67,500 with the structure |
| riding the free delta band / conviction 0.8 / holding into settlement | all inside noise |

Symmetry looked obviously right — both cheapest legs land on the same side of
spot, which is one-sided short gamma. It costs five sixths of the trade, because
forcing a leg onto the near side forces it onto a leg whose vega is close to the
money's, and the vega SPREAD is the entire point.

### What it cost, and the guard

The hedge has to follow a 1.75x position inside an unchanged 50,000-share
limit. Peak share usage goes from 25,733 to 39,684, worst heat 49,936 — 64
shares of margin. `structure.hedgeable()` stands the structure down when the
shares to hedge it are not there; the straddle is the view and never gives way.
Discounting the straddle cap instead was tried first and costs 35% of the
position in every heat to guard a breach that 125 recordings never produce.

The delta penalty roughly doubles in the backtest ($8,768 → $18,842 a heat).
That figure is the backtest's pessimistic model — it charges one full second of
the whole pre-hedge delta at every resize, which is exactly what Change 1
stopped doing. **The live number is the one to trust, and it is what the current
live run is for.**

## 18 Sep, evening — what the LIVE run taught us that no backtest could

Three findings, in order of how much they change the picture.

### 0. CORRECTION, 20:45 — it is 1.20x, not 1.46x, and 26 live heats say so

The 1.46x below was measured over 91 heats spanning a whole day of different
builds, including ones with known bugs and heats the trader joined half-way
through. Restricted to the 26 heats traded by the frozen build 3532ba7
(`python validate_build.py 180124`):

```
live mean        $138,919
backtest mean    $167,150
ratio                1.20x
correlation          0.98
live sd          $ 85,675
live worst       $ 12,000      losing heats 0/26
```

**Correlation 0.98.** The backtest tracks live almost perfectly heat by heat
and is simply scaled about 20% high. That matters far more than the level:
every paired A/B comparison in this document is a RANKING question, and at 0.98
the ranking is sound. Offline deltas translate at roughly 0.83x, not 0.68x.

And the headline number: **live mean $138,919 over 26 heats, with a standard
error of $16,802 - so a 95% interval of $106k to $172k. Zero losing heats.**
$150k is inside that interval. The ten-heat average, which is what the
competition scores, has a standard deviation of about $27,100.

Read the 1.46x section below as a lesson about pooling across builds, not as a
fact about the model.

### 1. The backtest is 1.46x optimistic — SUPERSEDED, see above

`python sweep.py --validate` over **91 matched heats** (live results.csv against
the recording of the same heat):

```
live mean        $109,017
backtest mean    $159,487
ratio                1.46x
correlation          0.89
```

It RANKS heats well and is biased high. **Every offline delta in this document
is in backtest currency and translates to roughly 0.68x in real money.** A
+$6,000 backtest improvement is about +$4,100 live.

It also means $150k live needs about $219k in backtest terms, which is far
beyond anything measured. Decide the target against live numbers, not these.

Where the gap goes, measured on the same twelve heats:

```
backtest (atomic fills, modelled penalty)         $137,810
replay_trader.py (real trader, order-accurate)    $127,725
  ... minus the measured live penalty (~$6,600)   ~$121,100
live, current build                                ~$99,505
```

So the trader's own order sequencing costs ~$10k against the backtest's
idealisation, the delta penalty ~$6.6k, and **~$20k is still unexplained.**
The open hypothesis is execution TIME: a full-size resize of the structure is
25+ option orders at the 100-contract cap plus hedges between them, each an
HTTP round trip, against a one-second tick. `timing_from_log.py` was written to
measure this from the trader's own timestamps and has NOT yet been run over a
full heat. **That is the first thing to do.**

### 2. The real delta penalty is half what the backtest charges

The backtest charges one second of the whole pre-hedge delta at every resize -
a model of exactly the behaviour interleaved hedging removed. Read off the live
logs with `penalty_from_log.py`:

```
17:47 heat   29 ticks outside the band   worst $2,171/s   ~$11,664
17:54 heat   16 ticks                    worst   $531/s    ~$3,332
18:01 heat   19 ticks                    worst   $765/s    ~$4,844
```

Mean ~$6,600 against the backtest's $14,839.

### 3. The variance is the case, not the model — and the worst heats are fine

`tail_study.py`, 133 recordings. The 13 worst heats against the 13 best:

| | P&L | penalty | friction | contracts | flips | flat |
|---|---|---|---|---|---|---|
| worst | $13,158 | $17,703 | $48,706 | ~21k | 0.0 | 29% |
| best | $277,853 | $14,360 | $50,836 | ~22k | 0.2 | 27% |

**Operationally identical.** Same costs, same turnover, same engagement, no
flip-flopping. The difference is whether the maker mispriced vol that day.
There is no failure mode in the bad heats to fix.

Also worth keeping in view: **4 losing heats out of 133**, worst -$40,716. The
spread is mostly upside dispersion, not downside risk. Per-heat sd is ~$60k
without the structure and ~$74k with it; the ten-heat average - which is what
the competition scores - has sd of roughly $19k and $23k respectively.

Every configuration ever tested lands within $5k of the same sd, including
"always max size" ($58,463) and "conviction only" ($59,427) against a base of
$58,854. Maximum aggression and minimum engagement produce the same spread.

### The process failure, and the guard that now prevents it

`overnight.py` spawns a FRESH trader per heat, so it trades whatever is on disk
when the heat starts. Editing the model while the loop was live put three
unvalidated builds into the market, one of which had a bug measured minutes
later at -$19,941 a heat. It returned **-$35,777 live**.

`overnight.py` now refuses to start, and refuses to spawn each subsequent heat,
while any of the six modules the trader loads has uncommitted edits. It logs
the commit it is trading. **Trade a commit, not a working directory.**

### Ideas measured and rejected this evening

| idea | result |
|---|---|
| taper position by sqrt(T) to cut gamma | -$20,536 (t -7.2) |
| tighter hedge band 3000/1000 | -$1,312 |
| gate on extra gamma carried, best of six | +$446 (t 1.2) |
| stand structure down at 0.60 of the heat | +$3,314 (t 3.0) — **not shipped** |

The last one is the interesting rejection. The surface is not smooth (0.50 is
-$2,599, 0.55 is +$2,556), the tick it prefers sits on the week-3 news
boundary, and the principled version of the same idea - gating on the gamma
actually added - is worth $446. That is calendar fitting, so it stays out.

The general result: the ~$15k of penalty is the price of carrying the vega.
Every way of shrinking the position to avoid it costs more than it saves.

## HOW TO KEEP TESTING WITHOUT ME (18 Sep)

Everything below writes its results to a file, so an unattended run is not
lost. Nothing here sends an order except `overnight.py`.

```
cd volatility
python overnight.py                    # trade + record every heat
                                       #   -> results.csv, logs/, heats/
python overnight.py --summary          # what has been earned so far
python overnight.py --dry-run          # record only, never trades
```

The runner refuses to start if the trader's own code has uncommitted edits.
That is deliberate - see the process failure above. Commit first.

Offline analysis, all safe to run while trading (none of them import into the
live path). Redirect the output, because these print and do not save:

```
cd volatility
python test_engine.py                       # 180 offline checks
python replay_trader.py > ../out_replay.txt # real trader, order-accurate, P&L
python penalty_from_log.py logs/auto_*.log  # what the delta penalty REALLY cost
python timing_from_log.py logs/auto_*.log   # how long a resize takes vs a tick
python sweep.py --validate                  # backtest against live, same heats

cd ../volatility_research                   # results/ is written automatically
python ship_or_not.py | tee results/ship.txt   # is the structure worth it?
python tail_study.py  | tee results/tail.txt   # what the worst heats look like
python guard_check.py | tee results/guard.txt  # the structure's safety gates
python gamma_check.py | tee results/gamma.txt  # late-heat gamma mitigations
```

**The most valuable unattended thing is simply more heats.** Every conclusion
here is limited by sample size: 4 clean live heats on the current build against
a $60k per-heat spread. `overnight.py` left running overnight turns that into
something that can actually settle the 1.46x question.

## Next moves, in order

0. **Run `timing_from_log.py` over a full heat.** The single largest unexplained
   number is the ~$20k a heat between the order-accurate replay and live. If
   resizes routinely overrun a one-second tick, that is where it is going, and
   it would argue for a smaller structure - fewer contracts is fewer orders.
1. ~~Smoke-test against the practice server~~ — **done 17 Sep.** Read path
   verified; three bugs found and fixed (see Live findings).
2. **Exercise the order path once, with tiny size.** Still completely untested.
   Send a 1-lot manually and confirm the fill, the position update, and that the
   delta recomputes. This is now the largest remaining risk.
3. **Record 3–4 practice heats** — `python record.py --name heat1`.
4. **Settle the tail-week prior** — `python replay.py heats/*.jsonl`, then set
   `TAIL_VOL_MODE` in `config.py`. Still genuinely open.
5. **Tune `MIN_EDGE_DOLLARS` and order size** against replayed heats.
6. Confirm the delta limit is ±7,000 on the day — the general RITC rules say it
   can be announced by news instead. Both UIs read and display an announced
   limit if one appears.

## Note on the observed edge

At tick 160 of a live heat the blend read 23.14% against a market IV of 25.55%,
i.e. the maker was pricing **2.4 vol points above** our forecast after the
announced drop to 20% — options rich, sell signal, exactly the lag the case
describes. Encouraging, but a single observation, and week 4 was still unknown
(carried at the market prior). Do not over-read it.

## Open questions

- Is the heat 300 or 600 ticks on the day? Handled dynamically, but worth knowing.
- Does the practice server quote all ten strikes from tick 1?
- Is `r` actually 0%, or has the instructor changed it? (`RISK_FREE_RATE`.)
- How fast does the market maker converge after a shock? Determines how long the
  edge survives and therefore how aggressively to size. **Measurable from a
  recording** — this is the most valuable thing a practice heat can tell us.
