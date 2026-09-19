#!/usr/bin/env bash
# Emit one line per finished heat, plus an alarm if the runner stalls or dies.
#
# One event per heat is the useful cadence: results.csv gains a row when
# overnight.py finishes one. Silence has to be distinguishable from health, so
# a stalled runner and a dead runner both emit too - a heat is ~6m20s, and
# nothing arriving for 15 minutes means something is wrong, not that the night
# is quiet.
cd "$(dirname "$0")" || exit 1

STALL=900          # seconds without a heat before shouting
last=$(wc -l < results.csv)
pinned_log=""
was_running=1
seen=$(date +%s)

while true; do
    if ! n=$(wc -l < results.csv 2>/dev/null); then n=$last; fi
    if [ "$n" -gt "$last" ]; then
        tail -n "$((n - last))" results.csv | while IFS=, read -r fin tag nlv fric dir con sh os rs ticks log; do
            printf 'HEAT %s  NLV $%s  friction $%s  contracts %s  shares %s  ticks %s\n' \
                   "$tag" "$nlv" "$fric" "$con" "$sh" "$ticks"
        done
        last=$n
        seen=$(date +%s)
    fi

    # The share-limit pin. It does NOT print "CANNOT HEDGE" - the trader's own
    # clamp lets the order through and the SERVER refuses it, so the only
    # evidence is a rejection in the Limit-ETF risk category. Watching the
    # wrong string meant nine heats went by with the tripwire looking at
    # nothing. Report once per heat log.
    newest=$(ls -t logs/auto_*.log 2>/dev/null | head -1)
    if [ -n "$newest" ] && [ "$newest" != "$pinned_log" ]; then
        # grep -c exits NON-ZERO when the count is zero, so `|| echo 0`
        # appends a second zero and the comparison below dies with
        # "integer expression expected". Assign, then correct on failure.
        n=$(grep -c "Limit-ETF" "$newest" 2>/dev/null) || n=0
        if [ "$n" -gt 0 ]; then
            echo "ALARM RTM share limit hit $n times in $(basename "$newest") - the hedge was refused"
            pinned_log="$newest"
        fi
    fi

    # A stalled runner is only news if a runner is supposed to be running.
    # Between the overnight run and the competition the machine is
    # DELIBERATELY idle, and a watcher that shouts every fifteen minutes
    # through that is noise at exactly the moment attention is scarce.
    if tasklist //FI "IMAGENAME eq python.exe" //FO CSV 2>/dev/null | grep -q python.exe; then
        if [ "$was_running" = "0" ]; then
            echo "NOTE trading processes are up again"
            seen=$(date +%s)
        fi
        was_running=1
        if [ $(( $(date +%s) - seen )) -gt "$STALL" ]; then
            echo "ALARM no heat finished in $(( ($(date +%s) - seen) / 60 )) minutes - runner stalled"
            seen=$(date +%s)
        fi
    else
        if [ "$was_running" = "1" ]; then
            echo "ALARM all trading processes have STOPPED"
        fi
        was_running=0
    fi
    sleep 20
done
