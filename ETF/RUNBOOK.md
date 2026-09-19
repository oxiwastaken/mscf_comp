# Competition runbook — ETF arbitrage case

The version being run is **`v27_holdall`**, tagged `etf-v27-competition`.

## What it does

Holds **every** tender to close-out — `HOLD_TO_CLOSEOUT_TICK = 0`, so the
unwind never sends an order. A tender is scored against the **mid** with no fee
(close-out is free and exact, proven live to 0.02%) and must clear
`MIN_TENDER_EDGE_HELD = 0.08`. The swap dumps just enough inventory to fit a
blocked tender when its edge pays for the dump. Market making is off. The
spread arb runs inside a 97,000 gross cap so it can never be the reason a
tender is refused.

`BAR_ADD_SAME_WAY = 0.0` — the v28 anti-doubling-down rule is **off**, by
decision. See VERSIONS.md v28 for what that costs and buys.

## Accepted tradeoffs

Running v27 means accepting its distribution, not just its average. Over 105
replayed heats:

| | |
|---|---|
| mean | +63,172 |
| median | +64,865 |
| losing heats | 22/105 (21%) |
| worst 5% average | −87,600 |
| worst heat | −110,945 |

A large losing heat is a property of the strategy, not a malfunction. It comes
from carrying a ~97,000-unit position to close-out while the ETF drifts: the
edge on that position is ~10,000 and a heat's drift is worth several times
that. Seven controls were tested against it; six made things worse and the one
that helped is off by choice. **Do not treat a −90,000 heat as a bug.**

## Before the first heat

1. **Credentials.** `ETF/.env` is gitignored and must exist:

   ```
   RIT_USER=your_trader_id
   RIT_PASS=your_password
   ```

   Write it without putting the password in a shell history or a chat:

   ```
   notepad ETF\.env
   ```

2. **Preflight** — read-only, places no orders:

   ```
   cd ETF
   python preflight.py
   ```

   It checks auth, then every constant the strategy is calibrated on against
   what the server actually reports: fee, rebate, max order size, gross and net
   limits and their fine rates, the ETF's ×2 multiplier, ticks per heat, and
   whether limits are enforced or merely fined. **A FATAL line means do not
   trade** — `MAX_GROSS` is derived from the gross limit, so if the server's
   limits moved, the tender reserve is wrong. The case states outright that
   iterations vary liquidity and spreads, so re-run it whenever the iteration
   changes.

3. **Offline suites** — no credentials needed, a few seconds:

   ```
   python test_cycle.py && python test_prehedge.py && python test_hold_gate.py
   python test_swap.py && python test_mm.py && python test_reconcile.py
   python test_holdall.py && python test_encoding.py && python sim_unwind.py
   ```

## Running

```
cd ETF
python etf_arb_bot.py
```

It waits for the case to go ACTIVE, runs heat after heat on its own, and
survives the gap between them. `Ctrl+C` once shuts it down cleanly after the
current cycle so the CSVs close properly — do not kill the window, the logs
are the only record.

## Windows notes

- **Python 3.13.7, `requests` 2.32.5** — the only third-party dependency.
- **Console and file encoding.** This machine's `locale.getpreferredencoding()`
  is **cp1252**. Tender captions and server error strings are free text we do
  not control and both reach `print()` and the CSVs. A character outside
  cp1252 raised `UnicodeEncodeError` on the unfixed build, and that is not an
  `ApiException` — it skips the per-cycle handler and `run_heat`'s
  `except Exception` abandons the rest of the heat. All logs are now opened
  `encoding="utf-8"` and stdout/stderr are reconfigured at import.
  `test_encoding.py` guards it.
- **Only one bot per trader account.** Two instances on the same account fight
  over the same position and limits. Settle which machine runs before starting.

## While it runs

Console prints each cycle's edges and NLV, every tender decision with its
reason, and a per-heat summary. Afterwards:

```
python heat_report.py        # standing against the 40,000/heat target
python backtest.py           # per-heat attribution -> logs/scoreboard.csv
python analyze_tenders.py    # realised tender P&L
python decompose_unwind.py   # execution vs drift
```

`logs/` is the record — `ticks.csv`, `trades.csv`, `tenders.csv`, `heats.csv`.
The case wipes its own order history on reset, so anything not written during
the heat is gone.

## If something goes wrong

| symptom | meaning |
|---|---|
| `Authentication failed` | wrong `RIT_USER`/`RIT_PASS`; the bot stops on purpose |
| `Rate limit exceeded` | handled — it waits out `Retry-After` and retries |
| `Network error … retry n/6` | handled — backoff, then it gives up the cycle, not the heat |
| `Heat N crashed (…) - continuing` | the cycle raised something unexpected; the heat is abandoned but the run continues. **Capture the console text** — this is the line that matters |
| a large negative heat | expected behaviour, see the tradeoffs above |
