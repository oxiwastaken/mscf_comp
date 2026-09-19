# Overnight run — 19 Sep 2026, unattended

What is running, what changed, and **how to put it back**.

## HOW TO REVERT — do this first if anything looks wrong

Every change below is one commit, and the live path is six files. To undo the
most recent change only:

```
cd volatility
git log --oneline -8              # find the commit to drop
git revert --no-edit <commit>     # undo it, keeping the history honest
python test_engine.py             # expect 192 passed
```

To put the whole live path back to a known-good state in one move:

```
git checkout <commit> -- autotrader.py structure.py vol_engine.py \
                         news_parser.py config.py rit_api.py
python test_engine.py
git commit -m "revert live path to <commit>"
```

The runner refuses to start a heat while any of those six files has uncommitted
edits, so a revert is not live until it is committed. That guard is deliberate.

### Known-good revert points, newest first

| commit | live path state | why you would go back to it |
|---|---|---|
| `217aff3` | **hedge at MARKET IV** | current |
| `eac7f96` | hedge at forecast IV, `HEDGE_TARGET 3,000` | **the one-line rollback for the market-IV change** |
| `fd4a94e` | `NO_NEW_AFTER = 0.90`, heat-state fix in | if tonight's tuning disappoints over ~15 heats |
| `be66fd4` | the build that traded all night to 02:30 | full rollback; loses the cross-heat state fix, which is a genuine competition-day bug — prefer the rows above |

### ROLLBACK TRIGGER for the market-IV hedge (`217aff3`)

This is the largest change of the night and **the same idea failed live once
already** (`29ea36c`, reverted in `2c51f1e` after a heat with >$100k of fines).
The implementation is different — chain average, not each row's own solve — and
it is validated order-accurate with the fine charged. But watch it.

Roll back immediately if ANY of these show up over the next few heats:

- a single heat with a fine over **$40,000**
- five heats averaging a fine over **$20,000** (the forecast-IV build ran
  ~$15,000, so anything above that means it is going the wrong way)
- repeated `CANNOT HEDGE` lines in the trader log — that is the 50,000-share
  pin, which 4 heats in 120 reach by design

The rollback is one line, and it does not need a revert of the whole commit —
the measurement improvements in it are worth keeping:

```
cd volatility
sed -i 's/^HEDGE_AT_MARKET_IV = True$/HEDGE_AT_MARKET_IV = False/' vol_engine.py
python test_engine.py                       # expect 192 passed
git commit -am "revert to hedging at forecast IV"
```

`be66fd4` is the "30-40 minutes ago" save state: it is the model that produced
the $144,175 mean over 25 heats, and nothing about a heat's behaviour differs
from `fd4a94e` except the between-heats reset.

## What changed tonight, and what each is worth

Offline tooling only — no effect on trading, but everything below rests on it:

- **The backtest could not see the fine.** It charged the penalty *after*
  hedging, on the delta the hedge had just flattened: $834 a heat modelled
  against $14,080 actually charged. Now charged at the top of the tick on the
  book carried in, which is what the server samples. Ratio 0.79x, correlation
  0.53, up from 0.06x.
- **`STRUCTURE_OFF_AFTER` was invisible** — the backtest never passed
  `wing_contracts`, so 0.70/0.80/0.90 scored identically to the dollar.
- **`sweep.apply` could not reach `structure.*`** — it silently set the
  attribute on `autotrader`, so every wing sweep ever run reported the
  reference score as the candidate's.

Live path:

- **Cross-heat state leak** (`fd4a94e`). `_gap_ema` was seeded once per
  *process*, not per *heat*, so a trader left running opened each new heat at
  full size on the last heat's view. `overnight.py` hid it by spawning a fresh
  trader per heat; the competition, where one trader sees ten heats, would not
  have. This is the most important fix of the night and it is a correctness
  fix, not a tuning one.
- **`NO_NEW_AFTER` 0.90 -> 0.87** (`454ba6c`). +$1,421 a heat (t 2.2) and the
  fine falls from $9,740 to $7,677. Broad plateau, interior optimum.

## Measured, and deliberately NOT shipped

- **`WING_NET_BUFFER`.** The backtest likes 0 (+$5,902, t 11.9) — and the
  order-accurate replay produces an **illegal book** on it, which the server
  refuses. The surface is a sawtooth (0: +5,902, 25: -9,576, 50: 0, 75:
  -12,484), so it is resolving the backtest's own atomic sequencing, not
  economics. **Left at 50.** Do not ship a knife edge.
- **Wider hedge band 6,900/6,500.** STATE.md records +$4,307 (t 3.3), never
  shipped. With the fine priced it is **-$3,516 (t -3.6)**: saves $2,643 of
  friction, pays $7,295 more in fines. The shipped 5,000/2,000 is the optimum
  of the whole 16-point grid. **Do not ship this.**
- **`STRIKE_SWITCH_DISTANCE` 2.0.** Strike switching costs $17,321 a heat, the
  largest single line of friction — but widening it is -$5,896 order-accurate.
  It pays for itself. **Left at 1.5.**

## Rules being followed unattended

1. Nothing ships without a **paired** comparison over the whole library, and a
   **plateau** around the winner. Single heats are noise at a ~$60k spread.
2. Anything the backtest likes gets checked against `replay_trader.py`, which
   enforces both option limits after every order. The backtest moves the whole
   book at once and will happily score a book the server would reject.
3. One change per commit, so any one of them can be reverted alone.
4. `python test_engine.py` before every commit. 192 checks.
