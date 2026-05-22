#!/bin/bash
cd /mnt/c/Users/admin/Downloads/SA-02m-web-build
for s in scripts/01-system.sh scripts/02-network.sh scripts/03-webserver.sh scripts/04-flasher.sh scripts/lib.sh scripts/update-www-only.sh etc/fix-eth.sh etc/storage-mount.sh etc/sa02m-pre-start.sh etc/sa02m-watchdog-feed.sh etc/net-watchdog.sh; do
    printf '%-40s ' "$s"
    if bash -n "$s" 2>/tmp/syn.err; then
        echo OK
    else
        echo FAIL
        cat /tmp/syn.err | sed 's/^/    /'
    fi
done
