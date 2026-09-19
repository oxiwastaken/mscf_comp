# Strategy versions

Every logged row carries a `risk_model` column naming the version that produced
it, so data from different formulas never gets averaged together. Bump
`RISK_MODEL` in `etf_arb_bot.py` whenever the trading maths changes, and add an
entry here.

Full parameter sets per launch are in `logs/runs.csv`; per-heat threshold
decisions are in `logs/calibration.csv`.

---

## What the bot does now

The history below is long and several entries are retractions, so this is the
current behaviour in one place. Every parameter links to the version that
justifies it.

**Tenders are where the money is.** ~6 arrive per heat at 70,000–100,000 units;
the spread arb is worth ~783 CAD/heat against tenders at 10–50x that.

1. **Gate.** Each tender is re-scored every cycle of its ~6-cycle life and
   fires on a good cycle, not the first one. The bar depends on how the
   position will actually be exited (`v15`): before tick 200 it is priced
   against a market unwind at chunk VWAP, less the 0.02 fee, and must clear
   `MIN_TENDER_EDGE = 0.04` (`v19` — break-even is +0.0138); at or after 200 it
   is priced against close-out at the mid with no fee, and must clear
   `MIN_TENDER_EDGE_HELD = 0.04` (`v15`, `v20`).
2. **Make room if needed.** A tender too large for the x2 net cap even on a
   flat book gets a small BULL+BEAR pre-hedge to fit (`v13`). Never past gross,
   never past the edge gate.
3. **Exit, cheapest first** (`v14`, `v20`):
   - **Hold** — past tick 200, nothing is traded. Close-out is *exact* and
     free, proven live to 0.02%.
   - **Rest** — a 10,000-unit limit order at `bid + 0.01`, earning the 0.01
     rebate instead of paying the 0.02 fee (`v12`).
   - **Cross** — v11's depth-limited market chunker, only when behind an
     8-cycle glide path to flat.
4. **Never touch the arb's hedge leg.** The unwind targets `tender_position`,
   not raw RITC (`v11`), and that tracker is reconciled to the live position
   every cycle so it cannot ratchet (`v16`).
5. **The arb may not spend gross a tender needs.** 200,000 is reserved,
   capping the arb at 97,000 (`v21`). It cost tenders 14,277 USD/heat before.

**Where we stand against the 40,000 target** (`python heat_report.py`,
`python backtest.py`):

| sample | n | mean/heat |
|--------|---|-----------|
| all full 300-tick heats that saw tenders | 21 | 13,449 |
| v14/v17 live heats, per 300 ticks | 7 | ~22,000 |
| best full heat under the new code (v14 h2) | 1 | **+40,012** |

Per-heat sd is 21,447, so the 95% CI on any of these is roughly +/-9,600 with
the samples available. That is the reason parameters are settled structurally
here and not by comparing heat averages.

**Key measured quantities:**

| quantity | value | source |
|----------|-------|--------|
| execution, aggressive unwind | -0.0420/unit | v10, 12 tenders |
| execution, passive unwind | **-0.0229/unit** | v14 live, 8 tenders |
| drift while holding | -0.0164/unit (sd 0.0740) - mean-zero | 41 tenders |
| passive fill share | **46-70% live** (simulated 12-15%) | v14 heats |
| close-out accuracy | **exact** - expected 24,430 USD, got 24,913 CAD | v17 live heat |
| tender break-even, gate basis | +0.0138 (se 0.0058) | two estimators agreeing |
| spread arb | +0.0248/unit net, 783 CAD/heat | 438 arbs, 67 heats |

**Offline suites** (no credentials needed, seconds to run):

    python sim_unwind.py && python test_prehedge.py \
      && python test_hold_gate.py && python test_cycle.py

**Analysis tools:** `decompose_unwind.py` (execution vs drift),
`analyze_tenders.py` (realised tender P&L), `sweep_deadline.py` and
`sweep_holdtick.py`, `sweep_tenderbar.py` (parameter choice), `backtest.py`
(per-heat attribution -> `logs/scoreboard.csv`), `heat_report.py` (headline vs
target).

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

## `v12_passiveunwind` — the unwind rests before it crosses

Every order this bot had ever sent was a MARKET order. The case pays a
**0.01/share rebate on passive limit fills** against the 0.02/share market fee,
and the unwind moves ~490,000 shares a heat, so the fee line alone was worth
looking at.

Built on top of v11, which had just established *what* the unwind should
target. This changes *how* it gets there.

**What it does.** Two layers. The passive layer keeps one 10,000-unit limit
order resting at the touch — `bid + 0.01` when selling, which is inside the
0.08 median RITC spread and therefore becomes the best offer rather than
joining a queue. The aggressive layer is v11's market chunker, unchanged, but
it only fires when the position is behind a linear glide path to flat. Any unit
the passive layer clears is a unit the aggressive layer no longer has to take.

If nothing ever fills passively, every unit goes through the aggressive layer
and behaviour degrades exactly to v11. That property is what makes this safe to
ship in a way v8/v9 were not.

**What it is worth — and what it is not.** The first estimate was 14,700
CAD/heat, from 490,000 shares × 0.03. That was wrong: it assumed every share
fills passively. Measured passive share is 12–15% at the shipped setting, so
the real figure is **~315 CAD per unwind, ~1,300 CAD/heat** at four tenders.
Small, certain, and free — but not a fix for the 40,000/heat target.

**The deadline is the whole design.** `sweep_deadline.py` over 463 windows of
the logged RITC tape, per ~90,000 unit unwind:

| deadline | passive | flat@ | fee saved | price sd | worst window |
|----------|---------|-------|-----------|----------|--------------|
| 8        | 15%     | 8.7   | +405      | 0.0248   | −36,000      |
| 12       | 23%     | 12.0  | +608      | 0.0750   | −89,000      |
| 25       | 47%     | 24.9  | +1,280    | 0.1847   | −175,000     |

Resting longer earns more rebate and costs far more variance. The price term
has a mean near zero at every setting (−0.005/unit) — drift is a martingale,
so extra cycles add spread, not expected cost — but the spread grows 7x from 8
to 25 cycles, on a heat P&L that already ranges −16,522 to +74,664. Paying ~875
of certain fee saving for a 0.16/unit wider distribution on 90,000 units is a
bad trade.

`UNWIND_DEADLINE_CYCLES = 8` is the pace v11's chunker already achieves, so the
passive layer **displaces** aggressive fills rather than **delaying** them. At 8
the price sd (0.0248) matches the market-only baseline (0.0254) and the position
clears in 8.7 cycles against 8.0. Below 8 the setting stops binding — the
depth-limited chunker cannot go faster — so 3 and 5 give identical results.

**Bug found and fixed while testing.** `UnwindState.behind()` computed
`self.opened or cycle`, which reads a legitimate cycle 0 as unset and pins
`elapsed` at 0 forever. The deadline silently never engaged and the position
never cleared. Caught only because `sim_unwind.py` asserts that the position
reaches flat; nothing in a live heat would have reported it as anything other
than a slow unwind.

**Interaction with v11.** A passive fill changes the position between cycles,
without passing through `unwind_ritc` the way a market order does. It is the
only inventory change the unwind does not make itself, so `tender_position` is
decremented in the reconcile loop before anything is decided - otherwise the
tracker would keep pointing at inventory that has already gone.

**Also here.** `authorization()` resolves credentials at connect time instead of
at import, so the module can be imported without a `.env` — without that the
simulator cannot drive the real trading functions. `analyze_tenders.py` now
reads `trades*.csv` and `tenders*.csv` across generations rather than only the
current file, and scores on `quantity_filled` rather than order size, because a
resting order fills in pieces.

Re-running it across the recovered history says something the single-file view
hid: v10 has now taken five tenders at a gate edge of +0.1035 and realised
+0.0485, giving back +0.0550/unit. **v7's +0.2354 does not look like a property
of one-chunk unwinding** — on six tenders it looks like a small sample.

### Tooling

- `sim_unwind.py` — fake exchange implementing the endpoints the bot calls,
  driving the real `unwind_ritc` over the logged RITC tape. Asserts: the
  position reaches flat, inside the deadline, never flips through zero, leaves
  no resting orders, and does not clear later than the market-only baseline. Its *cost* numbers come
  from a synthetic book with an invented impact coefficient and are labelled
  INDICATIVE — they are not evidence.
- `sweep_deadline.py` — picks `UNWIND_DEADLINE_CYCLES` by paired comparison.
  An earlier version of it differenced each window against the *pooled* mean
  baseline instead of the same window's baseline; since RITC ranges 22–27 across
  these logs that reported a 0.94/unit spread which was entirely cross-window
  price level. Same failure mode as the v8/v9 metric, caught before it decided
  anything.

## `v13_prehedge` — buy net room for the tenders that cannot fit

**The diagnosis first, because it reframes three versions of work.** Every
tender the net check has ever refused was pulled out of the logs and paired
with the position at that tick:

    ver              heat tick      qty     edge    bull    bear    ritc      net
    netbar_fxhedge      8  180   99,000  +0.2800  15,000  15,000  -2,000   26,000
    v7_edgecal          2  203   97,000  +0.1400  35,000  35,000  -2,000   66,000
    v7_edgecal          3  167   87,000  +0.1700  20,000  20,000  -2,000   36,000
    v10_revert          4  199   97,000  +0.0800 -20,000 -20,000   2,000  -36,000

`ritc` is ±2,000 — the unwind residual — in **every single one**. The net that
was blocking tenders was never tender inventory waiting to be unwound. It was
the arb's own BULL/BEAR. 29 blocked tenders, **350,740 USD of positive edge**,
and v8/v9/v10 spent three versions tuning unwind speed to fix it.

That splits cleanly:

- **257,320 USD across 24 tenders** would fit on a book where the arb is
  net-neutral. That is exactly what v11 restored by stopping the unwind eating
  the arb's ETF hedge leg — so v11 is worth far more than the ~9,000 NLV it was
  written to recover.
- **93,420 USD across 5 tenders** does not fit *even on a completely flat book*.
  That is this version.

**The structural problem.** At the ETF's x2 multiplier a 99,000 unit tender is
198,000 of net against a 200,000 cap, and 100,000 is exactly 200,000. With
`LIMIT_BUFFER` on top, the largest tenders are unreachable no matter how well
everything else behaves. They are not rare — 6% of 234 logged tenders are
98,500 or larger — and the ones clearing the edge bar are worth ~4,200 USD/heat.

**The fix.** One BULL plus one BEAR carries the same exposure as one RITC, but
at a x1 multiplier each. Selling `need/2` of both moves net by `need` in the
opposite direction, so a few thousand shares buys room for a tender that is
tens of thousands of units too large. Replayed against the four reachable
cases on a v11-clean book:

| heat | qty     | edge    | value       | hedge/side | fee  |
|------|---------|---------|-------------|------------|------|
| 3    | 99,000  | +0.1600 | +15,840 USD | 2,500      | 100  |
| 4    | 100,000 | +0.2200 | +22,000 USD | 3,500      | 140  |
| 7    | 97,000  | +0.1600 | +15,520 USD | 500        | 20   |
| 2    | 98,000  | +0.0950 |  +9,310 USD | 1,500      | 60   |

20 to 140 USD of fee to reach 9,310 to 22,000 USD of edge.

It is also a real hedge, not a limits trick: short basket against long ETF is
less directional exposure carried through the unwind, not more. So the
pre-hedge is held for the whole life of the tender position and taken off only
once the ETF leg has cleared — `unwind_prehedge` runs after `unwind_ritc` for
that reason.

**Guards.** Only the *net* check is bought past; a gross breach means the book
is genuinely full, and the edge gate is never overridden. The hedge is sized
against projected gross as well as net, so net room is never bought with a
gross breach. `prehedge_position` is tracked separately from the arb's
BULL/BEAR — flattening it by reading the aggregate position would eat the arb's
legs, which is the v11 bug one instrument over. The running total is capped at
`MAX_PREHEDGE`, not just the single order, because several tenders can be live
in one cycle and would each size against the same book. If an accept fails
afterwards, the hedge is flat again on the next cycle.

### Tooling

`test_prehedge.py` replays the real refusals rather than a synthetic scenario,
in two regimes. AS LOGGED, the arb's basket is still in the book and the
shortfalls are far larger than a hedge should cover — reported, not asserted.
V11-CLEAN puts the same tenders against a book carrying only the unwind
residual, which is the world v11 creates and the one this is designed for, and
asserts all four are reached, cheaply, without breaching gross, and flattened
afterwards.

One row was excluded: a netbar-era rejection of a legal 182,000 that today's
absolute `LIMIT_BUFFER` already allows. The test reports the exclusion rather
than quietly dropping it.

## `v14_holdout` — stop unwinding late in the heat

**First, the measurement that reframes the unwind.** `decompose_unwind.py`
splits what a tender gives back into the two things it is actually made of,
both against the *mid* rather than the touch, because a cost measured against a
quote we are ourselves pushing cannot see the push:

    tender price -> mid at accept : the edge the tender carried
    mid at accept -> mid at fill  : DRIFT, the market moving while we hold
    mid at fill  -> our fill      : EXECUTION, spread plus our own impact

Pooled over 32 live tenders:

| component                          | per unit | sd     |
|------------------------------------|----------|--------|
| execution (fill vs contemporary mid) | −0.0550 | 0.0304 |
| drift (mid moved while held)         | −0.0126 | 0.0779 |

**Execution is 81% of the give-back, and it is systematic** — a 0.0304 sd around
a 0.0550 mean. Drift is the opposite: a mean near zero inside a spread six
times its size, which is what a martingale looks like. Three versions (v8, v9,
v10) argued about unwind *speed*; the number says the problem is unwind *cost*.

**Then the rule that makes most of it optional.** The case: *"Any non-zero
position will be closed out at the end of trading at either the last traded
price or the correct price... participants are not required to close
statistical arbitrage positions as the iteration ends."* Close-out charges no
commission and has no impact, so against the mid it costs **zero**:

| exit                           | vs mid  |
|--------------------------------|---------|
| market order                   | −0.0750 |
| passive order (v12)            | −0.0200 |
| close-out                      |  0      |

On a median 85,000 unit tender the market figure is **6,375 USD**.

The only thing the unwind buys is capacity for the *next* tender — so the
question is purely how many more are coming. Over 234 logged tenders, the
average number of later tenders in the same heat clearing the 0.08 bar:

| arrives | 0–29 | 30–59 | 60–89 | 90–119 | 120–149 | 150–179 | 180–209 | 210–239 |
|---------|------|-------|-------|--------|---------|---------|---------|---------|
| later   | 3.50 | 3.11  | 2.62  | 1.89   | 1.70    | 0.96    | 0.48    | 0.03    |

After tick 210 a tender blocks essentially nothing. `HOLD_TO_CLOSEOUT_TICK` is
set to **200**, one bucket earlier, where 0.48 expected follow-ons at ~3,000
USD of realised value still sit well under the 6,375 USD the unwind costs —
about 2x in favour of holding rather than the 17x at 210, so the threshold does
not depend on the tail of that distribution being exactly right.

Past the threshold the unwind does nothing at all: no market orders, and
resting passive orders are cancelled, because a resting fill (−0.0200) is
cheaper than crossing but still worse than close-out (0).

**What this trades.** A certain 0.0750/unit for an uncertain drift term with no
expected price. With the heat mean well short of 40,000, that is the right side
of the trade. The pre-hedge is deliberately left on too — `unwind_prehedge`
only fires once the tender position has cleared, so a held position keeps its
hedge and both legs close out at the correct price.

An opposite-direction tender arriving after the threshold still works and is
now doubly good: it clears the held position *and* pays us to do it. The
existing `tender_safety` projection handles that without a special case.

`sim_unwind.py` asserts the guard is a threshold and not an off switch: past it
the position is untouched with no fills and nothing resting, and thirty ticks
before it the unwind still runs to flat.

## `v15_exitbasis` — price the exit the tender is actually going to take

v14 stopped unwinding after tick 200 but left the gate pricing every tender
against a market unwind. Past the threshold that unwind never happens, so the
gate was valuing an exit that does not exist — and valuing it too low.

The gate's edge is `price − chunk_vwap − FEE_MKT`. A held tender's exit is
close-out at the last traded price: no commission, no impact, so the honest
reference is the mid and nothing comes off for fees. The difference, measured
on 385 logged tender books rather than assumed:

| uplift from close-out basis | value   |
|-----------------------------|---------|
| mean                        | +0.0626 |
| median                      | +0.0550 |
| p10                         | +0.0250 |
| p90                         | +0.1030 |

(An earlier pass through this used a flat +0.0750 taken from the pooled
execution figure. Deriving it per-tender from the logged `chunk_vwap` instead
put it 20% lower. The smaller number is the one used.)

**The two bars are not comparable, and that is the point.** `MIN_TENDER_EDGE =
0.08` is measured *after* subtracting a market unwind. `MIN_TENDER_EDGE_HELD =
0.04` is not. Put on the same footing, 0.04 on the close-out basis is a
materially **stricter** requirement than 0.08 on the unwind basis, not a looser
one.

0.04 because the risk on a held tender is not execution — there is none — but
that close-out lands on the wrong side of the book rather than at the mid. One
half-spread covers that, and the median RITC spread is 0.08. Drift to the close
needs no cushion: it is mean-zero (−0.0126/unit against a 0.0779 sd).

**Worth ~3,000 USD/heat** on the conservative count (11 tenders that carry a
full logged book, scored at the logged `chunk_vwap`). Driving the real gate
over every logged book puts it higher — 36 distinct tenders, 333,780 USD — but
that count uses a synthetic book thinner than the real one at the touch, which
makes the unwind basis look worse and the uplift larger, so it is an upper
bound. The change is justified on the ~3,000.

Typical of what it unlocks: +0.0086 on 93,000 units, refused by the
unwind-priced bar, worth +0.0836 once the exit is close-out.

### Tooling

`test_hold_gate.py` drives the real `tender_safety` over 1,144 logged tender
books and asserts the basis switches at the threshold and not before; that
close-out is never worse than the unwind on the same book (0 of 1,144 go the
wrong way — if any did, the whole argument is backwards); that the logged
uplift is the figure the change was justified on; that the synthetic book errs
in the conservative direction; and that all 47 money-losing late tenders are
still refused.

It also caught two of its own errors first: an assertion that every gate
rejection carries a basis tag, when a limit rejection legitimately carries
neither, and a headline count that was counting log rows rather than distinct
tenders — each tender is logged once per cycle of its ~6-cycle life.

## `v16_trackersync` — the tender tracker could ratchet

Found by writing the first end-to-end test. `test_cycle.py` drives the real
`step_once` through a whole 301-tick heat — book fetch, tender poll, gate,
pre-hedge, accept, arb legs, unwind, pre-hedge unwind, FX hedge — and asserted
that `tender_position` still agrees with the real RITC position at the end. It
did not: the tracker read −59,250 against an actual position of −2,000.

**Two defects, one causing the other.**

`pos` is read once at the top of the cycle, *before* `handle_tenders` runs. So
on the cycle a tender is accepted, `tender_position` includes it and `pos[RITC]`
does not — the unwind and both limit checks spend the rest of that cycle
looking at a book 90,000 units out of date. `handle_tenders` now folds the
accepted quantity into `pos` as well.

With that fixed, the tracker can be reconciled. v11's clamp already refused to
trade more than the live position holds, but it only clamped the *target* and
left `tender_position` untouched, so the tracker ratcheted: the arb's ETF leg
moves RITC without the unwind ever seeing it, and the stale claim survives. A
later arb SELL then takes RITC negative again and the clamp happily lets the
unwind buy back **the arb's hedge leg** — which is the v11 bug returning by a
different route. The clamped value is now written back: believe the position,
not the tracker.

That write-back is only safe *because* of the first fix. Before it, `pos` was
90,000 units stale on the accept cycle and reconciling would have zeroed a
tender we had just taken on.

Neither defect was reachable by the existing tests, which each drive one
decision against a stub. Both needed the whole cycle running against something
that keeps its own books.

### Tooling

`test_cycle.py` asserts, over a synthetic heat with tenders on the cadence the
logs show: no exception escapes any cycle; server limits are never breached;
tender inventory is worked down before the hold threshold and held after it;
the pre-hedge fires and does not outlive its position; the tracker agrees with
the position at the end; nothing is left resting.

Two of its own bugs are worth recording, because both would have made it lie:
it served the **RITC book for BULL and BEAR**, so the arb saw a 20+ CAD edge
every cycle and built a 65,000-a-side basket that no real book could produce;
and two `check` calls printed a failure message on the *pass* path, which reads
as a contradiction in the output. The first tender in the scenario is now
deliberately 100,000 units on a flat book — 200,000 of net against a 197,000
cap — because without it nothing exercised the pre-hedge end to end.

## `v17_holdtick` — the hold threshold, chosen for robustness not for the peak

v14 set `HOLD_TO_CLOSEOUT_TICK = 200` by reading a table. Two things were wrong
with that.

**Holding does not block every later tender — only same-direction ones.** An
opposite-direction tender *reduces* net rather than adding to it, so it still
fits. It is in fact the ideal outcome: it flattens the held position and pays
us for the privilege. **43% of consecutive tenders are opposite**, which
roughly halves the cost of holding:

| arrives   | 0–29 | 30–59 | 60–89 | 90–119 | 120–149 | 150–179 | 180–209 | 210–239 |
|-----------|------|-------|-------|--------|---------|---------|---------|---------|
| all later | 3.50 | 3.11  | 2.62  | 1.89   | 1.70    | 0.96    | 0.48    | 0.03    |
| same-dir  | 2.04 | 1.75  | 1.62  | 1.04   | 0.91    | 0.46    | 0.34    | 0.00    |

**And the peak of the curve is not where to sit.** `sweep_holdtick.py` puts the
central estimate at 150 (+6,629/heat), with 175 (+6,300) and 200 (+5,666)
behind it — flat enough that the peak is not meaningful on its own. The cost
side depends on what a captured tender is worth, which is a median over 32
noisy observations. Sweeping that from 4,000 to 20,000 USD moves the argmax
from **90 all the way to 225**.

So the threshold is chosen by worst-case regret across that whole range:

| threshold | 0      | 60     | 90     | 120    | 150   | 175   | 200   | 225   |
|-----------|--------|--------|--------|--------|-------|-------|-------|-------|
| regret    | 92,125 | 45,441 | 25,139 | 14,640 | 3,706 | 3,732 | 5,278 | 8,092 |

150 and 175 are tied — 26 USD/heat apart — and both clearly beat the 200 that
v14 picked. The tiebreak is the one thing the sweep deliberately does not
price: **drift**. It is mean-zero (−0.0126/unit against a 0.0779 sd) so it
cannot move the argmax, but it does widen heat P&L, and 175 holds 1.79
tenders/heat for up to 125 ticks against 150's 2.28 for up to 150. Same
expected value, less variance — so **175**.

Worth roughly +630/heat over v14's 200 at the central estimate, and rather more
than that in confidence: the old figure sat on one reading of one table.

### Tooling

`sweep_holdtick.py` scores both sides of the trade per tender over the same
logged history, so the comparison is like-for-like, then sweeps the one input
it cannot pin down and reports minimax regret rather than only the argmax.
Drift is excluded by design and the reason is stated in the output, so nobody
later mistakes its absence for an oversight.

## `v19_barmeasured` — the tender bar, finally measured

Every previous move of `MIN_TENDER_EDGE` (0.10 → 0.08 → 0.05 → 0.08) was
reasoned about rather than measured. `sweep_tenderbar.py` estimates break-even
two independent ways:

| estimator | value | se | source |
|-----------|-------|----|--------|
| structural | +0.0138 | 0.0061 | fee 0.0200 + execution 0.0550 − uplift 0.0612 |
| direct | +0.0136 | 0.0187 | mean of (gate edge − realised), 32 live tenders |
| **combined** | **+0.0138** | **0.0058** | 95% upper bound **+0.0252** |

They agree to **0.0002/unit**, from completely different data — execution from
32 live unwinds, uplift from 80 logged tender books. The old bar of 0.08 sat
**eleven standard errors above break-even**.

Expected value by bar, over 234 distinct tenders in 39 heats:

| bar | se above BE | tenders/heat | EV USD/heat | sd | vs 0.08 |
|-----|-------------|--------------|-------------|----|---------|
| 0.02 | 1.1 | 4.31 | 41,679 | 18,762 | +3,255 |
| 0.03 | 2.8 | 4.15 | 41,597 | 18,422 | +3,174 |
| **0.04** | **4.5** | **4.05** | **41,442** | **18,183** | **+3,019** |
| 0.05 | 6.2 | 3.82 | 40,906 | 17,614 | +2,482 |
| 0.08 | 11.4 | 3.23 | 38,424 | 16,227 | +0 |

**0.04, not the 0.02 peak.** The curve is flat — every bar from 0.00 to 0.05
earns within 1,000 USD/heat of the same amount — so the right choice is the
lowest value comfortably clear of break-even, not the argmax. 0.04 is 4.5 se
above it and, more usefully, above the **95% upper bound** of 0.0252: it stays
profitable even in the pessimistic case. It captures +3,019 of the +3,255
available while taking the fewest marginal, highest-variance tenders.

**Why this is not v8/v9 again.** Those ran 0.05 and lost. The bar was not what
lost: v8 (bar 0.05) leaked 7,582 USD/heat and v10 (bar 0.08) leaked 7,871 —
indistinguishable — while v9 leaked 23,005 on the same 0.05 bar as v8. The
variable was `UNWIND_CHUNKS_PER_CYCLE`, not the bar. It is back to 1, and the
exit is now cheaper than either of them had: v12 rests before crossing, and
v14/v17 does not exit at all late in the heat.

### Correction to v15

v15 justified `MIN_TENDER_EDGE_HELD = 0.04` by claiming it was "a materially
**stricter** requirement than 0.08 on the unwind basis, not a looser one."
**That was backwards.** On the shared price-minus-mid basis:

| bar | requires price−mid | break-even | margin |
|-----|--------------------|------------|--------|
| unwound 0.08 (v15-era) | ≥ 0.1412 | 0.0750 | 0.0662 |
| unwound 0.04 (now) | ≥ 0.1012 | 0.0750 | 0.0262 |
| held 0.04 | ≥ 0.0400 | 0.0000 | 0.0400 |

At a 0.08 unwound bar the held bar was the **looser** of the two, not the
stricter. The economics of v15 were right — a held tender needs less edge
because it has no exit cost — but the argument given for them was wrong, and
anyone tuning the pair would have been misled. Only now that the unwound bar is
0.04 does held become the stricter of the two.

`test_hold_gate.py` now asserts the relationship on the shared basis rather
than leaving it to prose.

### Two things ruled out, cheaply

- **Accept timing.** The gate re-scores a tender every cycle of its ~6-cycle
  life. Measured shortfall between the edge we accepted at and the best edge
  available in that life: **0.0000/unit over 44 accepts.** A first-crossing
  rule against a high bar only crosses when the edge spikes, so it is already
  taking the peak. The edge trajectory is flat too (median change 0.0000 from
  first sighting to last), so there is nothing to wait for.
- **Size-dependent bar.** Leak does rise with tender size — bucket means run
  −0.0405 at 70k to +0.0378 at 100k — but the slope is +0.0033 per 10,000 units
  at **t = 1.57**, and over the whole 70k–100k range it moves the bar by 0.010.
  Not worth the complexity on that evidence.

---

## `v18_livepassive` — first live results for v12-v17, and what they say

v12-v17 were built without credentials and were predictions. Four v14 heats and
one v17 heat have now run live. Answering the checklist in order:

| check | predicted | live | verdict |
|-------|-----------|------|---------|
| passive fills happen | > 0 | 0.70 / 0.63 / 0.46 | yes |
| passive share | 0.12-0.15 | **~0.60** | 4x under-predicted |
| rebate is real | 0.03 x passive units | see below | **not testable as written** |
| hold fires | once after tick 200 | never | not yet seen |
| pre-hedge reaches something | non-zero on >=98,500 | never | not yet seen |
| tracker stays honest | no unwind at small pos_ritc | holds | v16 works |

**Passive share is ~60%, not 12-15%.** The checklist's own prescription for
"much higher" is to raise `PASSIVE_MAX_LIVE`, and it is one of the two
parameters explicitly flagged as not sized by evidence, to be tuned against a
real heat rather than `sim_unwind.py`. Raised 1 -> 2: at a 60% fill rate the
single resting order is the binding constraint, not the fill probability. Two
lets a second chunk rest while the first works, without collapsing into the
aggressive chunker. All four suites still pass.

**The `fee_saved` check cannot fail.** It is computed as `0.03 x passive units`
and then compared against `0.03 x passive units`, so the ratio came back exactly
1.00 on all three heats. That is the formula agreeing with itself, not evidence
the exchange paid the rebate. Confirming it needs realised cash against the
passive fills, independent of the assumed rate. Leaving the column, but it
should not be read as validation.

**Heat 3's P&L is missing, not zero.** `nlv_start` and `nlv_end` both logged
0.0 across 257 ticks with 5 accepts and 158,500 units unwound. The NLV read
returned 0 at both ends - almost certainly sampled while the case was reset.
Any mean over these heats should exclude it; it is the heat with the most
tender activity, so including it biases the average down.

Neither the hold nor the pre-hedge has fired yet, so `HOLD_TO_CLOSEOUT_TICK`,
`MIN_TENDER_EDGE_HELD` and the pre-hedge path remain unvalidated live. The
"close-out is benign" question - the one the checklist calls most important,
because v14 and v15 both rest on it - is still open.

## Live validation: close-out is exact, not merely benign

The checklist called "close-out is benign" the most important open question,
because v14 and v15 both rest on it. A v17 heat has now answered it directly.

The heat opened at tick 181, past `HOLD_TO_CLOSEOUT_TICK = 175`, so every
tender was held and **no unwind ran at all**:

    HOLD -97,000 RITC to close-out (tick 201 >= 175) - saves ~7,275 of execution

    accepted  SELL 97,000 @ 25.32  edge +0.0700  ->  +6,790 USD
    accepted  BUY  98,000 @ 25.28  edge +0.1800  -> +17,640 USD
    expected edge                                   +24,430 USD  (~24,919 CAD)

    unwind fills: 0
    heat P&L:     +24,913 CAD

A 0.02% match. Close-out pays the correct price, charges nothing, and has no
impact, exactly as the case PDF states. Held inventory captures **100% of gate
edge**, against the 2-7% that the unwind gives back even at its best. v14 and
v15 stand, and `MIN_TENDER_EDGE_HELD` does not need to rise.

This raises a question the heat cannot answer: if close-out is free and exact,
the only thing unwinding ever buys is capacity for later tenders, so
`HOLD_TO_CLOSEOUT_TICK` may be too late. This heat is not evidence either way -
it began at tick 181, so no later tender could have arrived to be blocked. The
sweep that chose 175 assumed close-out was approximately fair; it is exactly
fair, which shifts the trade in favour of holding earlier. Worth re-running
sweep_holdtick.py with a zero-cost close-out before moving the constant.

**`passive_share` reads 0.0 on these heats, which is correct, not a regression.**
Nothing was unwound, so there was nothing to fill passively. The
`PASSIVE_MAX_LIVE = 2` change from v18 has not yet been tested by a heat that
actually unwinds.

## `v20_holdrevised` - a cheaper unwind makes holding worth less

**This retracts v17's threshold and puts it back where v14 had it.** The reason
only became measurable once v12 ran live.

What holding saves is the unwind cost it avoids - no more, now that close-out
is proven *exact* rather than merely fair. That cost is regime-dependent:

| regime | execution (fill vs mid) | net fee | holding saves |
|--------|-------------------------|---------|---------------|
| aggressive (v5-v11) | 0.0420 | +0.0200 | **0.0620/unit** |
| passive (v14 live, ~60% fill) | 0.0229 | +0.0020 | **0.0249/unit** |

v17 used the `chunk_vwap` uplift, +0.0612/unit, as the saving. That matches the
aggressive regime almost exactly and was the only figure available before v12
had run - but **v12's passive layer more than halved the unwind cost, and a
cheaper unwind makes holding worth less, not more.** At 0.0249 the sweep ranks
175 at **-305 USD/heat**: mildly value-destroying, still blocking later tenders
while saving almost nothing.

This is the opposite of what the "close-out is exact" result first suggested.
Exactness confirms the saving is the *full* unwind cost with no haircut - but
that full cost had itself collapsed, and the second effect is much larger.

Both inputs are uncertain - the unwind cost is 8 tenders of the current regime
(se ~0.011), the value of a captured tender a median over ~40 - so the choice
is worst-case regret over **both** axes rather than either point estimate:

| threshold | 0 | 60 | 90 | 120 | 150 | 175 | 200 | 225 |
|-----------|---|----|----|-----|-----|-----|-----|-----|
| regret | 130,603 | 70,204 | 44,105 | 27,957 | 10,711 | 7,373 | **4,405** | 5,866 |

On the single-axis sweep 225 wins (regret 628). Over both it is 200, because
225 is exposed if the passive layer ever stops filling and the unwind reverts
to costing 0.0620.

`sweep_holdtick.py` now takes the regime as an argument and reads the saving
from a measured `UNWIND_COST` table rather than a `chunk_vwap` proxy. If
`PASSIVE_MAX_LIVE = 2` raises the passive share further, the unwind gets
cheaper again and this should move later still - re-run it after the next heat
that actually unwinds.

### Why the tender bar was NOT moved again

The same logic suggests break-even should have fallen too: at a 0.0249 unwind
cost the structural break-even on the gate basis is about **-0.036**. But the
direct estimator disagrees - over the 9 passive-era tenders it reads **+0.0335
(se 0.0434)** - and `sweep_tenderbar.py` exists partly to refuse a bar change
when its two estimators diverge. They diverge by 0.07 here, on n=9.

So `MIN_TENDER_EDGE` stays at the 0.04 set in v19 from the aggressive-era
break-even of +0.0138. If the structural view is right, 0.04 is simply more
conservative than intended, which is safe. Re-derive once the passive era has
30+ tenders.

**Methodological note for the next person:** v19's `sweep_tenderbar.py` pooled
execution across v5-v11 (all market orders) to set a bar for a passive unwind.
That breaks this file's own opening rule - rows produced under different
formulas must never be averaged. The 0.04 it produced survives because it errs
conservative, but the method was wrong and the tool should segment by regime.

---

## `v21_grossreserve` - the arb was blocking tenders on gross

v11 stopped the arb blocking tenders on NET. It then started blocking them on
GROSS instead, and nothing caught it until v14 ran live and `backtest.py`
attributed refusals by reason:

    v14_holdout    gross 14,277   edge 7,548   net 5,883   (USD/heat)

`gross` had never appeared as a refusal reason before. The position behind it:

    tick 210   bull +55,000   bear +55,000   ritc -57,000   gross 224,000
      -> refused 87,000 units at +0.2500 = +21,750 USD
    tick 236   bull +50,000   bear +50,000   ritc -52,000   gross 204,000
      -> refused 92,000 units at +0.1997 = +18,374 USD

Net is -4,000 - perfectly hedged, invisible to every net check v11 added - but
224,000 of gross, 75% of the cap. **40,119 USD refused in a single heat.**

The arithmetic is not close. Over 438 logged arbs:

| arb-gross cap | arbs blocked | cost at 120 CAD/arb |
|---------------|--------------|---------------------|
| 200,000 | 3% | ~24 CAD/heat |
| 127,000 | 7% | ~55 CAD/heat |
| **97,000** | **10%** | **~78 CAD/heat** |

against 14,277 USD/heat of tenders. Reserving for the largest tender ever seen
(100,000 units = 200,000 of gross) costs about 78 CAD a heat and guarantees the
arb can never be the reason a tender is refused. The whole arb is worth 783
CAD/heat; one blocked tender is worth ~10,000 USD.

`MAX_GROSS` is now **derived**, not a magic number:

    MAX_GROSS = SERVER_GROSS_LIMIT - LIMIT_BUFFER - TENDER_GROSS_RESERVE
              = 300,000 - 3,000 - 200,000 = 97,000

so changing the reserve or the buffer cannot leave a stale constant behind.

The check is also on **projected** gross rather than current. One arb is
`ORDER_QTY` of each leg with the ETF counting double, so it adds up to
4 x ORDER_QTY = 20,000. Gating on current gross would let an arb start at
96,000 and finish at 116,000, spending 19,000 of the reserve the check exists
to protect.

`test_cycle.py` asserts the invariant directly: whenever tender inventory is
flat, everything left in gross belongs to the arb and must sit inside
`MAX_GROSS`; and `MAX_GROSS + 200,000` must fit under the server cap.

**Pattern worth naming.** This is the third time a control has been blind to an
aggregate it could not attribute - per-chunk slippage could not see cost
accruing across cycles (v8/v9), raw RITC position could not see which strategy
owned the inventory (v11), and now a net-neutral arb was invisible to every
check while quietly consuming the gross that tenders need.

---

## `v22_settlelag` — one stale read could strand a whole tender

**Evidence first.** Ten full v21 heats, decomposed as expected edge (the edge
of accepted tenders at the moment of acceptance) against realised heat P&L:

| | |
|---|---|
| sd of expected edge (what the market offered) | 21,912 |
| sd of the gap (drift while exiting) | 46,278 |
| mean gap | **+61** |

The exit is unbiased: on average we bank exactly the edge we take. So nothing on
the exit side can raise the mean - only more or better tenders can. Sizing what
was refused despite positive edge:

| refused, positive edge | tenders | per heat |
|---|---|---|
| on position limits | 9 | **+13,472** |
| edge 0.00-0.04, below the bar | 9 | +1,825 combined |

So `MIN_TENDER_EDGE` is not the lever, and the planned bar test was dropped.

**Five of the nine limit refusals were one bug.** Heat 13 accepted SELL 89,000
at tick 20, and `pos_ritc` then sat at -91,000 for 125 ticks - well before the
hold threshold - while five later tenders were refused on gross, one at +0.360
edge on 71,000 units. The heat unwound 3,000 units in total.

The server settles a tender fill about a cycle after the accept, and in between
`/securities` alternates old and new: -94,000 -> -5,000 -> -94,000. v16 clamps
the tracker to the live position and writes the clamp back, and that write-back
can only shrink the tracker, never restore it. One read took it from -89,000 to
-5,000 permanently; the unwind cleared to its 2,000 threshold (the 3,000) and
treated the remaining -91,000 as not its business.

v16's own note says the write-back is "safe only because handle_tenders now
folds an accept into pos in the same cycle". That covers the accept cycle. The
settlement lag lands on the cycle after. Six transient reads in 55 heats, every
one straight after an accept.

**Fix.** The reconcile is now a pure function, `reconcile_tracker`, and a
binding clamp is believed only when it binds on two consecutive reads. The
first time, the tracker is kept and the unwind skips one cycle. A transient read
recovers by the next cycle; a real move by the arb persists and is written back
one cycle later than v16 did, which is the entire cost.

`test_reconcile.py` replays the logged sequences from v21 heats 13 and 10 and
v17 heat 1, and checks v16's original case - a real arb move - is still shed.
Run against the old logic, heat 13's sequence gives
`[-89000, -5000, -5000, -5000, -5000]`: stranded, as observed live.

## `v23_agreedclamp` — v22's debounce confirmed the wrong value

v22's first live full heat caught its own bug. The first tender in v22 heat 2
proved the fix: SELL 86,000 at tick 52, a settlement flicker to +10,000 at tick
53, one skipped cycle, then a clean unwind to flat by tick 61. Under v21 that
flicker would have stranded the position.

The second tender broke it. BUY 84,000 at tick 164 left the tracker at 82,000
against a real 77,000 - 5,000 of arb residue, a legitimate bind - and the next
read was a settlement flicker to -7,000. v22 believed a clamp that bound on two
consecutive reads and wrote back the *second* read's value, so the flicker was
confirmed and the tracker went to 0. +77,000 then sat unhedged from tick 165 to
close-out, swinging NLV by +/-14,000 with no UNWIND or PASSIVE line logged.

Two binds in a row do not mean two reads agree - they can bind for different
reasons. A confirmed clamp now writes back only the shrink both reads support,
the milder of the two. Here that is 77,000; the flicker to 0 has to confirm
itself again on its own and cannot.

`test_reconcile.py` gains the heat-2 sequence. v22's logic on it gives
`[82000, 0, 0, 0]`, stranded as observed live; v23 gives
`[82000, 77000, 77000, 77000]`. v22's own regression cases were all flickers
from an exactly-reconciled tracker, which is why they passed.

## Replay harness - comparing policies on the same heats

`replay_tenders.py` replays every logged full heat's tender stream through a
parameterised policy, so two policies are compared on identical markets. Live
A/B cannot do this cheaply: heat P&L has an sd of ~53,000, so resolving a 10,000
difference needs ~450 full heats per version. Paired replay removes the market
from the comparison.

The tender stream and prices are exact; held tenders are exact (close-out is
proven). Unwind execution is modelled at a measured cost, so the harness
**cannot evaluate changes to how we unwind** - only which tenders to take and
whether to hold them.

**Validation, before any experiment:**

| check | result |
|---|---|
| exit model, live accepts replayed (19 heats) | correlation 0.86 with live P&L, error sd 26,313 against heat sd 51,509, bias -13,533 |
| gate, v21 parameters on v21 heats | 42 of 45 live accepts reproduced (93%) |

The first version scored 0.42. The missing piece was **FX**: the live bot
converts every USD flow to CAD as it happens (`hedge_fx` flattens USD cash each
cycle), and USD/CAD moves with an sd of ~0.04 inside a heat on a ~2.25M USD
tender notional. Converting at the close instead is what a USD-only analysis
implicitly assumes - which also means the earlier chat-only estimate that held
tenders realised +0.338/unit, measured in USD, should not be relied on.

**Findings, 39 heats, 309 tenders:**

- **v21 vs v23 is a wash in expectation.** At the measured flicker rate (4.2% of
  pre-hold accepts, 300 paired draws) v23 is +413/heat, t +0.19. The fix is a
  correctness and tail-risk fix, not a mean improvement; the gap between their
  live means is noise.
- **Current settings are at or near the best tested.** No setting beat
  `min_edge 0.04 / hold 200` at |t| >= 2. Raising the bar costs money: 0.08 is
  -6,511/heat (t -2.25), 0.12 is -12,064 (t -2.49).
- **Holding is valuable.** Never holding costs ~9,000/heat at the live bar, and
  up to -30,107 (t -4.59) at a 0.12 bar.
- **Holding from tick 150 looked +7,609/heat (t +1.66) but does not
  replicate:** +15,940 in the first half of the heats, -306 in the second. Left
  at 200.
- **The FX treatment is fine.** Keeping USD until close-out instead of
  converting is +1,274/heat (t +0.22) with a higher sd; no change.

## `v24_swap` - take blocked tenders by dumping for them

**How the case is scored changes what "better" means.** Per the RITCx page, ETF
arbitrage teams are ranked per heat by P&L and the heat ranks are averaged. A
+140,000 heat earns no more than first place; a -50,000 heat can mean last. So
policies are now judged on their downside (10th/25th percentile heat, losing
heats) as well as their mean - `structural.py` reports both.

Two structural ideas were tested on the replay harness, 41 full heats, paired:

| policy | mean | median | p10 | p25 | sd | paired vs live |
|---|---|---|---|---|---|---|
| live (v23) | +56,044 | +61,354 | +816 | +33,694 | 44,279 | - |
| swap, dump cost 0.10 | +58,972 | +61,515 | +816 | +34,050 | 47,547 | +2,929 (t +3.17) |
| hedge 100% held, 0.03/leg | +45,693 | +45,495 | +7,731 | +28,836 | 29,341 | -10,350 (t -2.38) |

**Swap - adopted.** When a tender clears the edge bar but not the limits, dump
just enough same-direction tender inventory at market to fit, if its edge pays
for the dump. Positive at every dump cost tested (it self-limits), significant
up to 0.15, replicates in both halves (+3,746 / +2,150), downside unchanged,
~0.3 swaps a heat. The live decision assumes `SWAP_DUMP_COST = 0.15`, near the
~0.14 walk plus fee that v8/v9 paid pushing ~90,000 units out at once. Only
tender inventory is ever dumped, never the arb's legs. `test_swap.py` covers the
sizing, the no-pay case, direction, the arb legs, and partial fills.

**Basket hedge of held inventory - rejected.** It does what it says to risk (sd
44k to 29k, p10 +0.8k to +7.7k) but costs the typical heat: median -16k, p25
-5k, mean -10k, and the loss replicates in both halves. Ranks are averaged over
heats, so the typical heat matters more than the worst one; this only pays if
the field turns out to cluster around our worst outcomes.

## `v25_mmtest` - live test of passive RITC market making

The one structural idea the replay could not test: whether passive quotes fill
is unknowable offline, because the logs carry no trade prints. So this is a
deliberately small live test that measures it.

**What it does.** While the tender side is idle - no tender inventory held, no
tender on offer, ticks 5-285 - it quotes 3,000 RITC a side one tick inside the
touch (at the touch when the spread is one tick), earning the 0.01 rebate
instead of paying the 0.02 fee. It leans on fair value, (BULL + BEAR) / USD:
rich to it, only the offer shows; cheap, only the bid. Inventory is capped at
+/-9,000.

**Staying out of the tenders' way**, which are worth 10-50x more:
- quoting pauses while any tender is on offer;
- inventory is flattened at market only when it is actually in the way - a
  tender refused on the limits, or tender inventory being taken on. Flattening
  on every tender sighting would pay the dump 6-8 times a heat for nothing;
- tender inventory must never coexist with MM inventory, or the v16/v23
  tracker would net one against the other and the end-of-heat flatten would
  leave a stray position;
- a loss of `MM_MAX_LOSS` (5,000 CAD) in a heat switches it off for that heat.

**Measured in `logs/mm.csv`**, one row per heat: fills, volume, passive vs
flatten volume, end inventory, P&L in CAD. Every fill is also in `trades.csv`
with `direction = mm`.

**What counts as working:** MM P&L positive per heat with real fill volume,
and tender results unchanged - same accept counts, no rise in limit refusals.
If fills are rare or P&L negative after a fair sample, it comes out.

`test_mm.py` drives it against a fake exchange: quoting inside the touch, no
duplicate quotes, repricing, fill booking including a fill that races a
cancel, leaning on fair value, the inventory cap, pausing without dumping, the
blocked-tender flatten, the tender-inventory flatten, and the kill switch.

## `v26_mmoff` - market making off after one live heat

v25's first full heat answered the question the replay could not. Quotes fill
readily - 61 passive fills, 159,996 units, no API errors - but they fill on the
wrong side of the next move:

| after our fill | move in our favour, USD/unit | fills in our favour |
|---|---|---|
| immediately | +0.0075 | 72% |
| +1 tick | +0.0084 | 57% |
| +5 ticks | -0.0229 | 34% |
| +20 ticks | -0.0212 | 45% |
| +50 ticks | +0.0045 | 50% |

Quoting inside the spread starts each fill ~0.008 ahead of the mid, and the
rebate adds 0.01, but within five ticks the price has moved ~0.023 against us.
Inventory pinned at the +9,000 cap buying into a falling price, and the kill
switch stopped it at -5,179 CAD. The move partly reverts by 50 ticks, but the
cap and the flattens realise the loss first.

One heat, and fills cluster in trends, so this is not settled - but it is a
measured mechanism rather than an inference, and at up to ~5,000 a heat it
costs more than v24's swap gains. Switched off; the code and `test_mm.py` stay.
Tender results in that heat were unaffected in any way that could be
attributed: 5 of 8 accepted, expected edge +43,580, and the heat's -30,459 is
mostly held-inventory drift.

**Next variant, if revisited:** quote at or outside the touch rather than
inside it, so fills come only on larger moves and pay a wider spread each -
fewer fills, less adverse selection. It needs its own live test.

## `v27_holdall` - hold every tender to close-out

**60 live v26 heats found a systematic leak.** Against the edge of the tenders
it accepted, v26 realised -35,071/heat less (t ~ -5), in every third of the
night. v26 overall: mean +13,858, median +22,623, sd 52,137.

- The swap is not it: its dumps cost 0.168/unit, close to the 0.15 assumed,
  ~2,400/heat in all; heats with no swap still lost -31,767 against expected.
- Nor is it the market changing overnight: the price moves against us after
  acceptance in every version since v14 (-0.048/unit at +10 ticks on v26,
  -0.066 on v21). v21's +61 gap was a fortunate 10-heat sample.
- It is the unwind. Tenders we unwind move -0.057/unit against us within 10
  ticks - largely our own exit pushing the price; tenders we hold move -0.023,
  not significant. Calibrated on the 60 live heats, the replay matches live at
  an unwind cost of **~0.084/unit** against the mid - over 3x the 0.0249 the
  gate and the replay had assumed since v14.

**At the true cost, holding beats unwinding.** Close-out is free and exact.
Replayed over 104 heats with the swap on, paired against v26:

| policy | mean | median | p10 | p25 | losing | vs live |
|---|---|---|---|---|---|---|
| v26 (hold from 200, bar 0.04) | +25,795 | +30,844 | -40,699 | -8,441 | 31/104 | - |
| hold all, bar 0.08 | +62,119 | +64,876 | -22,499 | +11,987 | 22/104 | +36,325 (t +5.73) |

Positive in both halves at every unwind cost tried, still +21,839 (t +3.39) at
0.05. Better on every measure the per-heat ranking cares about. The held bar
plateaus at 0.06-0.10 and falls at 0.12; 0.08 is its middle, chosen for
robustness rather than the 0.10 peak.

`HOLD_TO_CLOSEOUT_TICK = 0`, `MIN_TENDER_EDGE_HELD = 0.08`. Capacity for later
tenders comes from the swap. The unwind code stays, dormant.

`test_holdall.py` asserts the production path: the unwind sends no order at any
tick, and every tender is priced on the close-out basis. The three suites that
exercise the unwind and late-hold logic now pin the constants they were written
against. `test_hold_gate`'s uplift check asserted v15's exact +0.0626 from 385
books; on today's 3,169 it is +0.0715, so it now checks what v15 rests on - that
the uplift is materially positive - rather than a figure that moves with the data.

## `v28_nodoubledown` - charge more to double down

**The tail is made by size, and size is made by doubling down.** The worst heat
on record, -161,676:

    tick 205   ritc  -83,000   net -136,000    mid 24.47
    tick 245   ritc -112,000   net -194,000    mid 24.81
    tick 295   ritc -112,000   net -194,000    mid 25.42   NLV -142,864

Short 112,000 RITC at the net cap while the mid rose 1.12. That is ~125,000 of
the loss. Not execution, not a bug - a directional bet that went the wrong way.

Since v27 every tender is carried to close-out, so a heat is the edge captured
minus however far the ETF drifts while it is held. On ~90,000 units the edge is
~10,000 and a heat's drift is worth several times that, so position size is the
whole risk.

`BAR_ADD_SAME_WAY = 0.04` demands 0.04/unit more from a tender that adds to
inventory we already hold. Measured on `tender_position`, not raw RITC - the
arb's ETF leg is hedged by its own BULL/BEAR and carries no directional risk
(v11). A tender in the **opposite** direction is deliberately left alone: it is
the only way exposure ever comes off without paying execution, since it
flattens the book and pays us for doing it.

Replayed over 105 full heats against live v27:

| | v27 | v28 |
|---|---|---|
| expected shortfall, worst 5% | -87,600 | **-74,836** |
| worst heat | -110,945 | **-92,638** |
| losing heats | 22/105 | **20/105** |
| mean | 63,172 | 66,986 (t +1.62) |

**The mean gain is claimed as zero.** 75% of it comes from three heats and it
disappears at a 0.10 held bar. The ES5 improvement is what is being bought, and
that held in all nine `exec_cost` x `dump_cost` combinations tried and at held
bars 0.06 and 0.08; losing heats never got worse in any of them. It costs
nothing to operate - it is a threshold, not a trade.

### Six alternatives tested and rejected

| idea | result |
|---|---|
| **net cap** | degenerate. Tenders are 70,000-100,000 units, so any cap below 140,000 of net takes nothing at all |
| **loss stop** | losing heats **22 -> 50**. Drift is mean-zero, so a stop converts recoverable mark-to-market dips into realised losses and truncates the recovery with them |
| **basket hedge** | at the **measured** cost it halves the mean, -31,130 (t -9.60), and raises losing heats to 25 |
| **held bar tuning** | flat between 0.06 and 0.10; v27 already sits at the robust middle |
| **swap off** | -8,931 (t -3.87), and a slightly worse tail. Keep it |
| **time-scaled bar** | charging for holding time moved ES5 erratically and made the worst heat worse (-118,657, -160,415) |

**The basket hedge deserves its own note, because it looked like the answer.**
At an assumed 0.02-0.03/share/leg it cut the worst heat from -110,945 to
-47,832 and losing heats from 22 to 14. Then the cost was measured rather than
assumed: **1,474 live BULL/BEAR fills average 0.0560 against the mid, plus the
0.02 fee = 0.0760/share/leg**, two to four times what was assumed. At the
measured figure the hedge is strictly bad. This is the fourth time on this case
that a change looked good on an assumed execution cost and reversed on a
measured one - v8/v9's per-chunk slippage, v12's passive fill share, v20's
unwind cost, and now this.

**What this does not do.** It reduces the tail; it does not remove it. A
-92,638 heat is still possible, and no policy tested removes that while holding
a 97,000-unit position for 300 ticks - the drift on that position is simply
larger than the edge on it. Eliminating it would mean not taking full-size
tenders, which costs far more than the tail does.

`test_holdall.py` covers the rule in both directions, that the opposite side is
never penalised, that it is symmetric, that a large enough edge still clears
the raised bar, and that a flat book is unaffected.

### Also

`replay_tenders.py` gains `stop_loss`, `hedge_above`, `bar_add`, `bar_reduce`
and `bar_time` as policy options, all defaulting to inert and each asserted to
reproduce the baseline exactly when off, so the rejected experiments above can
be re-run rather than re-argued.

---

## `v27_holdall` (competition build) - v28 off, Windows hardening

**The competition runs v27_holdall**, tradeoffs accepted. `BAR_ADD_SAME_WAY` is
**0.0**, so the v28 block in `tender_safety` is skipped entirely and behaviour
is identical to the `etf-v27-competition` tag. `RISK_MODEL` is back to
`v27_holdall` so logged rows carry the name of the logic that produced them.

Verified rather than assumed: every uppercase constant matches the tag, and
`tender_safety` agrees with the tagged build on **900/900** cases across both
actions, three sizes, five prices, five inventory states (flat, long and short,
large and small) and six ticks.

### Windows hardening - a stray character could abandon a heat

`locale.getpreferredencoding(False)` on the competition machine is **cp1252**.
Two values the bot handles are free text from the server:

- tender `caption`, written to `tenders.csv` on every sighting, deliberately
  **before** the accept and outside any `try`;
- `accept_err`, which is `str(e)[:200]` of whatever the server returned.

A character outside cp1252 raises `UnicodeEncodeError` on the write. That is
not an `ApiException`, so it skips the per-cycle handler and lands in
`run_heat`'s `except Exception`, which gives up the rest of the heat.

Demonstrated, not theorised - the same row through both builds:

    v27 (unfixed)          RAISED UnicodeEncodeError: 'charmap' codec can't
                           encode character '：'
    v27 + encoding fix     wrote the row OK

All 958 logged captions are ASCII today, so this has never fired. It costs a
heat when it does and the fix is free: every log opens `encoding="utf-8"`, and
`sys.stdout`/`sys.stderr` are reconfigured to utf-8 with `errors="replace"` at
import so the console can never be the thing that stops a run. All 22 existing
logs still decode, so a restart can still seed its bars from `heats.csv`.

The em dash and curly quotes that appear in most "unicode" test strings are
**in** cp1252 and pass either way; the audit only found this because it hit
`：`. `test_encoding.py` uses a character genuinely outside the codepage.

### Also

`RUNBOOK.md` - what the build does, the distribution being accepted, preflight
and the suites, how to run it, the Windows notes, and what each error line
means. `test_holdall.py` pins `BAR_ADD_SAME_WAY = 0.04` for its own checks
rather than reading production, so the mechanism stays tested while the
competition runs with it off - the same pattern `sim_unwind.py` uses for the
hold tick.

---

## Known open items

- **Latent tracker mismatch under swap-heavy sequences.** With the unwind path
  enabled and a 0.08 held bar, test_cycle's synthetic heat (five same-direction
  tenders back to back, each needing a swap dump) ends with the tracker at
  2,000 against a real 22,000. Dormant in v27, where nothing is unwound; the
  tracker then only sizes swap dumps, so the worst case is an under-sized swap.

- ~~`basket_rich` is negative (−0.0218/unit over 8 arbs)~~ — **RETRACTED.**
  Rebuilt over the full history with `calibration.load_arbs`, `basket_rich` is
  **+0.0223/unit over 159 arbs** (103 winners, t = +2.37). The −0.0218 came from
  8 arbs, and the per-arb sd is 0.1186 — a standard error of 0.0419 on that
  sample, so the reading was well inside noise and pointed the wrong way. Both
  directions are profitable: `etf_rich` +0.0263 over 253. **Do not disable
  either.** The related worry that arb positions starve tenders of capacity was
  real but is v11's fix, not a reason to stop arbing: the arb is net-neutral
  when its ETF hedge leg survives.
- **Converters are unusable.** Human-only per the case rules, so a tender
  position can only be unwound into the market — or, since v14, held for
  close-out, which is the cheaper of the two and needs no converter.
- **Spread arb is ~783 CAD/heat**, not the ~10k previously recorded: 412 arbs
  over 62 heats at +0.0248/unit net of fees. Tenders are worth 10-50x that.
  Effort belongs on tender capture and exit choice.
- **Nothing since v11 has run live.** v12–v16 are validated only against logged
  data and the offline simulators, on a machine with no `.env`. Everything
  below is a prediction until a heat says otherwise.

### What to check on the first live heats

Run the four suites first — they need no credentials and take seconds:

    python sim_unwind.py && python test_prehedge.py \
      && python test_hold_gate.py && python test_cycle.py

Then, after one heat:

| check | where | expected | if it disagrees |
|-------|-------|----------|-----------------|
| passive fills happen at all | `heats.csv` → `passive_share` | > 0 | 0 means limit orders never fill; set `PASSIVE_UNWIND = False` and v12 is simply off |
| how much fills passively | same | 0.12–0.15 | much higher: raise `PASSIVE_MAX_LIVE`. Much lower: the rebate claim is dead, not the bot |
| the rebate is real | `heats.csv` → `fee_saved` | ~0.03 × passive units | RIT may rebate differently than the case PDF states |
| the hold fires | console → `HOLD … to close-out` | once, after tick 200 | never firing means no tender survives that late |
| close-out is benign | `heat_pnl` on a heat that held vs one that did not | no systematic gap | a gap means close-out is NOT at the mid, and `MIN_TENDER_EDGE_HELD` must rise |
| the pre-hedge reaches something | `tenders.csv` → `prehedge` | non-zero on ≥98,500 unit tenders | never firing means the net ceiling is not what we think |
| the tracker stays honest | `ticks.csv` → `pos_ritc` vs the unwind's behaviour | no unwind while `pos_ritc` is small | v16 is what this guards |

The single most important one is **close-out is benign**. v14 and v15 both rest
on the case's statement that positions close out at the correct price with no
commission. If that is wrong, both should be reverted — they are two isolated
constants (`HOLD_TO_CLOSEOUT_TICK`, `MIN_TENDER_EDGE_HELD`) precisely so that
reverting is a one-line change rather than an unpick.

`PASSIVE_MAX_LIVE` and `PASSIVE_CHUNK` are the only parameters here **not**
sized by evidence: passive fill *volume* cannot be measured from quote history
because the logs carry no trade prints. Do not tune them against
`sim_unwind.py`, whose fill model is invented — tune them against
`passive_share` from a real heat.
