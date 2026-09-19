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
