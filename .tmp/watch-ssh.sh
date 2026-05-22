#!/bin/bash
set -u
HOST=${1:-192.168.1.136}
DUR=${2:-180}
echo "Watching SSH on $HOST for ${DUR}s..."
START=$(date +%s)
LAST=open
END=$((START + DUR))
while [ $(date +%s) -lt $END ]; do
    if nc -w1 -z "$HOST" 22 >/dev/null 2>&1; then
        STATE=open
    else
        STATE=closed
    fi
    if [ "$STATE" != "$LAST" ]; then
        NOW=$(date +%s)
        echo "[+$((NOW - START))s $(date +%H:%M:%S)] $LAST -> $STATE"
        LAST=$STATE
    fi
    sleep 1
done
echo "[+${DUR}s] watch done, final state=$LAST"
