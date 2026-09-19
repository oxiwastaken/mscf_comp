"""Windows console and log encoding: a stray character must not kill a heat.

The Windows console and Python's default file encoding are both cp1252 here.
Two values the bot handles are free text from the server and are not under our
control:

  tender `caption`   written to tenders.csv on every sighting, BEFORE the
                     accept is attempted, and deliberately outside any try
  `accept_err`       `str(e)[:200]` of whatever the server returned

Neither is an `ApiException`, so a `UnicodeEncodeError` on either skips the
per-cycle handler entirely and lands in `run_heat`'s `except Exception`, which
abandons the rest of the heat. 958 logged captions are all ASCII today, so this
has never fired - but it costs one heat when it does, and the fix is free.

    python test_encoding.py
"""

import csv
import sys
import tempfile
from pathlib import Path

import etf_arb_bot as bot

failures = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# The kinds of character a server caption or error string could plausibly carry:
# curly quotes from prose, an em dash, a non-breaking space, an accented name.
NASTY = "caption — with “curly” quotes, nbsp , café, ："

check("stdout is not left on a codec that cannot print server text",
      (sys.stdout.encoding or "").lower().replace("-", "") == "utf8",
      f"encoding {sys.stdout.encoding}")

try:
    print(f"        (printing it: {NASTY})")
    check("printing free text from the server does not raise", True)
except UnicodeEncodeError as e:
    check("printing free text from the server does not raise", False, str(e)[:60])

with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "tenders.csv"
    fields = ["tender_id", "caption", "accept_err"]
    try:
        log = bot.CsvLog(path, fields)
        log.write({"tender_id": "1", "caption": NASTY, "accept_err": NASTY})
        log.close()
        check("a tender row with non-ASCII free text is logged, not raised", True)
    except UnicodeEncodeError as e:
        check("a tender row with non-ASCII free text is logged, not raised",
              False, str(e)[:70])

    # And it must read back intact, or the log is written but useless.
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            row = next(csv.DictReader(fh))
        check("...and reads back byte-for-byte", row["caption"] == NASTY,
              repr(row["caption"])[:60])
    except Exception as e:                            # noqa: BLE001
        check("...and reads back byte-for-byte", False, f"{type(e).__name__}: {e}")

    # A second CsvLog on the same path re-reads the header to detect a schema
    # change; that read must not choke on what the first one wrote.
    try:
        bot.CsvLog(path, fields).close()
        check("re-opening an existing log re-reads its header cleanly", True)
    except UnicodeDecodeError as e:
        check("re-opening an existing log re-reads its header cleanly", False,
              str(e)[:70])

# Every log already on disk has to stay readable, or a restart cannot seed its
# bars from heats.csv and calibration cannot load its arbs.
unreadable = []
for f in sorted((Path(__file__).parent / "logs").glob("*.csv")):
    try:
        f.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        unreadable.append(f.name)
check("every existing log still decodes as utf-8", not unreadable,
      ", ".join(unreadable[:3]))

print(f"\n{len(failures)} FAILED" if failures else "\nall passed")
raise SystemExit(1 if failures else 0)
