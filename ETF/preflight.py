"""Check this machine, and this server, before trading a real heat.

Two jobs.

CAN WE CONNECT - credentials present, auth accepted, case reachable.

DO THE BOT'S ASSUMPTIONS STILL HOLD - and this is the one that matters. Nearly
every constant in `etf_arb_bot.py` is a number copied from the case PDF: the
0.02 fee, the 0.01 rebate, the 10,000 order cap, the 300,000 gross and 200,000
net limits, the ETF's x2 multiplier, 300 ticks a heat. The case says outright
that iterations differ - *"Potential parameter changes include factors such as
the spread of tender orders, liquidity, and volatility"* - and the API reports
every one of those numbers per security. So they can be checked rather than
assumed.

A mismatch here is not cosmetic. `MAX_GROSS` is derived from the gross limit
and reserves 200,000 for tenders; if the real limit moved, the reserve is wrong
and tenders get refused or fines get paid. `TENDER_GROSS_RESERVE` assumes the
ETF counts double.

Read-only: it places no orders and accepts no tenders.

    python preflight.py
"""

import sys

import requests

import etf_arb_bot as bot

CHECKS = []


def check(name, ok, detail="", fatal=False):
    CHECKS.append((name, ok, detail, fatal))
    mark = "OK  " if ok else ("FAIL" if fatal else "WARN")
    print(f"  [{mark}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def num(d, key):
    try:
        return float(d.get(key))
    except (TypeError, ValueError):
        return None


def main():
    print("=" * 72)
    print(" PREFLIGHT")
    print("=" * 72)

    print("\nCREDENTIALS")
    if not (bot.USERNAME and bot.PASSWORD):
        check("ETF/.env present with RIT_USER and RIT_PASS", False,
              "copy .env.example to .env and fill it in", fatal=True)
        print("\nCannot continue without credentials.")
        return 1
    check("ETF/.env present with RIT_USER and RIT_PASS", True,
          f"user {bot.USERNAME}")

    with requests.Session() as s:
        s.headers.update(bot.authorization())

        print("\nCONNECTION")
        try:
            case = bot.api_request(s, "GET", "case")
        except Exception as e:                       # noqa: BLE001
            check("GET /case", False, f"{type(e).__name__}: {e}", fatal=True)
            return 1
        if case is None:
            check("GET /case", False, "auth rejected - check the password",
                  fatal=True)
            return 1
        check("GET /case", True,
              f"{case.get('name')} tick {case.get('tick')} "
              f"status {case.get('status')}")

        print("\nCASE SHAPE")
        tpp = case.get("ticks_per_period")
        check("300 ticks a heat", tpp == 300,
              f"server says {tpp}; HOLD_TO_CLOSEOUT_TICK="
              f"{bot.HOLD_TO_CLOSEOUT_TICK} assumes 300")
        enforced = case.get("is_enforce_trading_limits")
        check("trading limits enforced by the server", bool(enforced),
              "limits are NOT enforced - breaches become fines, not rejections"
              if not enforced else "orders that would breach are rejected")

        print("\nLIMITS")
        try:
            limits = bot.api_request(s, "GET", "limits") or []
        except Exception as e:                       # noqa: BLE001
            limits = []
            check("GET /limits", False, f"{type(e).__name__}: {e}")
        for lim in limits:
            g, n = num(lim, "gross_limit"), num(lim, "net_limit")
            check(f"gross limit == {bot.SERVER_GROSS_LIMIT:,}",
                  g == bot.SERVER_GROSS_LIMIT,
                  f"{lim.get('name')}: server {g:,.0f}" if g else "missing",
                  fatal=True)
            check(f"net limit == {bot.SERVER_NET_LIMIT:,}",
                  n == bot.SERVER_NET_LIMIT,
                  f"{lim.get('name')}: server {n:,.0f}" if n else "missing",
                  fatal=True)
            gf, nf = num(lim, "gross_fine"), num(lim, "net_fine")
            print(f"        fines: gross {gf}/unit, net {nf}/unit; "
                  f"currently gross {num(lim, 'gross'):,.0f} "
                  f"net {num(lim, 'net'):,.0f}")

        print("\nPER-SECURITY PARAMETERS")
        try:
            secs = bot.api_request(s, "GET", "securities") or []
        except Exception as e:                       # noqa: BLE001
            secs = []
            check("GET /securities", False, f"{type(e).__name__}: {e}")
        by = {x.get("ticker"): x for x in secs}
        for t in (bot.BULL, bot.BEAR, bot.RITC):
            d = by.get(t)
            if not d:
                check(f"{t} present", False, "not in /securities", fatal=True)
                continue
            fee, reb = num(d, "trading_fee"), num(d, "limit_order_rebate")
            mx, mult = num(d, "max_trade_size"), num(d, "unit_multiplier")
            check(f"{t} fee == {bot.FEE_MKT}", fee == bot.FEE_MKT,
                  f"server {fee}")
            check(f"{t} rebate == {bot.REBATE_LMT}", reb == bot.REBATE_LMT,
                  f"server {reb} - the whole passive unwind rests on this")
            check(f"{t} max order == {bot.MAX_SIZE_EQUITY:,}",
                  mx == bot.MAX_SIZE_EQUITY, f"server {mx:,.0f}" if mx else "?")
            want = 2 if t == bot.RITC else 1
            check(f"{t} counts x{want} toward limits", mult == want,
                  f"server x{mult:.0f}" if mult is not None else "not reported")
            delay = d.get("execution_delay_ms")
            if delay:
                print(f"        {t} execution delay {delay}ms, "
                      f"{d.get('api_orders_per_second')} orders/sec")

        print("\nDERIVED CONFIG")
        check("gross reserve leaves room for the largest tender",
              bot.MAX_GROSS + 2 * 100000 <= bot.SERVER_GROSS_LIMIT - bot.LIMIT_BUFFER,
              f"arb cap {bot.MAX_GROSS:,} + 200,000 vs "
              f"{bot.SERVER_GROSS_LIMIT - bot.LIMIT_BUFFER:,}")
        check("hold threshold sits inside the heat",
              0 < bot.HOLD_TO_CLOSEOUT_TICK < (tpp or 300),
              f"{bot.HOLD_TO_CLOSEOUT_TICK} of {tpp}")

        print("\nSTATE")
        try:
            tr = bot.api_request(s, "GET", "trader") or {}
            print(f"        NLV {num(tr, 'nlv'):,.2f}   "
                  f"total fines {num(tr, 'total_fines'):,.2f}")
            pos = {t: int(by.get(t, {}).get("position") or 0)
                   for t in (bot.BULL, bot.BEAR, bot.RITC, bot.USD)}
            gross = abs(pos[bot.BULL]) + abs(pos[bot.BEAR]) + 2 * abs(pos[bot.RITC])
            print(f"        position BULL {pos[bot.BULL]:+,} "
                  f"BEAR {pos[bot.BEAR]:+,} RITC {pos[bot.RITC]:+,} "
                  f"USD {pos[bot.USD]:+,} -> gross {gross:,}")
            open_orders = bot.api_request(s, "GET", "orders",
                                          params={"status": "OPEN"}) or []
            check("no stale open orders from a previous run",
                  not open_orders, f"{len(open_orders)} resting")
        except Exception as e:                       # noqa: BLE001
            check("state readable", False, f"{type(e).__name__}: {e}")

    fails = [c for c in CHECKS if not c[1] and c[3]]
    warns = [c for c in CHECKS if not c[1] and not c[3]]
    print("\n" + "=" * 72)
    print(f" RISK_MODEL {bot.RISK_MODEL}   bar {bot.MIN_TENDER_EDGE} / "
          f"held {bot.MIN_TENDER_EDGE_HELD}   hold@{bot.HOLD_TO_CLOSEOUT_TICK}"
          f"   arb gross cap {bot.MAX_GROSS:,}")
    if fails:
        print(f" {len(fails)} FATAL mismatch(es) - do not trade until resolved:")
        for n, _, d, _ in fails:
            print(f"   - {n}: {d}")
    elif warns:
        print(f" {len(warns)} warning(s) - readable but check before a scored heat:")
        for n, _, d, _ in warns:
            print(f"   - {n}: {d}")
    else:
        print(" all checks passed - safe to run etf_arb_bot.py")
    print("=" * 72)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
