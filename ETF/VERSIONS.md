# Strategy versions

Every logged row carries a `risk_model` column naming the version that produced
it, so data from different formulas never gets averaged together. Bump
`RISK_MODEL` in `etf_arb_bot.py` whenever the trading maths changes, and add an
entry here.

Full parameter sets per launch are in `logs/runs.csv`; per-heat threshold
decisions are in `logs/calibration.csv`.

---

## `ritc_x1` — Rotman base template

Exposure counted as `BULL + BEAR + RITC`. Fixed `ARB_THRESHOLD_CAD = 0.07`,
raw price spread with fees never subtracted. Tenders accepted unconditionally.

**Result: −0.0217 net/unit, 19/37 losers, mean −4,420/heat.**

Two defects. The x1 weighting scored a fully hedged book (+25k BULL, +25k BEAR,
−25k RITC) as net 25,000 and froze trading, while the server's own LIMIT-STOCK
read net 0 on the same position. And 0.07 sat at roughly half the ~0.129
break-even, since fees alone are ~0.060 and measured slippage averaged 0.068.

## `ritc_x2` — correct exposure weighting

`BULL + BEAR + 2*RITC` for both net and gross. One RITC is one BULL plus one
BEAR, so it carries two shares of underlying — confirmed by the case PDF, which
specifies a x2 multiplier for the ETF. `MAX_GROSS` cut to 250,000, because the
corrected count grows twice as fast and the old 500,000 cap would have sailed
past the server's 300,000 limit into 0.5/unit fines.

**Result: unfroze trading, but still −9,653/heat — correct exposure maths does
not fix an entry bar that is too low.**

## `ritc_x2_adaptive` — per-direction adaptive threshold

Threshold rebuilt each heat as `fees(FX) + slippage_quantile + margin`, per
direction, with a feedback controller widening after a losing heat.

**Result: net/unit +0.0793, 56/76 winners — but 16 of 31 heats took no trades.**

The estimator ratcheted. Slippage is measured as `expected − realised`, which
scales with the size of the edge taken: raise the bar, only fat spreads qualify,
fat spreads slip more in absolute terms, the estimate rises, the bar rises
again. Margin floored at 0.0 trying to narrow while the slippage term held the
bar at ~0.19. The two terms fought and the estimator won.

## `netbar_fxhedge` — outcome-fitted bar, FX hedge, working tenders

Three changes.

**Bar fitted on net outcome.** Scan candidate bars upward, take the lowest whose
surviving arbs historically net above `TARGET_NET`. No slippage term, so no
feedback loop. `MAX_BASE_STEP` limits movement to 0.01/heat, because the fit can
only judge bars at or below the one that produced its data — every logged arb
cleared the bar in force at the time, so a lower candidate keeps all of them and
always looks profitable. Bars are seeded from `heats.csv` at startup so a
restart cannot jump.

**FX hedged.** Residual USD flattened every cycle. Unhedged it averaged 91,966
and peaked at 1,626,563 against ~30k of arb earnings, swinging NLV by 72,439
within a run — roughly 2.4x the entire arb signal, which made heat P&L useless
as feedback. USD carries zero commission, so this costs only the ~0.0002 spread.

**Tenders fixed.** `POST /tenders/{id}` requires `price` on every accept, and
for a fixed-bid tender it must match the tender price exactly. The base template
omits it precisely when `is_fixed_bid` is true — which is every tender this
server issues — so every accept returned HTTP 500. Worse, the accept ran before
the log write, so the exception unwound the whole cycle and the tender left no
trace: 32 heats and ~7,300 cycles logged zero tenders while ~4 arrived per heat.
Fixed by sending `price` always, logging the tender before any accept attempt,
and isolating the accept so a failure costs one tender rather than the cycle.

Tenders are gated on edge after the unwind fee (`MIN_TENDER_EDGE`) and on
projected exposure against the server caps (`LIMIT_BUFFER`, absolute rather than
a percentage — tenders run 90k–99k units, i.e. deliberately just inside the 200k
net cap, and a 10% safety factor rejected a legal 182,000 outright).

**Result: 6 of 8 tenders captured (75%), ~69,910 USD of edge. Heat 2 ran
+36,504 CAD. Spread arb +0.0541/unit, 17/23 winners.**

## `v5_depthunwind` — depth-sized unwind, instrumented

Unwind chunks sized to visible book depth (`DEPTH_FRACTION` of the top
`DEPTH_LEVELS`) rather than a flat 10,000, and every unwind fill logged to
`trades.csv` with `direction="unwind"` against the pre-trade quote.

Motivation: heat 2's four accepts implied ~43,780 USD of edge and delivered
+36,504 CAD, so ~18% leaked between accept and flat — around 8,000/heat, more
than the entire spread-arb contribution. The previous unwind discarded its fill
data, so that leak could not be attributed between our own price impact and
market drift. This version measures it.

Books are now fetched once per cycle and reused for both pricing and unwind
sizing, so depth awareness costs no extra requests.

`load_arbs` filters to arb rows before grouping — unwind rows carry `leg=0` and
would otherwise be swept into the preceding arb's group, pushing it to four rows
and silently discarding a valid arb from calibration.

## `v6_depthgate` — tender edge priced at chunk VWAP

`tender_safety` priced the unwind at top-of-book. The RITC book holds ~190k
units visible but only ~5-10k at the touch, so a 90k tender quoted against the
touch overstated its edge by roughly 0.26/unit — about 23,000 USD on a single
tender, enough to turn an apparently +0.10 tender into a real loss.

Full-size VWAP is the opposite error. The unwind leaves as ~10k chunks across
many cycles and the book refills between them, so the single 90k sweep that
full-size VWAP assumes never happens. Measured book: bids 194,248 visible
running 25.05 → 24.62, asks 184,875 running 25.06 → 25.31.

The gate now prices at the VWAP of one `UNWIND_CHUNK`, which is what each order
actually experiences. Per-chunk that gap is ~0.035/unit, matching the ~18% leak
observed on the first captured tenders almost exactly.

`tenders.csv` gains `chunk_vwap` and `depth_visible` so the two bases stay
comparable going forward.

## `v7_edgecal` — tender bar recalibrated, float-safe comparison

Two problems surfaced on v6's first heat.

A tender for 84,000 units logged `edge +0.1000 < 0.1` and was declined. The
true value was 0.09999…, so the comparison was correct, but an ~8,400 USD
opportunity died on a rounding artefact and the rounded log display actively
contradicted the decision. Comparison now carries `EDGE_EPSILON`, and edges
print to five decimals with their USD value beside them.

More importantly, v6 silently tightened the bar twice. Pricing the unwind at
chunk VWAP removed ~0.017/unit of phantom edge from the *measurement*, so an
unchanged `MIN_TENDER_EDGE = 0.10` was materially stricter than the 0.10 that
produced heat 2's profitable 75% capture. Lowered to 0.08 to hold the effective
bar where the evidence says it worked: honest pricing should sharpen the
estimate, not quietly cut the acceptance rate.

Measured basis gap across v6's tenders: mean 0.0172/unit, max 0.0700, and
exactly 0.000 whenever the touch holds a full chunk — so it bites only on thin
books, which is the intended behaviour.

## `v8_capacity` — unwind 3x faster, tender bar to 0.05

The binding constraint moved. Under v7 the gate stopped rejecting tenders on
edge and started rejecting them on position limits: six tenders across three
heats were refused with "net would hit", including +0.2350 on 97,000 units
(~22,795 USD) and +0.1700 on 87,000 (~14,790 USD). Total ~78,000 USD, about
26,000 USD/heat, every one of them because an earlier tender was still
unwinding when the next arrived.

The v5 instrumentation finally had enough data to settle the trade-off: 97
unwind chunks at +0.0133/unit mean (+0.0042 median), 12,093 USD total cost.
Unwinding costs 0.0133/unit; being blocked costs 0.09-0.235/unit. Clearing the
book faster wins by roughly 10x even if slippage worsens from taking more
depth per cycle.

`UNWIND_CHUNKS_PER_CYCLE = 3` sends up to three orders per cycle instead of
one, decrementing its view of available depth between them since the book is
only fetched once per cycle. That clears an ~87,000 unit tender in ~3 cycles
rather than ~11.

`MIN_TENDER_EDGE` lowered 0.08 -> 0.05. With the unwind now measured at
0.0133/unit, break-even is ~0.033 after the 0.02 fee, so 0.08 was refusing
profitable tenders: three in three heats at +0.060 to +0.077, worth ~18,800 USD.

Two changes at once, which muddies attribution. They are independent levers
(capacity vs threshold) and both are grounded in the same measurement, so the
cost of waiting a version to separate them was higher than the ambiguity.

## `v9_slipgated` — unwind backs off on realised slippage

v8's three-chunks-per-cycle was too blunt. The RITC book refills *between*
cycles, not within one, so three back-to-back orders simply walk it. Measured
on a single cycle: +0.0316, then +0.1100, then +0.1397 — a 4x escalation,
~0.083/unit against the 0.0133 that one chunk per cycle had been costing.

At ~90,000 units per tender that is ~6,300 USD of extra unwind cost to recover
~26,000 USD/heat of blocking. Still positive, but a far thinner margin than the
"10x" the single-chunk measurement implied, because that measurement did not
extrapolate to chunks taken back to back.

Rather than guess a fixed chunk count, the loop now reads the realised fill and
stops for the cycle once slippage exceeds `MAX_UNWIND_SLIP = 0.05` — between the
0.0133 one-chunk baseline and the 0.09+ cost of being blocked. Deep book, it
keeps the v8 speed; thin book, it backs off to v7 behaviour. The remainder
carries to the next cycle, by which time the book has refilled.

## `v10_revert` — back to v7 settings; the v8/v9 measurement was wrong

v8 and v9 were a regression and both rested on a broken measurement.

Realised economics, tender price against the VWAP of the entire unwind:

| version | chunks/cycle | bar  | gate edge | realised | given back |
|---------|--------------|------|-----------|----------|------------|
| v7      | 1            | 0.08 | +0.1420   | +0.2354  | -0.0933    |
| v8      | 3            | 0.05 | +0.1067   | +0.0815  | +0.0251    |
| v9      | 3 (gated)    | 0.05 | +0.1265   | +0.0548  | +0.0717    |

v7 realised *more* than its gate predicted. v9 hands back 0.0717/unit — a
0.165/unit swing, roughly 74,000 USD/heat at ~5 tenders of ~90,000 units. v9
heat 2 accepted five tenders with gate edges of +0.168, +0.145 and +0.140 and
still lost 21,421, because the unwind gave back everything it captured.

The cause was concentration. Three chunks per cycle pushes ~30,000 units into a
book holding ~5-10k at the touch, and the fills walk it: 25.194 → 25.235 →
25.260 → ... → 25.332 while buying back 92,000 units. The book refills *between*
cycles, so one chunk per cycle lets the price recover instead of chasing it.

The measurement that hid this was `MAX_UNWIND_SLIP` / the per-chunk slippage
column. It compares each fill against top-of-book *at that instant*, so when our
own unwind drags the quote between cycles it sees nothing. It read +0.0123/unit
while the true cost was ~0.14/unit of self-inflicted drift. That figure is what
justified dropping the bar to 0.05, so one bad metric produced two bad changes.
The v9 slip gate was blind for the same reason — it was gating on the metric
that could not see the problem.

`UNWIND_CHUNKS_PER_CYCLE` back to 1 and `MIN_TENDER_EDGE` back to 0.08.
Per-chunk slippage is kept only as a safety stop, explicitly not as a cost
measure. `analyze_tenders.py` now scores the real thing — tender price against
the full unwind VWAP — and writes `logs/tender_pnl.csv`.

Lesson worth keeping: a metric that only sees one instant cannot price an
action whose cost accrues across many.

## `v11_hedgesafe` — the unwind was eating the arb's hedge

v10 heat 4 ran to -15,162. The tender gate was behaving correctly (skipping at
-0.01 against the 0.08 bar), and both arb directions score positive over full
history, so neither was the cause. The position trace was:

    tick 128: bull  -5,000  bear  -5,000  ritc 7,000  unwind SELL 5,000
    tick 129: bull -10,000  bear -10,000  ritc 7,000  unwind SELL 5,000
    tick 135: bull -15,000  bear -15,000  ritc 7,000  unwind SELL 5,000
    tick 136: bull -20,000  bear -20,000  ritc 7,000  unwind SELL 5,000

`unwind_ritc` worked off the raw RITC position, so it could not tell tender
inventory from the arb's ETF hedge leg. Each basket_rich arb bought 5,000 RITC
to hedge its short BULL/BEAR, and the unwind sold that hedge straight back on
the next cycle. RITC sat flat at 7,000 while the short basket grew without
bound, turning a net-neutral arb into a naked directional short. NLV fell ~9,000
across ten ticks, and the arb kept re-entering because on its own terms each
trade still looked like edge.

The unwind now tracks `tender_position` - signed RITC acquired from accepted
tenders, reset each heat since positions close out - and targets only that,
clamped to the live position so a stale tracker cannot drive trades the wrong
way. The arb's hedge leg is left alone.

Same root cause as the v8/v9 regression, one level up: a control acting on an
aggregate it could not attribute. There, per-chunk slippage could not see cost
accruing across cycles; here, raw RITC position could not see which strategy
owned the inventory.

---

## Known open items

- **`basket_rich` is negative** (−0.0218/unit over 8 arbs) while `etf_rich` runs
  +0.0945. The calibrator fits each direction separately so it should widen that
  bar on its own, but the sample is thin and it is slow. Arb positions also
  consume the gross capacity tenders need, and tenders are worth roughly 6x more
  per heat.
- **Converters are unusable.** Human-only per the case rules, so a tender
  position can only be unwound into the market.
- **Spread arb ceiling is ~10k/heat** measured; tenders are ~57k/heat at peak
  edge. Effort belongs on tender capture and unwind quality.
