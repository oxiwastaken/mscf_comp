# STATE — Volatility Trading Case

Work tracker for the RITCx Volatility Trading Case build.
**Competition: Saturday 19 September 2026, Carnegie Mellon.** Last updated 17 Sep 2026.

---

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

That one-tick lag accounts for essentially all 215,495 shares. Fixed with
`projected_delta()`, which hedges the position the tick is about to *have*.
Fills are immediate here (`execution_delay_ms: 0`), so the projection is exact.

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

## Four live heats — the strategy works

| | Heat 1 | Heat 2 | Heat 3 | Heat 4 |
|---|---|---|---|---|
| **Final NLV** | −$15,154 | +$16,734 | +$40,494 | **+$112,634** |
| Directional P&L | +$10,236 | +$83,865 | +$69,037 | +$141,889 |
| Friction | $25,390 | $67,130 | $28,543 | $29,255 |
| Option contracts | 10,540 | 28,444 | 11,340 | 12,264 |
| RTM shares | 215,495 | 512,117 | 293,167 | 236,364 |
| Rejections | many | 0 | 0 | 0 |

Heat 1 → 2 was the hedge-lag fix plus recalibrated sizing. Heat 2 → 3 was the
anti-churn work: option churn fell 60% and friction 57% while the directional
edge held. Heat 4 ran the identical configuration and returned +$112,634 on a
+7.25 vol-point gap — the largest mispricing seen so far.

**Friction is now stable at ~$29k regardless of opportunity** (heats 3 and 4:
$28,543 and $29,255) while directional P&L varies with what the market offers.
That is the right shape: cost is controlled, upside tracks the edge.

Configuration that produced heats 3 and 4 (do not change without a reason):

```
TAIL_VOL_MODE       market
ENTRY_VOL_EDGE      0.008     FULL_SIZE_VOL_EDGE  0.025
EXIT_VOL_EDGE       0.003     GAP_SMOOTHING       0.15
REBALANCE_MIN       25        REBALANCE_FRACTION  0.25
STRIKE_SWITCH_DISTANCE 1.5    NO_NEW_AFTER        0.90
```

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
| `test_engine.py` | 87 offline checks, no server needed |

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

## Next moves, in order

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
