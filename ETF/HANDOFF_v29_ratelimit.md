# Handoff — v29, lifted request ceiling

**Date:** 2026-09-19 · **Author:** Claude (Opus 5) session
**Status:** implemented, offline-verified, **never run live**
**Touches:** `etf_arb_bot.py`, new `bench_ratelimit.py`

Read this with `VERSIONS.md`. The house convention there applies here: every
claim below carries the measurement behind it, and the things I could not
measure are labelled as such rather than rounded into confidence.

---

## Why this exists

Hayden's instruction was that bypassing the API rate limit had just been made
allowed, and to implement and backtest it.

**Confirm that rule change before shipping this.** I could not verify it from
anything in the repo — the case PDF, `RUNBOOK.md` and `VERSIONS.md` say nothing
about a request ceiling, and I have no access to the organisers' announcement.
Everything here is built on Hayden's statement that it is permitted. It raises
the bot from ~9 to ~35 requests/second, which is visible to the organisers in
their server logs. If the permission is narrower than stated, set
`REQ_PER_SEC = 8.0` and `MAX_CYCLES_PER_TICK = 1` and the request behaviour is
back to where it was, with the reordering below still intact and still paying.

**What I did NOT build, deliberately:** nothing that multiplexes across several
credentials, sessions-per-account or source addresses to get more than one
trader's budget. "The ceiling is higher" and "use more than one account's
ceiling" are different claims, and the second is the one that gets a team
disqualified. If that is what was actually permitted, say so explicitly and it
can be added — but it should not be inferred.

---

## The finding that should change how you read this

The premise — that the rate limit was the binding constraint — is **mostly
wrong**, and the data in `logs/` says so clearly.

Of 41,928 logged tick observations (sentinel rows excluded, see *Bug 1*), 2,118
cleared the edge bar. Of those:

| outcome | count | share |
|---|---|---|
| traded | 437 | 20.6% |
| **refused by the net/gross gate** | **1,680** | **79.3%** |
| cleared, in limits, no trade | 1 | 0.0% |

Median `|net|` when blocked was **36,000**, against a self-imposed
`MAX_LONG_NET`/`MAX_SHORT_NET` of **±25,000** and a server cap of ±200,000.

**The arb is limit-bound, not observation-bound.** Looking at the book eight
times more often does not relax that gate — only the net cap or the tender
reserve does. Anyone who reads "8× the cycles" below and expects 8× the arbs
will be disappointed, and should go read the `TENDER_GROSS_RESERVE` reasoning
in `etf_arb_bot.py` before touching the net cap, because that reserve is well
argued and expensive to get wrong.

Reproduce with `python bench_ratelimit.py`, part 2 (b).

## Where the speed actually pays

**1. Staleness — the big one, and it is free.** Conditional on clearing the
bar, mean surplus was **+0.0775/unit when read** and **−0.0329/unit one
observation (~0.96s) later**; only **42.4%** still cleared. The edge does not
merely decay, it inverts. The old cycle read the four books at the top and
fired the arb legs at the *bottom*, after the tender pass, the unwind, the
prehedge unwind, the MM and the FX hedge — six-plus round trips of rot.

**2. Tender timing — the real prize, and it does need the ceiling.** Tenders
live a median of **6 ticks** but we looked at each one a median of **3 times**
(0.60 looks/tick). The edge swings a median of **~7,100 CAD across a tender's
life** on its own size. The gate re-scores every cycle and fires on the first
clearing look; more looks in the same window means a better one to fire on.

**3. More arbs — no.** See above.

---

## What changed in `etf_arb_bot.py`

| knob | before | now | revert to |
|---|---|---|---|
| `REQ_PER_SEC` | — (flat 0.5s sleep) | `40.0` | `8.0` |
| `ADAPTIVE_FLOOR` | — | `4.0` | — |
| `PARALLEL_FETCH` | — (serial) | `True` | `False` |
| `ARB_FIRST` | — (arb last) | `True` | `False` |
| `LOOP_SLEEP` | `0.5` | `0.02` | `0.5` |
| `MAX_CYCLES_PER_TICK` | — (1/tick) | `8` | `1` |

- **`RateGovernor`** — process-wide token bucket replacing the flat sleep.
  Adaptive on purpose: a 429 halves the working ceiling and it climbs back over
  ~30s. This ships without a live heat behind it, so the failure mode of
  guessing the ceiling too high had to be *a slower heat, not a dead one*.
  Per-heat line now reports requests, cycles/tick, ceiling, 429s, self-throttle.
- **Concurrent snapshot** — the 4 books + `/securities` + `/trader` go out
  together through a thread pool. Each worker thread gets its **own**
  `requests.Session` (`worker_session()`); `requests.Session` is not documented
  thread-safe. A fake session injected by the test harnesses is passed through
  untouched, so `sim_unwind.py` and friends still see every call.
- **`ARB_FIRST`** — the arb gate moved to immediately after the snapshot.
  Nothing below it needed to run first: the gate reads `ok` from the same
  position map, and `risk_metrics` already reserves `4 × ORDER_QTY` of
  projected gross.
- **`MAX_CYCLES_PER_TICK`** — stops us re-reading an unchanged book and
  spending budget the tender pass wants more.

---

## Backtest: `python bench_ratelimit.py`

Part 1 drives the **real `step_once`** against a synthetic exchange with 58ms
injected per call (what the logs imply: 0.965s mean cycle, less 0.5s sleep,
over ~8 requests), with a dislocation that decays in **wall-clock** time at the
rate measured from `ticks.csv`. Wall-clock is the point — decaying per tick
would make every config capture the same thing and measure nothing.

```
config                                          cycle  c/tick   req/s    stale  arbs
before  (serial, 0.5s sleep, arb last)          1049ms    1.00     9.0     117ms     4
reorder (serial, 0.5s sleep, arb first)         1049ms    1.00     9.0       0ms     4
after   (concurrent, governor, arb first)        257ms    8.00    35.2       0ms     4

  reordering alone (NO extra requests):
      edge at the legs 0.2395 -> 0.2530 /unit = +67 CAD per arb
  plus the lifted ceiling:
      edge at the legs 0.2530 -> 0.2618 /unit = +44 CAD per arb
      cycles per tick  1.00 -> 8.00   request rate 9.0/s -> 35.2/s

  total +111 CAD per arb, of which 61% needs no extra requests.
```

**Take the +111 CAD/arb as a shape, not a number.** It depends on `DISLOCATION`
and `DECAY_PER_SEC`, only the second of which is measured. The robust claim is
the *split*: most of the win is the reordering, which costs nothing and carries
no rule risk.

### What this backtest cannot do

- **No sub-second market data exists.** `ticks.csv` is sampled at ~1 Hz. The
  decay *inside* one second is modelled as linear between the two points we
  have. For a dislocation being competed away it is probably front-loaded,
  which would make this an **under**statement — but that is reasoning, not
  measurement.
- **Tender timing is not backtested at density.** Only **3** tenders have ≥8
  looks, far too few for the thinning curve I tried first. The ~7,100 CAD
  figure is the observed within-life *range*, i.e. an upper bound on what
  perfect timing is worth, not a forecast.
- **Part 1 runs without tenders, on purpose.** My first version included them
  and *no arb fired in any config* — one 90,000-unit accept puts 180,000 of net
  against a ±25,000 cap and shuts the gate for the rest of the run. That is a
  faithful reproduction of the finding above, and exactly why the two must be
  measured separately.

---

## Two latent bugs found on the way

**1. `top_of_book` returns `1e12` for an empty book.** 26 of 41,954 logged rows
(0.062%) carry `|edge| > 5`. It fails *safe* for trading — a `1e12` ask makes
both edges hugely negative, so no false trade — but it **wrecks any statistic
computed over the edge series**. It fooled my own first pass into reporting
lag-1 autocorrelation of ≈0.000 (one outlier owning the whole variance);
excluding those 26 rows, the true figure is **0.65–0.86**. Anything in
`calibration.py` that averages over these rows is reading the same poison.
*Not fixed — it is a live-behaviour change and it belongs to whoever owns the
calibration path.*

**2. `handle_rate_limit` could raise on a non-JSON 429 body.**
`response.headers.get('Retry-After', response.json().get('wait', 1))` evaluates
`response.json()` **eagerly**, so a 429 with a non-JSON body raised
`JSONDecodeError` out of the rate-limit handler. It had never fired because the
bot never approached the limit — at 35 req/s it will. *Fixed.*

---

## Before the first live heat

All eight offline suites pass — `test_cycle`, `test_hold_gate`, `test_prehedge`,
`test_holdall`, `test_reconcile`, `test_swap`, `test_mm`, `sim_unwind`.

1. **Confirm the rule change.** See the top of this file.
2. **Run one heat with `REQ_PER_SEC = 8.0` and `MAX_CYCLES_PER_TICK = 1` first**
   — i.e. the reordering and the concurrency, at the *old* request rate. That
   is 61% of the modelled win with none of the rule exposure, and it isolates
   whether concurrency broke anything before speed is added on top.
3. Then raise the ceiling and watch:

| check | where | expected | if it disagrees |
|---|---|---|---|
| 429s | console → `rate governor: 429` | none | the ceiling is not what we were told; the governor self-corrects, but stop and re-confirm the rule |
| cycles/tick | heat-end line | ~8 | far lower means latency, not budget, is binding — raise `FETCH_WORKERS` |
| tender looks | `tenders.csv`, rows per `tender_id` | ~6× today's 3 | unchanged means the tender pass is not benefiting and (c) above is dead |
| arb count | `heats.csv` → `arbs_fired` | **roughly unchanged** | a big jump means I misread the net gate — good news, but check limits were not breached |
| limit breaches | `ticks.csv` → `within_limits` | no new pattern | concurrency means the position map can be read while an order is in flight |
| realised slippage | console → `ARB … slippage` | smaller than before | this is the whole thesis; if it does not improve, `ARB_FIRST` bought nothing |

The last row is the one that matters. If realised arb slippage does not narrow,
the staleness argument is wrong and `ARB_FIRST = False` reverts it in one line.

**One risk the offline suites cannot cover:** the position map is now fetched
concurrently with the books, so `pos` and the books are no longer strictly
sequentially consistent. The gate uses `ok` from that map while an order from
the previous cycle may still be settling. The projected-gross reserve in
`risk_metrics` should absorb it, but `within_limits` in `ticks.csv` is the
column to watch, and `test_reconcile.py` covers the tracker, not this.
