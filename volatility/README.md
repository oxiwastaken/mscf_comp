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

## During a heat

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
- `DISP` — this strike's IV minus the chain average. All ten options share one
  expiry and one pricing model with no smile, so in equilibrium every `DISP`
  should be zero. **Non-zero is the market maker's own inconsistency, and
  capturing it doesn't require our forecast to be right.**

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
