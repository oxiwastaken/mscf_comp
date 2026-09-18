# PUNT MODEL — read this before changing anything

A fork of `../volatility`, kept separate so the working model is never put at
risk. Same engine, same recordings, different risk appetite.

**Purpose:** trade expected P&L for a fatter right tail, to raise the chance of
a 10-heat average at $250–300k.

**Status as of 18 Sep 2026: the purpose is not achievable, and the reason is
structural rather than a tuning failure.** Everything below is measured over
95 recorded heats. Keep the folder — it is the right place to try structural
ideas — but do not expect a punt dial to close the gap, because there isn't one.

---

## The finding, in one table

Every variant scored on the identical 95 heats (`python sweep.py --punt`).
`sd` is the per-heat standard deviation — the thing a punt is supposed to raise.

| Config | mean | sd | P(10-heat avg ≥ $250k) |
|---|---|---|---|
| base (tuned ramp) | $135,176 | $58,854 | 0% |
| free delta band | $139,286 | $61,310 | 0% |
| hold to expiry | $132,911 | $61,906 | 0% |
| conviction only | $131,638 | $59,427 | 0% |
| no gap smoothing | $109,239 | $53,252 | 0% |
| `fixed` prior | $109,282 | $55,157 | 0% |
| **always max size** | $125,991 | $58,463 | 0% |
| always max + free delta | $131,702 | **$63,491** | 0% |

**`always max size` is the whole argument.** It pins the book at the full
1,000-contract net limit, in the direction of the view, for every tick of every
heat, and never trims. It is the largest bet the case permits. Its standard
deviation is **$58,463 against the base model's $58,854** — statistically
indistinguishable. Maximum possible aggression does not move the spread at all.

The best spread anything achieved was $63,491, 8% above base, and it bought
nothing: p95 of the 10-heat average was $165,604 versus the base model's
$166,348. The upper tail did not move either.

## Why — the arithmetic that closes it

The book is **already at the 1,000-contract net limit in every heat under every
config.** Measured directly: peak net position was exactly 1,000 in 26 of 26
heats sampled. There is no unused risk capacity to spend.

Both exposures the case offers are hard-capped, and both caps are
server-enforced:

```
option net limit     1,000 contracts
ATM vega             ~$5.76 per contract per vol point
                 ->  ~$5,760 per vol point, and that is the ceiling

delta band           +/- 7,000 free
                 ->  a $1 move in RTM is worth at most $7,000
beyond the band      $0.10 per delta per SECOND
                 ->  riding 20,000 delta for a heat costs ~$390,000
```

So vega is capped at 1,000 contracts and directional exposure is capped at
7,000 delta. Variance in the P&L is therefore set by **how badly the maker
misprices vol on the day**, which we do not control. That is exactly what the
heat-to-heat record shows: byte-identical code returned +$162,011 and +$33,437
on consecutive heats.

To reach P(10-heat avg ≥ $250k) ≈ 10% from a $135k mean, the 10-heat average
would need a standard deviation near $90k, i.e. a **per-heat sd near $285,000** —
roughly five times what any config produces, at no cost to the mean. Nothing
available does this.

## The gross-capacity idea is dead — do not rebuild it

`../volatility/STATE.md` lists "use gross capacity, not just net" as the biggest
unbuilt lever, worth ~1.55x: long 1,750 ATM against short 750 low-vega wings,
keeping net at 1,000 while lifting vega.

**It was built and tested. It lost roughly half the P&L on the first heat.**

The 1.55x came from a single observation at tick ~190, late in a heat, when
vega has collapsed toward the money. Early in a heat — when positions are
largest and the gaps are widest — `sigma*sqrt(T)` is large relative to the
48–52 strike range and the wings are **not** low-vega. Measured across the
recordings, min/max vega by tick:

```
tick  25    0.59      tick 150    0.38      tick 250    0.08
tick 100    0.61      tick 200    0.17      tick 275    0.03
```

At a ratio of 0.6 the structure trades 75% more contracts to gain ~30% more
vega, and needs about 2 vol points of convergence just to break even on the
extra fees; at the 0.93 ratio seen early in a live heat it needs ~10 points.
Observed gaps are 1–5.

## What is actually left

Not variance — **edge**. The one direction never tested is forecast quality:
capturing more of each convergence rather than more vega per point. The base
model's own notes list it as lever 5 and it remains open. A better forecast
raises the mean, which is what the 10-heat average actually rewards.

If you want to keep punting, the only untested structural idea is speed: the
maker converges gradually after each weekly shock, and `GAP_SMOOTHING` delays
the reaction by ~10 ticks. A filter that responds instantly to a news step but
stays slow otherwise is an EV lever, not a variance lever — but it is real, and
`sweep.py` can settle it.

## Config shipped here

Set to the highest-variance configuration found, since that is what this folder
is for. It costs about $3.5k of mean against the base model (t = −0.9, not
significant) and buys ~8% more spread.

```
ENTRY_VOL_EDGE       0.0001      always in the market
FULL_SIZE_VOL_EDGE   0.0002      always at the limit
EXIT_VOL_EDGE        0.0         never trim
NO_NEW_AFTER         1.00        carry into expiry
HEDGE_TRIGGER_DELTA  6900        ride the whole free delta band
HEDGE_TARGET_DELTA   6500
```

**Do not run this in competition over the base model on current evidence.** It
is a worse bet on the mean and no better on the tail.

## Running it

Recordings are shared with the base model — not copied — so both are scored on
identical heats:

```
python sweep.py --baseline        # score the shipped config
python sweep.py --punt            # every variance variant, side by side
python test_engine.py             # 139 offline checks
python autotrader.py --live       # trade it
```
