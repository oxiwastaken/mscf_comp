"""
Unattended overnight runner: trade every heat, record everything, log results.

    python overnight.py                 # run until stopped
    python overnight.py --dry-run       # supervise but never send orders
    python overnight.py --summary       # print results so far and exit

Uses no Claude tokens. Start it and leave it.

For each heat it:
  1. launches autotrader.py and record.py BEFORE the heat begins, so tick 1 is
     caught (the early news releases carry the largest gaps),
  2. waits for the heat to finish,
  3. captures the final NLV at the moment the case settles,
  4. kills both children so the next heat starts from clean state,
  5. appends a row to results.csv and writes a per-heat friction breakdown.

Each heat gets FRESH processes rather than one long-lived trader. That matters:
the trader carries a smoothed gap, a news counter and a working strike, and
leaking those across a heat boundary would corrupt the first ticks of the next
one. Restarting is the simplest guarantee of a clean slate.

Nothing here is allowed to crash the loop. An API blip, a dead child or a
malformed heat is logged and skipped - the run continues.
"""

import argparse
import csv
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from rit_api import ApiException, RitClient

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, 'logs')
HEATS = os.path.join(HERE, 'heats')
RESULTS = os.path.join(HERE, 'results.csv')

POLL = 0.3
FIELDS = ('finished', 'tag', 'nlv', 'friction', 'directional',
          'contracts', 'shares', 'opt_sends', 'rtm_sends', 'ticks', 'log')


def log(text):
    print(f'{datetime.now():%H:%M:%S}  {text}', flush=True)


def spawn(args, out_path):
    """Start a detached child with stdout+stderr to a file."""
    handle = open(out_path, 'w', encoding='utf-8')
    proc = subprocess.Popen(
        [sys.executable] + args, cwd=HERE,
        stdout=handle, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    proc._log_handle = handle
    return proc


def stop(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc._log_handle.close()
    except Exception:
        pass


def parse_log(path):
    """Totals from an autotrader log: volume and friction."""
    contracts = shares = opt_sends = rtm_sends = 0
    pattern = re.compile(r'SENT\s+(BUY|SELL)\s+([\d,]+)\s+(\S+)')
    try:
        with open(path, encoding='utf-8', errors='ignore') as handle:
            for line in handle:
                match = pattern.search(line)
                if not match:
                    continue
                quantity = int(match.group(2).replace(',', ''))
                if match.group(3).upper() == 'RTM':
                    shares += quantity
                    rtm_sends += 1
                else:
                    contracts += quantity
                    opt_sends += 1
    except OSError:
        pass
    # $1.00/contract commission + $1.00/contract of spread; $0.01/share each.
    friction = contracts * 2.00 + shares * 0.02
    return {
        'contracts': contracts, 'shares': shares,
        'opt_sends': opt_sends, 'rtm_sends': rtm_sends,
        'friction': round(friction),
    }


def append_result(row):
    exists = os.path.exists(RESULTS)
    with open(RESULTS, 'a', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def summary():
    if not os.path.exists(RESULTS):
        print('no results yet')
        return 0
    with open(RESULTS, encoding='utf-8') as handle:
        rows = [r for r in csv.DictReader(handle)]
    if not rows:
        print('no results yet')
        return 0
    values = [float(r['nlv']) for r in rows if r['nlv'] not in ('', None)]
    print(f'\n  {len(values)} heats recorded\n')
    print(f'  {"finished":<20}{"NLV":>12}{"friction":>10}{"contracts":>11}')
    for row in rows:
        print(f'  {row["finished"]:<20}{float(row["nlv"]):>12,.0f}'
              f'{float(row["friction"] or 0):>10,.0f}'
              f'{int(row["contracts"] or 0):>11,}')
    if values:
        values_sorted = sorted(values)
        median = values_sorted[len(values_sorted) // 2]
        print(f'\n  mean   ${sum(values) / len(values):>12,.0f}')
        print(f'  median ${median:>12,.0f}')
        print(f'  best   ${max(values):>12,.0f}')
        print(f'  worst  ${min(values):>12,.0f}')
        print(f'  total  ${sum(values):>12,.0f}\n')
    return 0


def build_id():
    """The commit being traded, and whether the tree has been edited since."""
    def git(*args):
        try:
            return subprocess.check_output(('git',) + args, cwd=HERE,
                                           text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            return ''
    return git('rev-parse', '--short', 'HEAD'), git('status', '--porcelain')


def check_build(allow_dirty=False):
    """
    Refuse to trade a working tree that is being edited.

    This is not hygiene, it is a loss that already happened. Every heat gets a
    FRESH autotrader process, so the loop picks up whatever is on disk at the
    moment the heat starts - including a half-finished edit. On 18 Sep three
    heats ran mid-edit builds and one of them ran a version whose bug was found
    minutes later and measured at -$19,941 a heat in backtest. It returned
    -$35,777 live.

    Trade a commit, not a working directory.
    """
    commit, dirty = build_id()
    if dirty and not allow_dirty:
        log('REFUSING TO TRADE: the working tree has uncommitted changes.')
        log('Every heat starts a fresh trader, so an edit mid-run goes live')
        log('on the next heat. Commit or stash first, then restart:')
        for line in dirty.splitlines()[:10]:
            log(f'    {line}')
        log('(pass --allow-dirty to override, deliberately)')
        return None
    return commit or 'unknown'


def run(dry_run=False, allow_dirty=False):
    commit = check_build(allow_dirty)
    if commit is None:
        return 1
    log(f'trading build {commit}')
    os.makedirs(LOGS, exist_ok=True)
    os.makedirs(HEATS, exist_ok=True)
    client = RitClient()
    heat_number = 0

    log(f'overnight runner started{"  [DRY RUN]" if dry_run else ""}')
    log(f'results -> {RESULTS}')
    log('ctrl-c to stop')

    trader = recorder = None
    tag = None
    seen_active = False
    last_tick = 0

    try:
        while True:
            # --- children up and waiting, before the heat starts ----------
            if trader is None or trader.poll() is not None:
                stop(trader)
                tag = datetime.now().strftime('%H%M%S')
                args = ['autotrader.py', '--verbose']
                if not dry_run:
                    args.append('--live')
                trader = spawn(args, os.path.join(LOGS, f'auto_{tag}.log'))
                recorder = spawn(['record.py', '--name', f'live_{tag}'],
                                 os.path.join(LOGS, f'rec_{tag}.log'))
                seen_active = False
                log(f'armed and waiting (tag {tag})')

            # --- watch the case ------------------------------------------
            try:
                case = client.case()
            except ApiException:
                time.sleep(2.0)
                continue

            status = case.get('status')
            if status == 'ACTIVE':
                # Remember the last live tick. By the time the case reports
                # STOPPED it has already reset to 0, so reading it then records
                # nothing useful.
                last_tick = case.get('tick', 0) or last_tick
                if not seen_active:
                    seen_active = True
                    heat_number += 1
                    log(f'heat {heat_number} started '
                        f'({case.get("ticks_per_period")} ticks, '
                        f'joined at tick {case.get("tick", 0)})')
            elif seen_active:
                # Settled. Read NLV immediately - it resets when the next heat
                # is loaded, so this is the only reliable moment to capture it.
                try:
                    nlv = float(client.trader().get('nlv') or 0.0)
                except ApiException:
                    nlv = 0.0

                stats = parse_log(os.path.join(LOGS, f'auto_{tag}.log'))
                row = {
                    'finished': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'tag': tag,
                    'nlv': round(nlv),
                    'friction': stats['friction'],
                    'directional': round(nlv + stats['friction']),
                    'contracts': stats['contracts'],
                    'shares': stats['shares'],
                    'opt_sends': stats['opt_sends'],
                    'rtm_sends': stats['rtm_sends'],
                    'ticks': last_tick,
                    'log': f'auto_{tag}.log',
                }
                append_result(row)
                log(f'heat {heat_number} finished: NLV ${nlv:,.0f}  '
                    f'friction ${stats["friction"]:,}  '
                    f'contracts {stats["contracts"]:,}')

                # Fresh processes for the next heat - a carried-over gap EMA,
                # news counter or working strike would poison its first ticks.
                stop(trader)
                stop(recorder)
                trader = recorder = None
                last_tick = 0
                time.sleep(1.0)
                continue

            time.sleep(POLL)

    except KeyboardInterrupt:
        log('stopping')
    finally:
        stop(trader)
        stop(recorder)
        log(f'{heat_number} heats run. summary: python overnight.py --summary')


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true',
                        help='supervise and record but never send orders')
    parser.add_argument('--allow-dirty', action='store_true',
                        help='trade an edited working tree (deliberately)')
    parser.add_argument('--summary', action='store_true',
                        help='print accumulated results and exit')
    args = parser.parse_args()

    if args.summary:
        return summary()
    return run(dry_run=args.dry_run, allow_dirty=args.allow_dirty)


if __name__ == '__main__':
    sys.exit(main())
