"""
Keep exactly one live trader and one recorder running, across heats.

    python supervise.py

Both `autotrader.py` and `record.py` finish with the heat, so an unattended run
needs something to restart them. Doing that with shell loops went wrong twice:
`record.py --name live` overwrites its file every heat, and two separate restart
scripts can each start a trader, which puts TWO actors on one book. Heat 1
showed what that costs - an unhedgeable position bills $2,002 a second.

So this owns both processes. It never starts a second trader while one is alive,
it waits for the heat to end before swapping anything, and Ctrl-C stops both.
"""

import os
import signal
import subprocess
import sys
import time

import config
from rit_api import ApiException, RitClient

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, 'logs')
stop = False


def _stop(_signum, _frame):
    global stop
    stop = True


def active(client):
    try:
        return client.case().get('status') == 'ACTIVE'
    except ApiException:
        return False


def spawn(args, log_name):
    handle = open(os.path.join(LOGS, log_name), 'a', encoding='utf-8')
    return subprocess.Popen([sys.executable] + args, cwd=HERE,
                            stdout=handle, stderr=subprocess.STDOUT)


def main():
    signal.signal(signal.SIGINT, _stop)
    os.makedirs(LOGS, exist_ok=True)
    client = RitClient()
    stamp = time.strftime('%H%M%S')
    trader = spawn(['autotrader.py', '--live', '--verbose'],
                   f'auto_{stamp}.log')
    print(f'trader pid {trader.pid} -> logs/auto_{stamp}.log', flush=True)
    recorder = None
    heats = 0

    while not stop:
        # One recorder per heat, each writing its own timestamped file. Handing
        # record.py a fixed --name makes every heat overwrite the last.
        if active(client) and (recorder is None or recorder.poll() is not None):
            recorder = spawn(['record.py'], 'record.log')
            heats += 1
            print(f'heat {heats}: recorder pid {recorder.pid}', flush=True)
        if trader.poll() is not None:
            # Only ever replace a dead trader, and only between heats, so there
            # is never a moment with two of them on the same account.
            while active(client) and not stop:
                time.sleep(1.0)
            if stop:
                break
            stamp = time.strftime('%H%M%S')
            trader = spawn(['autotrader.py', '--live', '--verbose'],
                           f'auto_{stamp}.log')
            print(f'trader restarted pid {trader.pid}', flush=True)
        time.sleep(1.0)

    for process in (trader, recorder):
        if process and process.poll() is None:
            process.terminate()
    print(f'stopped after {heats} heats', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
