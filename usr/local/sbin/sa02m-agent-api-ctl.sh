#!/bin/bash
# sa02m-agent-api-ctl.sh — enable, disable, restart or status the agent API unit.
# www-data reaches it through sudo. The verb is checked here, not trusted from sudoers alone.
set -euo pipefail

UNIT=sa02m-agent-api.service

verb_ok() {
    [[ "$1" =~ ^[A-Za-z]+$ ]] || return 1
    case "$1" in
        enable|disable|restart|status) return 0 ;;
        *) return 1 ;;
    esac
}

if ! verb_ok "${1:-}"; then
    echo "usage: sa02m-agent-api-ctl enable|disable|restart|status" >&2
    exit 2
fi

# Boards that only ever updated over the web never ran scripts/13-agent-api.sh:
# the sandbox user, the state dirs and the token seed come from here instead.
# Idempotent; the installer does the same (one definition of the layout is
# etc/tmpfiles.d/sa02m-agent-api.conf, applied by both).
bootstrap_layout() {
    if ! getent passwd sa02m-user >/dev/null 2>&1; then
        useradd --system --shell /usr/sbin/nologin --home-dir /opt/sa02m-user \
            --no-create-home sa02m-user >/dev/null 2>&1 || true
    fi
    if id -nG www-data 2>/dev/null | tr ' ' '\n' | grep -qx sa02m-user; then :; else
        usermod -aG sa02m-user www-data >/dev/null 2>&1 || true
    fi
    if [ -f /etc/tmpfiles.d/sa02m-agent-api.conf ] && command -v systemd-tmpfiles >/dev/null 2>&1; then
        timeout 30 systemd-tmpfiles --create /etc/tmpfiles.d/sa02m-agent-api.conf >/dev/null 2>&1 || true
    fi
    if [ -f /etc/sa02m-agent-api/tokens.json ] && [ ! -s /etc/sa02m-agent-api/tokens.json ]; then
        printf '{"tokens":[]}\n' >/etc/sa02m-agent-api/tokens.json
        chown root:www-data /etc/sa02m-agent-api/tokens.json
        chmod 0640 /etc/sa02m-agent-api/tokens.json
    fi
    return 0
}

case "$1" in
    enable)
        bootstrap_layout
        systemctl unmask "$UNIT" >/dev/null 2>&1 || true
        systemctl enable "$UNIT"
        systemctl restart "$UNIT"
        ;;
    disable)
        systemctl disable --now "$UNIT" >/dev/null 2>&1 || systemctl stop "$UNIT" >/dev/null 2>&1 || true
        ;;
    restart)
        systemctl restart "$UNIT"
        ;;
    status)
        systemctl is-active "$UNIT" || true
        ;;
esac
