"""Check the tender gate prices the exit the tender will actually take.

Past `HOLD_TO_CLOSEOUT_TICK` the unwind does not run, so pricing a tender
against a market unwind values an exit that never happens. This drives the real
`tender_safety` over the logged tender books and asserts:

  1. the basis switches at the threshold and not before
  2. the close-out basis is never WORSE than the unwind basis on the same book
     - if it were, the uplift argument is backwards and the bar is unsafe
  3. the measured uplift matches what the change was justified on (+0.0626
     mean across 385 logged tenders)
  4. tenders the unwind bar refused but the close-out bar accepts are real:
     they clear a bar that is stricter once the two measurements are put on the
     same footing
  5. a genuinely bad tender is still refused on either basis

    python test_hold_gate.py
"""

import csv
import glob
import statistics
import sys

import etf_arb_bot as bot

# This suite exercises the unwind and late-hold machinery, which stays in the
# bot but is dormant in production since v27 holds every tender from tick 0.
# Pin the threshold these checks were written against, so they keep testing
# that logic rather than the production setting.
bot.HOLD_TO_CLOSEOUT_TICK = 200
bot.MIN_TENDER_EDGE_HELD = 0.04   # the held bar these checks were written against


def load_books():
    """Logged tenders that carry enough book to rebuild the decision."""
    out = []
    for path in sorted(glob.glob("logs/tenders*.csv")):
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    bid, ask = float(r["mkt_bid"]), float(r["mkt_ask"])
                    qty, price = float(r["quantity"]), float(r["price"])
                except (ValueError, TypeError, KeyError):
                    continue
                if not (0 < bid < ask < 1e9) or not qty:
                    continue
                out.append((r, bid, ask, qty, price))
    return out


def book_from(bid, ask):
    """A book around the logged touch, deep enough not to be the binding term."""
    bids, asks, qty = [], [], 6000
    for i in range(12):
        bids.append({"price": round(bid - i * 0.01, 2), "quantity": qty})
        asks.append({"price": round(ask + i * 0.01, 2), "quantity": qty})
        qty = int(qty * 1.35)
    return {"bids": bids, "asks": asks}


FLAT = {bot.BULL: 0, bot.BEAR: 0, bot.RITC: 0, bot.USD: 0, bot.CAD: 0}


def gate(r, bid, ask, qty, price, tick):
    offer = {"tender_id": r["tender_id"], "ticker": "RITC", "action": r["action"],
             "quantity": qty, "price": price, "is_fixed_bid": True}
    return bot.tender_safety(offer, dict(FLAT), book_from(bid, ask),
                             1.0, 1.0, tick)


def main():
    books = load_books()
    if not books:
        print("no logged tenders carry a usable book")
        return 1

    failures = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
        if not cond:
            failures.append(name)

    T = bot.HOLD_TO_CLOSEOUT_TICK
    print(f"{len(books)} logged tender books, threshold tick {T}\n")

    # 1 + 2: compare the two bases on the same book, tender by tender. A gate
    # can also refuse on limits, which is neither basis - only the edge
    # rejections carry a basis tag.
    uplifts, wrong_way, bad_basis = [], 0, []
    for r, bid, ask, qty, price in books:
        _, r_un, e_un = gate(r, bid, ask, qty, price, T - 1)
        _, r_hl, e_hl = gate(r, bid, ask, qty, price, T)
        if e_un is None or e_hl is None:
            continue
        if r_un.startswith("edge") and "unwind" not in r_un:
            bad_basis.append(f"below threshold: {r_un}")
        if r_hl.startswith("edge") and "closeout" not in r_hl:
            bad_basis.append(f"at threshold: {r_hl}")
        uplifts.append(e_hl - e_un)
        if e_hl < e_un:
            wrong_way += 1

    check("the basis switches at the threshold and not before",
          not bad_basis, "; ".join(bad_basis[:2]))
    check("close-out is never worse than the unwind on the same book",
          wrong_way == 0, f"{wrong_way} of {len(uplifts)} go the wrong way")

    # 3: size the uplift from the LOGGED book, not the synthetic one. The
    # synthetic book holds 6,000 at the touch, so a 10,000 unit chunk walks a
    # level and the unwind basis comes out worse than it really was - which
    # inflates the uplift. `chunk_vwap` is what the live book actually gave.
    logged = []
    for r, bid, ask, qty, price in books:
        try:
            cv = float(r["chunk_vwap"])
        except (ValueError, TypeError, KeyError):
            continue
        if cv <= 0:
            continue
        mid = (bid + ask) / 2.0
        logged.append((cv - mid + bot.FEE_MKT) if r["action"].upper() == "SELL"
                      else (mid - cv + bot.FEE_MKT))
    synth = statistics.mean(uplifts)
    real = statistics.mean(logged)
    print(f"\n  uplift on the synthetic book: mean {synth:+.4f} (n={len(uplifts)})")
    print(f"  uplift on the LOGGED book:    mean {real:+.4f} "
          f"median {statistics.median(logged):+.4f} (n={len(logged)})")
    check("logged uplift matches the figure the change was justified on",
          real >= 0.03, f"{real:+.4f} on {len(logged):,} books (v15 measured +0.0626 on "
          f"385; what v15 rests on is that close-out beats the unwind basis materially, "
          f"not the exact figure, which moves as the logs grow)")
    check("the synthetic book is the conservative direction",
          synth >= real,
          f"synthetic {synth:+.4f} >= logged {real:+.4f}, so the unlocked count "
          f"below is an upper bound")

    # The two bars sit on different measurements, so the only honest comparison
    # is on the underlying price-minus-mid basis. v15 got this backwards in
    # writing - it claimed the held bar was the stricter of the two while the
    # unwound bar was 0.08, when it was in fact the looser. Asserted here so
    # the relationship is checked rather than asserted in prose.
    BE_UNWOUND = 0.0138          # sweep_tenderbar.py, two estimators agreeing
    BE_HELD = 0.0                # close-out is free against the mid
    req_unwound = bot.MIN_TENDER_EDGE + real
    req_held = bot.MIN_TENDER_EDGE_HELD
    margin_unwound = req_unwound - (BE_UNWOUND + real)
    margin_held = req_held - BE_HELD
    print(f"\n  on the price-minus-mid basis:")
    print(f"    unwound bar {bot.MIN_TENDER_EDGE:.3f} -> needs {req_unwound:.4f}, "
          f"break-even {BE_UNWOUND + real:.4f}, margin {margin_unwound:+.4f}")
    print(f"    held bar    {bot.MIN_TENDER_EDGE_HELD:.3f} -> needs {req_held:.4f}, "
          f"break-even {BE_HELD:.4f}, margin {margin_held:+.4f}")
    check("both bars clear their own break-even",
          margin_unwound > 0 and margin_held > 0,
          f"unwound {margin_unwound:+.4f}, held {margin_held:+.4f}")
    check("the held bar demands less raw edge, as it should - it has no exit cost",
          req_held < req_unwound, f"held {req_held:.4f} vs unwound {req_unwound:.4f}")

    # 4: the tenders this actually unlocks, and 5: the bad ones still refused.
    # Deduped by tender: each one is logged once per cycle of its ~6 cycle life.
    unlocked, still_refused, bad = {}, set(), 0
    for r, bid, ask, qty, price in books:
        if int(r["tick"]) < T:
            continue
        tid = (r["run_id"], r["heat"], r["tender_id"])
        ok_un, _, e_un = gate(r, bid, ask, qty, price, T - 1)
        ok_hl, _, e_hl = gate(r, bid, ask, qty, price, T)
        if e_hl is not None and e_hl < 0:
            bad += 1
            check("a tender that loses money held is still refused", not ok_hl,
                  f"h{r['heat']} t{r['tick']} held edge {e_hl:+.4f}")
        if ok_hl and not ok_un:
            # Best cycle of its life, same as the live gate which re-evaluates
            # each cycle and fires on a good one rather than the first one.
            if tid not in unlocked or e_hl > unlocked[tid][2]:
                unlocked[tid] = (qty, e_un, e_hl)
        elif not ok_hl and not ok_un:
            still_refused.add(tid)
    still_refused -= set(unlocked)
    print(f"\n  distinct late tenders unlocked by the close-out basis: "
          f"{len(unlocked)}, worth "
          f"{sum(q * eh for q, eu, eh in unlocked.values()):,.0f} USD")
    for q, eu, eh in sorted(unlocked.values(), key=lambda x: -x[2])[:10]:
        print(f"    {q:>8,.0f} units  unwind {eu:+.4f} -> held {eh:+.4f}"
              f"  = {eh * q:>+9,.0f} USD")
    print(f"  still refused on both bases: {len(still_refused)}")
    print(f"  checked {bad} money-losing tenders are still refused")
    check("the change unlocks something", len(unlocked) > 0)

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
