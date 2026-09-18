# STATE — Volatility Trading Case

Work tracker for the RITCx Volatility Trading Case build.
**Competition: Saturday 19 September 2026, Carnegie Mellon.** Last updated 17 Sep 2026.

---

## Topology phase

`BUILT, UNTESTED AGAINST A LIVE SERVER.`

The engine is verified offline (87/87 unit tests). Nothing in this folder has
yet touched the RIT practice server. That is the single largest open risk and
the next thing that should happen.

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
| Option entry | Manual, keypress | Sizing and strike choice are real judgement calls; two-day runway to validate |
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

1. **Smoke-test the dashboard against the practice server** —
   `flserver.rotman.utoronto.ca:16590`, trader `jobe-2` / `exercise`.
   Run `python dashboard.py --dry-run` first. Confirm tickers, case length, and
   that news parses. *Nothing here has run against a live server yet.*
2. **Record 3–4 practice heats** — `python record.py --name heat1`.
3. **Settle the tail-week prior** — `python replay.py heats/*.jsonl`, then set
   `TAIL_VOL_MODE` in `config.py`.
4. **Tune `MIN_EDGE_DOLLARS` and order size** against replayed heats.
5. Confirm the delta limit is ±7,000 on the day — the general RITC rules say it
   can be announced by news instead. The dashboard reads and displays an
   announced limit if one appears.

## Open questions

- Is the heat 300 or 600 ticks on the day? Handled dynamically, but worth knowing.
- Does the practice server quote all ten strikes from tick 1?
- Is `r` actually 0%, or has the instructor changed it? (`RISK_FREE_RATE`.)
- How fast does the market maker converge after a shock? Determines how long the
  edge survives and therefore how aggressively to size. **Measurable from a
  recording** — this is the most valuable thing a practice heat can tell us.
