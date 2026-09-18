"""
Record a live heat from the RIT practice server to a file for later analysis.

    python record.py                       # writes heats/heat_<timestamp>.jsonl
    python record.py --name mode_test_1

Run this alongside a practice heat. It only reads - it never sends an order, so
it is safe to leave running while you trade manually or while the dashboard is
open in another window.

Each line is one tick snapshot: the case state, every security's quote and your
position, and the full news feed. That is everything replay.py needs to re-run
any volatility model over the heat afterwards, as many times as you like.
"""

import argparse
import json
import os
import time

from rit_api import ApiException, RitClient

HEAT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'heats')


def record(client, path, poll=0.2):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    seen_ticks = set()
    written = 0
    started = False

    print(f'recording to {path}')
    print('waiting for an active heat... (ctrl-c to stop)')

    with open(path, 'w', encoding='utf-8') as handle:
        while True:
            try:
                case = client.case()
                status = case.get('status', '')
                tick = case.get('tick', 0)

                if status == 'ACTIVE':
                    if not started:
                        print(f'heat started: {case.get("ticks_per_period")} ticks')
                        started = True
                    if tick not in seen_ticks:
                        seen_ticks.add(tick)
                        snapshot = {
                            'case': case,
                            'securities': client.securities(),
                            'news': client.news(),
                            # Mark-to-market NLV at every tick. Without this a
                            # recording can only give a single number at
                            # settlement, so questions like "where inside a heat
                            # is the money made or lost - at the news releases,
                            # or between them?" can only be inferred. With it
                            # they are answerable directly from the tape.
                            'trader': client.trader(),
                            'wall_time': time.time(),
                        }
                        handle.write(json.dumps(snapshot) + '\n')
                        handle.flush()
                        written += 1
                        if written % 25 == 0:
                            print(f'  tick {tick}  ({written} snapshots)')
                elif started and status in ('STOPPED', 'INACTIVE'):
                    print(f'heat finished at tick {tick}: {written} snapshots')
                    break

            except ApiException as exc:
                print(f'  api error (retrying): {exc}')
                time.sleep(1.0)
                continue

            time.sleep(poll)

    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--name', help='base name for the output file')
    parser.add_argument('--host', default=None)
    parser.add_argument('--poll', type=float, default=0.2)
    args = parser.parse_args()

    name = args.name or f'heat_{time.strftime("%Y%m%d_%H%M%S")}'
    path = os.path.join(HEAT_DIR, f'{name}.jsonl')

    client = RitClient(host=args.host) if args.host else RitClient()
    try:
        count = record(client, path, poll=args.poll)
    except KeyboardInterrupt:
        print('\nstopped by user')
        return
    if count:
        print(f'\ndone. analyse with:\n  python replay.py {path}')


if __name__ == '__main__':
    main()
