"""
Parameter sweep over the recorded heats.

    python sweep.py --validate            # backtest vs live NLV, same heats
    python sweep.py --grid                # sweep the sizing parameters

`backtest.py` answers "which rebalancing policy?" on a handful of heats. This
answers "what should the tunables be?" over the whole overnight library, which
is the only way to see past the enormous heat-to-heat variance (identical code
has returned -$15k and +$162k).

Everything here calls `backtest.run`, so the simulated fills, fees and delta
penalty are exactly the ones already validated against live trading.
"""

import argparse
import csv
import glob
import os
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

import autotrader as at
import backtest
import config
import structure
from replay import load

# One-at-a-time sweeps. Each entry is a parameter and the values to try; the
# current production value is included so the comparison is like-for-like on
# identical heats, which is the only valid way to read these numbers.
AXES = {
    'FULL_SIZE_VOL_EDGE': (0.008, 0.010, 0.012, 0.015, 0.020, 0.025),
    'ENTRY_VOL_EDGE': (0.002, 0.004, 0.006, 0.008, 0.012),
    'EXIT_VOL_EDGE': (0.000, 0.001, 0.003, 0.005),
    'NO_NEW_AFTER': (0.85, 0.90, 0.95, 0.98, 1.00),
    'GAP_SMOOTHING': (0.08, 0.15, 0.25, 0.40, 1.00),
    'STRIKE_SWITCH_DISTANCE': (0.6, 1.0, 1.5, 2.5),
    'REBALANCE_FRACTION': (0.10, 0.25, 0.40),
    'config.HEDGE_TRIGGER_DELTA': (3000, 5000, 6500),
    'config.HEDGE_TARGET_DELTA': (0, 2000, 4000),
}

# The recordings are shared with the base model rather than copied: they are
# 325MB of identical market data, and both models must be scored on the SAME
# heats or the comparison between them means nothing. Anything recorded into
# this folder's own `heats/` is picked up too.
HEATS = ('../volatility/heats/*.jsonl', 'heats/*.jsonl')
RESULTS = '../volatility/results.csv'


def tag_of(path):
    """`heats/live_010635.jsonl` -> `010635`, the tag used in results.csv."""
    return os.path.basename(path).replace('live_', '').replace('.jsonl', '')


def live_results():
    """{tag: nlv} for heats that ran a full 299+ ticks and actually traded."""
    out = {}
    try:
        with open(RESULTS, encoding='utf-8') as handle:
            for row in csv.DictReader(handle):
                if int(row['ticks']) >= 299 and int(row['contracts']) > 1000:
                    out[row['tag']] = int(row['nlv'])
    except OSError:
        pass
    return out


def usable_heats(min_ticks=250):
    """Recorded heats long enough to be worth scoring."""
    found = []
    for pattern in HEATS:
        found.extend(glob.glob(pattern))
    paths = []
    for path in sorted(found, key=os.path.basename):
        with open(path, encoding='utf-8') as handle:
            n = sum(1 for _ in handle)
        if n >= min_ticks:
            paths.append(path)
    return paths


def apply(settings):
    """
    Push a settings dict onto the live modules.

    Keys are attribute names on `autotrader`, or `config.NAME` / `structure.NAME`
    for the tunables that live elsewhere (the hedge band and option limits in
    `config`, the gross-capacity structure in `structure`).
    """
    modules = {'config.': config, 'structure.': structure}
    for key, value in settings.items():
        for prefix, module in modules.items():
            if key.startswith(prefix):
                setattr(module, key[len(prefix):], value)
                break
        else:
            setattr(at, key, value)


def score_one(args):
    """Run one heat under one settings dict. Module-level for pickling."""
    path, settings, policy = args
    apply(settings)
    snapshots = load(path)
    book, _ = backtest.run(snapshots, policy)
    return tag_of(path), book.cash, book.fees + book.spread, book.penalty


def score(paths, settings=None, policy='news', workers=None):
    """Mean P&L across `paths` under `settings`. Returns (stats, per-heat)."""
    settings = settings or {}
    jobs = [(p, settings, policy) for p in paths]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(score_one, jobs, chunksize=4))
    values = [r[1] for r in rows]
    stats = {
        'mean': statistics.mean(values),
        'median': statistics.median(values),
        'worst': min(values),
        'sd': statistics.pstdev(values),
        'n': len(values),
        'friction': statistics.mean([r[2] for r in rows]),
        'penalty': statistics.mean([r[3] for r in rows]),
    }
    return stats, rows


def paired(rows_a, rows_b):
    """
    Mean per-heat difference b - a, and its standard error.

    Heat-to-heat variance here is enormous (sd ~$61k on a ~$115k mean), so an
    unpaired comparison of two means cannot resolve anything smaller than about
    $9k. Every run scores the SAME recordings, so the difference can be taken
    heat by heat, which cancels the opportunity variance and leaves a standard
    error roughly an order of magnitude smaller. `t` past ~2 is a real effect.
    """
    a = {tag: value for tag, value, _f, _p in rows_a}
    diffs = [value - a[tag] for tag, value, _f, _p in rows_b if tag in a]
    mean = statistics.mean(diffs)
    stderr = statistics.pstdev(diffs) / (len(diffs) ** 0.5) if len(diffs) > 1 else 0.0
    return mean, stderr, (mean / stderr if stderr else 0.0)


def grid(paths, base, name_a, values_a, name_b, values_b, valid=None):
    """Joint sweep of two parameters, scored against `base` as the reference."""
    reference, ref_rows = score(paths, base)
    print(f'  reference ${reference["mean"]:,.0f}   '
          f'({name_a}={base.get(name_a, getattr(at, name_a, "?"))}, '
          f'{name_b}={base.get(name_b, getattr(at, name_b, "?"))})\n')
    print(f'  {name_a:>10} {name_b:>10} {"mean":>12} {"vs ref":>12} '
          f'{"t":>6} {"worst":>11} {"friction":>10}')
    best = None
    for a in values_a:
        for b in values_b:
            if valid and not valid(a, b):
                continue
            settings = dict(base, **{name_a: a, name_b: b})
            stats, rows = score(paths, settings)
            delta, _stderr, t = paired(ref_rows, rows)
            print(f'  {a:>10} {b:>10} ${stats["mean"]:>11,.0f} '
                  f'{delta:>+12,.0f} {t:>6.1f} ${stats["worst"]:>10,.0f} '
                  f'${stats["friction"]:>9,.0f}')
            if best is None or stats['mean'] > best[0]:
                best = (stats['mean'], a, b)
    print(f'\n  -> best {name_a}={best[1]} {name_b}={best[2]} at ${best[0]:,.0f}\n')


# "Punt" variants: deliberately higher-variance books, to see whether trading
# expected P&L for spread buys a better chance at a hard target.
#
# Every one of these is reachable through existing parameters - none of them
# needs new strategy code. That is deliberate: if a risk dial cannot be
# expressed as a setting, it is a new strategy and belongs behind its own test,
# not bolted onto this one.
RAMP = {'ENTRY_VOL_EDGE': 0.002, 'FULL_SIZE_VOL_EDGE': 0.008}
PUNTS = {
    # Ride the whole free delta band instead of hedging back to the middle.
    # Delta inside +/-7,000 costs nothing, so this is variance at no fee cost.
    'unhedged band': {'config.HEDGE_TRIGGER_DELTA': 6900,
                      'config.HEDGE_TARGET_DELTA': 6500},
    # Carry the straddle into expiry, where ATM gamma is at its most violent.
    'hold to expiry': {'NO_NEW_AFTER': 1.00},
    # Only bet when the disagreement is large, then bet the whole limit.
    'conviction only': {'ENTRY_VOL_EDGE': 0.010, 'FULL_SIZE_VOL_EDGE': 0.011},
    # Trade the raw gap with no smoothing - faster, noisier, more often wrong.
    'no smoothing': {'GAP_SMOOTHING': 1.00},
    # A forecast that ignores the market prior, so it disagrees far harder.
    'fixed prior': {'config.TAIL_VOL_MODE': 'fixed'},
    'persist prior': {'config.TAIL_VOL_MODE': 'persist'},
    # Everything at once.
    'all of it': {'config.HEDGE_TRIGGER_DELTA': 6900,
                  'config.HEDGE_TARGET_DELTA': 6500,
                  'NO_NEW_AFTER': 1.00, 'GAP_SMOOTHING': 1.00,
                  'config.TAIL_VOL_MODE': 'fixed'},
}


# Gross-capacity variants. The straddle is capped by the NET option limit at
# 1,000 contracts while the GROSS limit allows 2,500, so shorting the cheapest
# vega on the board buys net room for more of the most expensive. See
# structure.py. Each entry is (wing contracts, max vega ratio to short at).
GROSS = {
    'wings 750, any ratio': (750, 1.00),
    'wings 750, r <= 0.80': (750, 0.80),
    'wings 750, r <= 0.60': (750, 0.60),
    'wings 750, r <= 0.50': (750, 0.50),
    'wings 750, r <= 0.40': (750, 0.40),
    'wings 750, r <= 0.30': (750, 0.30),
    'wings 375, r <= 0.60': (375, 0.60),
    'wings 375, r <= 0.40': (375, 0.40),
}


def gross(paths, policy='ratchet+news'):
    """Score the gross-capacity structure against the straddle, paired."""
    off = {'structure.WING_CONTRACTS': 0}
    stats, ref_rows = score(paths, off, policy=policy)
    print(f'  reference (straddle only)  mean ${stats["mean"]:>10,.0f}   '
          f'worst ${stats["worst"]:>10,.0f}   friction ${stats["friction"]:>8,.0f}\n')
    print(f'  {"variant":<22} {"mean":>12} {"vs ref":>10} {"t":>6} '
          f'{"worst":>11} {"friction":>10} {"P(10>=150k)":>12}')
    for name, (contracts, ratio) in GROSS.items():
        settings = {'structure.WING_CONTRACTS': contracts,
                    'structure.WING_MAX_VEGA_RATIO': ratio}
        stats, rows = score(paths, settings, policy=policy)
        delta, _stderr, t = paired(ref_rows, rows)
        band = heats10(rows)
        print(f'  {name:<22} ${stats["mean"]:>11,.0f} {delta:>+10,.0f} {t:>6.1f} '
              f'${stats["worst"]:>10,.0f} ${stats["friction"]:>9,.0f} '
              f'{p_target(rows):>11.1%}')
    print()


def p_target(rows, target=150_000, draws=20000, seed=11):
    """Chance the ten-heat average clears `target`, by bootstrap."""
    import random
    rng = random.Random(seed)
    values = [value for _tag, value, _f, _p in rows]
    return sum(1 for _ in range(draws)
               if statistics.mean(rng.choices(values, k=10)) >= target) / draws


def heats10(rows, draws=20000, seed=7):
    """
    Distribution of the 10-heat average, which is what the goal is stated in.

    The competition is scored on the mean of ten heats, and single-heat variance
    here is huge, so the mean of ten is the quantity worth forecasting. Sampling
    ten recorded heats at random, with replacement, many times gives its spread
    directly - no distributional assumption needed.
    """
    import random
    rng = random.Random(seed)
    values = [value for _tag, value, _f, _p in rows]
    means = sorted(statistics.mean(rng.choices(values, k=10))
                   for _ in range(draws))
    pick = lambda q: means[int(q * len(means))]
    return {'mean': statistics.mean(means), 'p05': pick(0.05),
            'p50': pick(0.50), 'p95': pick(0.95),
            'p_250k': sum(1 for m in means if m >= 250_000) / len(means)}


def report(paths, name, settings, ref_rows=None):
    """Score one candidate config and print it against the reference."""
    stats, rows = score(paths, settings)
    band = heats10(rows)
    line = (f'  {name:<22} mean ${stats["mean"]:>10,.0f}   '
            f'sd ${stats["sd"]:>9,.0f}   worst ${stats["worst"]:>9,.0f}')
    if ref_rows:
        delta, _stderr, t = paired(ref_rows, rows)
        line += f'   vs ref {delta:>+9,.0f} (t {t:>4.1f})'
    print(line)
    print(f'  {"":<22} 10-heat average: p05 ${band["p05"]:>9,.0f}   '
          f'p50 ${band["p50"]:>9,.0f}   p95 ${band["p95"]:>9,.0f}   '
          f'P(>=250k) {band["p_250k"]:.0%}\n')
    return rows


def validate(paths):
    """Compare the backtest against what the live trader actually earned."""
    live = live_results()
    _stats, rows = score(paths)
    pairs = [(live[tag], sim) for tag, sim, _f, _p in rows if tag in live]
    if not pairs:
        print('  no overlap between recordings and results.csv')
        return
    actual = [p[0] for p in pairs]
    simulated = [p[1] for p in pairs]
    print(f'  heats matched         {len(pairs)}')
    print(f'  live mean             ${statistics.mean(actual):>11,.0f}')
    print(f'  backtest mean         ${statistics.mean(simulated):>11,.0f}')
    print(f'  ratio                  {statistics.mean(simulated) / statistics.mean(actual):>11.2f}x')
    print(f'  correlation            {statistics.correlation(actual, simulated):>11.2f}')


def sweep(paths, base=None, axes=None):
    """One-at-a-time sweep. Every run scores the same heats, so deltas are real."""
    base = dict(base or {})
    axes = axes or AXES
    for name, values in axes.items():
        print(f'  {name}')
        best, best_mean = None, float('-inf')
        for value in values:
            stats, _rows = score(paths, dict(base, **{name: value}))
            if stats['mean'] > best_mean:
                best, best_mean = value, stats['mean']
            print(f'    {str(value):>8}   mean ${stats["mean"]:>10,.0f}   '
                  f'median ${stats["median"]:>10,.0f}   '
                  f'worst ${stats["worst"]:>10,.0f}   '
                  f'friction ${stats["friction"]:>8,.0f}')
        print(f'    -> best {name} = {best} at ${best_mean:,.0f}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--validate', action='store_true')
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--sweep', action='store_true')
    parser.add_argument('--punt', action='store_true',
                        help='higher-variance variants vs the tuned config')
    parser.add_argument('--final', action='store_true',
                        help='current config vs the proposed one')
    parser.add_argument('--sizing', action='store_true',
                        help='joint grid over the entry/full-size ramp')
    parser.add_argument('--gross', action='store_true',
                        help='spend the idle gross limit on wing shorts')
    parser.add_argument('--axis', action='append',
                        help='sweep only this parameter (repeatable)')
    parser.add_argument('--limit', type=int, help='use only the first N heats')
    args = parser.parse_args()

    paths = usable_heats()
    if args.limit:
        paths = paths[:args.limit]
    print(f'\n  {len(paths)} usable recordings\n')

    if args.validate:
        validate(paths)
    if args.baseline:
        stats, _rows = score(paths)
        print(f'  baseline mean ${stats["mean"]:>11,.0f}   '
              f'median ${stats["median"]:>11,.0f}   '
              f'worst ${stats["worst"]:>11,.0f}   '
              f'sd ${stats["sd"]:>11,.0f}')
    if args.sweep or args.axis:
        axes = ({k: AXES[k] for k in args.axis} if args.axis else AXES)
        sweep(paths, axes=axes)
    if args.punt:
        print('  all variants sit on top of the 0.002/0.008 ramp\n')
        ref = report(paths, 'ramp only', RAMP)
        for name, settings in PUNTS.items():
            report(paths, name, dict(RAMP, **settings), ref)
    if args.final:
        ref = report(paths, 'current', {})
        report(paths, 'ramp 0.002/0.008',
               {'ENTRY_VOL_EDGE': 0.002, 'FULL_SIZE_VOL_EDGE': 0.008}, ref)
    if args.gross:
        gross(paths)
    if args.sizing:
        grid(paths, {},
             'ENTRY_VOL_EDGE', (0.001, 0.002, 0.004, 0.006, 0.008),
             'FULL_SIZE_VOL_EDGE', (0.002, 0.004, 0.006, 0.008, 0.012, 0.025),
             valid=lambda a, b: b > a)
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
