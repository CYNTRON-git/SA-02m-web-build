#!/bin/bash
# sa02m-user-unit.sh — install|start|stop|remove|status a sa02m-user@ instance.
# The name is one label: a letter or digit, then up to 30 more of [a-z0-9-].
set -euo pipefail

verb_ok() {
    case "$1" in
        install|start|stop|remove|status) return 0 ;;
        *) return 1 ;;
    esac
}

name_ok() {
    [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || return 1
    return 0
}

if ! verb_ok "${1:-}"; then
    echo "usage: sa02m-user-unit verb name" >&2
    exit 2
fi
if ! name_ok "${2:-}"; then
    echo "refusing unit name" >&2
    exit 2
fi

UNIT="sa02m-user@${2}.service"
case "$1" in
    install)
        if [ ! -f "/opt/sa02m-user/$2/start.sh" ] || [ -L "/opt/sa02m-user/$2/start.sh" ]; then
            echo "start.sh is missing" >&2
            exit 2
        fi
        if ! getent passwd sa02m-user >/dev/null 2>&1; then
            echo "sa02m-user is missing: enable the API first" >&2
            exit 2
        fi
        # The daemon writes as www-data with umask 0027; the unit runs as
        # sa02m-user, which must read and execute the entry point.
        chgrp -R sa02m-user "/opt/sa02m-user/$2" 2>/dev/null || true
        chmod -R g+rX "/opt/sa02m-user/$2" 2>/dev/null || true
        chmod ug+x "/opt/sa02m-user/$2/start.sh"
        systemctl daemon-reload
        systemctl enable "$UNIT"
        ;;
    start) systemctl start "$UNIT" ;;
    stop) systemctl stop "$UNIT" ;;
    remove)
        systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
        ;;
    status)
        systemctl is-active "$UNIT" || true
        ;;
esac
