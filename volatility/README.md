# RITCx Volatility Trading Case

Decision-support tooling for the RITCx Volatility Trading Case (CMU, 19 Sep 2026).

See **[STATE.md](STATE.md)** for the full build log, verified case parameters,
design decisions and open questions. This file is just how to run it.

## Requirements

Python 3.13 with `requests`, `pandas`, `numpy`, `scipy`, `py_vollib` — all
already installed on this machine.

```
pip install requests pandas numpy scipy py_vollib
```

## Before a heat

Start the RIT client, enable the REST API on port 9999, then:

```
python test_engine.py                # 87 offline checks, no server needed
python dashboard.py --dry-run        # confirm it connects and parses correctly
```

`--dry-run` never sends an order. Use it to check that the ten tickers appear,
the case length is what you expect, and the news is parsing, **before** trusting
it with real orders.

### API key

The key is read from `RIT_API_KEY`, else from `api_key.txt` next to `config.py`,
else Rotman's `Rotman` default. `api_key.txt` is **gitignored** — it's a
per-machine credential and does not belong in the repo.

## Automated trading

```
python autotrader.py            # DRY RUN - prints decisions, sends nothing
python autotrader.py --live     # actually trades
python autotrader.py --live --max-legs 300 --verbose
```

It holds an **at-the-money straddle** sized by how far our forecast disagrees
with the market, and auto-hedges delta with RTM. A straddle is chosen over
single options because it is ~delta-neutral by construction and ATM carries the
most vega — pure volatility exposure that can't trap itself outside the delta
band. (Learned the hard way: a 500-lot of `RTM52P` at delta −0.987 produced
−49,342 delta with almost no vega, and couldn't be hedged within the share
limit. See STATE.md.)

| Behaviour | Value |
|---|---|
| Enter when | vol gap ≥ 1.0 points |
| Full size at | vol gap ≥ 4.0 points |
| Exit when | vol gap < 0.4 points (the maker has learned) |
| Max size | 500 contracts/leg — 2×500 hits the 1,000 net limit |
| Stops opening | last 10% of the case, where ATM delta flips violently |

Safety: dry run by default, never opens what it can't hedge, respects live
gross/net limits, refuses to trade without a two-sided quote, Ctrl-C exits
cleanly.

## Manual front ends

Two front ends, same engine. Pick either, or run both.

**Overlay** — floats on top of the RIT client so you read the analytics while
trading in RIT's own BookTrader. Never takes keyboard focus, so your typing
always goes to RIT. Drag by the title bar, right-click to close.

```
python overlay.py                        # display only (safe default)
python overlay.py --trade                # arm buttons + hotkeys
python overlay.py --trade --auto-hedge   # also manages delta in background
python overlay.py --geometry +1200+700 --opacity 0.85
```

RIT has no plugin API — its workspace format only accepts its own fixed window
types — so this is as close to "inside RIT" as is possible.

**Trading is off until you pass `--trade`.** Once armed there are two ways to
fire, and the difference matters in a 5-minute heat:

| | |
|---|---|
| **Buttons** | Obvious, but clicking the panel takes focus off RIT, so you have to click back into RIT afterwards |
| **Hotkeys** | Registered system-wide, so they fire **without leaving RIT**. Use these during a heat |

| Hotkey | Action |
|---|---|
| `ctrl+alt+B` | Buy the best buy-edge option |
| `ctrl+alt+S` | Sell the best sell-edge option |
| `ctrl+alt+H` | Hedge delta back inside the band |
| `ctrl+alt+F` | Flatten delta to zero |

`ctrl+alt` combinations are used specifically because RIT will never interpret
them as ordinary typing. Hotkeys are registered only with `--trade`, so an
unarmed overlay cannot fire anything. If one is already claimed by another app
the panel says so rather than failing silently.

Both paths refuse to trade when there's no live two-sided quote.

**Console dashboard** — fuller table, keyboard trading.

```
python dashboard.py
```

| Key | Action |
|---|---|
| `b` | BUY the option with the best buy edge |
| `s` | SELL the option with the best sell edge |
| `d` | Dispersion pair — sell richest IV, buy cheapest |
| `h` | Hedge delta back inside the band |
| `f` | Flatten delta to zero |
| `a` | Toggle auto-hedge |
| `+` / `-` | Adjust order size |
| `c` | Cancel all open orders |
| `q` | Quit |

**Option trades need a keypress. The delta hedge is automatic.** The penalty is
$0.10 per delta per *second*, and hedging needs no judgement, so the human stays
out of that loop. Disable with `--manual-hedge` if you'd rather drive it.

### Reading the table

- `MKT_IV` — implied vol from the quote midpoint.
- `FAIR` — Black-Scholes price at our blended forecast.
- `BUY_ED` / `SELL_ED` — edge **after** crossing the spread and paying
  commission. Positive means the trade makes money at the touch, not in theory.
- `DISP` — this strike's IV minus the (vega-weighted) chain average. All ten
  options share one expiry and one pricing model with no smile, so in
  equilibrium every `DISP` should be zero. Non-zero *can* be the market maker's
  own inconsistency, which is capturable without our forecast being right — but
  read the next line before trusting it.
- `noisy ±x%` — the option's vega is too low for the 2-cent quote to pin its
  implied vol to better than `MAX_IV_SPREAD`. These are excluded from trading,
  from dispersion, and from the market average. Measured live, two cents was
  worth 0.63 vol points on RTM48C but **17.5 vol points** on RTM52P, and the
  apparent cross-strike dispersion was entirely this artefact. At $2/contract,
  a 2-cent option also pays 100% of its premium in commission.

## Choosing the tail-week prior

`TAIL_VOL_MODE` in `config.py` controls what the forecast assumes for weeks the
news hasn't announced yet (`market`, `fixed`, or `persist`). Which is best is an
empirical question — settle it with real data:

```
python record.py --name heat1        # run during a practice heat, read-only
python replay.py heats/*.jsonl       # scores every mode on identical ticks
```

`replay.py` reports two independent measures: forecast error against the
volatility RTM actually went on to realise, and replayed P&L over the real
recorded quotes. Record 3–4 heats before trusting the answer.

## Practice server

`flserver.rotman.utoronto.ca` — port 16590 (Windows RIT), 16595 (browser/Mac).
Trader `jobe-2`, password `exercise`. Live since 11 Sep through competition day.

## Files

| File | Role |
|---|---|
| `config.py` | All tunables; SPEC vs TUNABLE values marked separately |
| `rit_api.py` | REST wrapper; fails loudly, never returns a silent default |
| `news_parser.py` | Volatility announcements → `{week: sigma}` |
| `vol_engine.py` | IV, greeks, integrated-variance blend, edges, hedging (pure) |
| `dashboard.py` | Live console |
| `record.py` | Read-only capture of a heat → `heats/*.jsonl` |
| `replay.py` | Scores volatility models on recorded heats |
| `test_engine.py` | 87 offline checks |

`RITCx Volatility Trading Case base script-REST API.py` is Rotman's original
template, kept for reference only — it is not on the execution path. See
STATE.md for the bugs in it that this build does not inherit.
