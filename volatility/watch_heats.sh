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
        n=$(grep -c "Limit-ETF" "$newest" 2>/dev/null || echo 0)
        if [ "$n" -gt 0 ]; then
            echo "ALARM RTM share limit hit $n times in $(basename "$newest") - the hedge was refused"
            pinned_log="$newest"
        fi
    fi

    if ! tasklist //FI "IMAGENAME eq python.exe" //FO CSV 2>/dev/null | grep -q python.exe; then
        echo "ALARM no python processes - the overnight runner is DEAD"
        sleep 120
    elif [ $(( $(date +%s) - seen )) -gt "$STALL" ]; then
        echo "ALARM no heat finished in $(( ($(date +%s) - seen) / 60 )) minutes - runner stalled"
        seen=$(date +%s)
    fi
    sleep 20
done
