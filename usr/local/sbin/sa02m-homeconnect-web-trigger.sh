#!/bin/bash
# Privileged helper for the Home Connect card (www/network_config/cgi-bin/
# sa02m_homeconnect_api.cgi): the four verbs the sudoers pin grants —
#   enable    unmask + enable + restart sa02m-homeconnect.service
#   disable   stop + disable (the daemon, stopped with the conf disabled,
#             removes its own retained topics and inventory.json itself)
#   restart   restart the unit ONLY when the conf has enabled=true (a new
#             Client ID, a «Подключить»); a disabled client is left alone
#   unlink    stop -> remove the sign-in (tokens.json + the .hc-*.tmp sidecars
#             of a torn write) -> start again when enabled. budget.json and
#             appliances.json STAY: BSH counts calls per client+user, and the
#             restarted daemon needs the appliance list to remove the unlinked
#             account's retained topics (docs/contracts/home-connect.md §7, §10)
# Contract: docs/contracts/home-connect.md §10. The CGI (www-data) has already
# validated and written the conf; this helper never reads caller data beyond
# argv[1], which is matched against a fixed `case` before anything runs.
#
# Every systemctl is bounded: the CGI calls this SYNCHRONOUSLY inside nginx's
# 20 s fastcgi budget (web-code-rigor "timeouts everywhere" floor).
#
# Root in daemon-owned directories: /run/sa02m-homeconnect and
# /var/lib/sa02m-homeconnect belong to the unprivileged sa02m-homeconnect user,
# which can plant a symlink under any name there. So root only ever UNLINKS
# names there (rm -f removes the link itself, never its target), never writes
# or chmods one; and the trigger lock is the helper's own root-owned file,
# never a lock file in the daemon's directory.
#
# Harness: scripts/dev/test-homeconnect-trigger.sh extracts the block between
# the two FUNCTIONS markers below and runs it against a sandbox — keep every
# piece of logic inside it.
set -euo pipefail

UNIT=sa02m-homeconnect.service
UNIT_FILE=/etc/systemd/system/sa02m-homeconnect.service
CONF=/etc/sa02m-homeconnect/sa02m-homeconnect.conf
VAR_DIR=/var/lib/sa02m-homeconnect
RUN_DIR=/run/sa02m-homeconnect
LINK_FILE=$RUN_DIR/link.json
LOCK_FILE=/usr/local/sbin/sa02m-homeconnect-web-trigger.sh
LOCK_WAIT_S=5
# The daemon's package (root:root, go-w — scripts/06d-homeconnect.sh): root
# imports its stdlib-only config module and nothing a non-root user can write.
PKG_DIR=/opt/sa02m-homeconnect

# >>> FUNCTIONS (extracted by scripts/dev/test-homeconnect-trigger.sh)

# enabled=true in the conf? Decided by the daemon's OWN reader,
# sa02m_homeconnect.config.load(), so the helper and the daemon cannot
# disagree on any input (section, key case, CRLF, duplicates, [DEFAULT], parse
# errors). Root safety: `env -i` + `python3 -I -B` (no PYTHON* env, no user
# site, no bytecode written); the conf path is fixed and passed explicitly; a
# non-regular conf (a FIFO would block the open) is refused first, and the
# read is bounded by `timeout` against a swap between that check and the open.
# Anything but a clean "1" is "not enabled"; anything but a clean 0/1 (package
# absent, load() raising, the timeout) is also logged once with its cause, so
# a `skipped` is diagnosable. The cause travels on stdout as one `error:` line
# (stderr stays discarded, so a stray interpreter warning cannot turn a real
# "1" into "not enabled").
hc_conf_enabled() {
    local out rc=0
    [ -f "$CONF" ] && [ -r "$CONF" ] || return 1
    out=$(timeout 5 env -i PATH=/usr/bin:/bin python3 -I -B -c '
import sys
try:
    sys.path.insert(0, sys.argv[1])
    from sa02m_homeconnect import config
    print("1" if config.load(sys.argv[2]).enabled else "0")
except BaseException as exc:
    print("error: %s: %s" % (type(exc).__name__, exc))
' "$PKG_DIR" "$CONF" 2>/dev/null) || rc=$?
    case "$out" in
        1) return 0 ;;
        0) return 1 ;;
    esac
    out=${out//[^[:print:]]/ }
    hc_log "conf read of $CONF gave no 0/1 answer (rc=$rc): ${out:0:200} — treated as not enabled"
    return 1
}

hc_log() {
    logger -t sa02m-homeconnect-web-trigger -- "$*" 2>/dev/null || true
}

# True only when systemd reports the unit fully down. `is-active` exits
# non-zero for `deactivating` too, so its exit code alone would let the delete
# run under a daemon still shutting down (it could write a refreshed token
# once more); a timed-out or unreadable answer is "not known stopped" as well.
hc_unit_stopped() {
    local st
    st=$(timeout 10 systemctl is-active "$UNIT" 2>/dev/null) || true
    case "$st" in
        inactive|failed) return 0 ;;
        *) return 1 ;;
    esac
}

hc_enable() {
    timeout 10 systemctl unmask "$UNIT" >/dev/null 2>&1 || true
    timeout 10 systemctl enable "$UNIT" >/dev/null 2>&1 || true
    timeout 10 systemctl restart "$UNIT" >/dev/null 2>&1 || true
    echo '{"ok":true,"action":"enable"}'
}

hc_disable() {
    timeout 10 systemctl stop "$UNIT" >/dev/null 2>&1 || true
    timeout 10 systemctl disable "$UNIT" >/dev/null 2>&1 || true
    # The daemon clears it on a clean stop; a killed one cannot.
    rm -f -- "$LINK_FILE" 2>/dev/null || true
    echo '{"ok":true,"action":"disable"}'
}

hc_restart() {
    if hc_conf_enabled; then
        timeout 10 systemctl restart "$UNIT" >/dev/null 2>&1 || true
        echo '{"ok":true,"action":"restart","applied":"restart"}'
    else
        echo '{"ok":true,"action":"restart","applied":"skipped"}'
    fi
}

# Remove the sign-in: exactly tokens.json and the `.hc-*.tmp` sidecars a torn
# atomic write can leave (a torn token write is the same secret under another
# name). Never budget.json, never appliances.json, never the conf; no
# recursive rm.
hc_remove_tokens() {
    local f
    rm -f -- "$VAR_DIR/tokens.json" 2>/dev/null || true
    for f in "$VAR_DIR"/.hc-*.tmp; do
        # an unmatched glob stays literal; a directory of that name is not ours
        if [ -L "$f" ] || [ -f "$f" ]; then
            rm -f -- "$f" 2>/dev/null || true
        fi
    done
    # Report what is TRUE afterwards, not what was attempted.
    if [ -e "$VAR_DIR/tokens.json" ] || [ -L "$VAR_DIR/tokens.json" ]; then
        return 1
    fi
    return 0
}

hc_unlink() {
    local restarted=false
    if [ -L "$VAR_DIR" ] || [ ! -d "$VAR_DIR" ]; then
        echo '{"ok":false,"action":"unlink","error":"state_dir_invalid"}'
        return 1
    fi
    timeout 10 systemctl stop "$UNIT" >/dev/null 2>&1 || true
    # Never delete under a live daemon: a refresh in flight would write the
    # token straight back.
    if ! hc_unit_stopped; then
        echo '{"ok":false,"action":"unlink","error":"still_running"}'
        return 1
    fi
    rm -f -- "$LINK_FILE" 2>/dev/null || true
    if ! hc_remove_tokens; then
        hc_log "unlink: $VAR_DIR/tokens.json could not be removed"
        echo '{"ok":false,"action":"unlink","error":"remove_failed"}'
        return 1
    fi
    hc_log "Home Connect sign-in removed on request from the web card"
    if hc_conf_enabled; then
        timeout 10 systemctl start "$UNIT" >/dev/null 2>&1 || true
        restarted=true
    fi
    echo "{\"ok\":true,\"action\":\"unlink\",\"restarted\":$restarted}"
}

hc_main() {
    local action="${1:-}"
    case "$action" in
        enable|disable|restart|unlink) ;;
        *)
            echo '{"ok":false,"error":"unknown_action"}'
            return 1
            ;;
    esac
    if [ "$#" -ne 1 ]; then
        echo '{"ok":false,"error":"unexpected_arguments"}'
        return 1
    fi
    if [ ! -f "$UNIT_FILE" ]; then
        echo '{"ok":false,"error":"not_installed"}'
        return 1
    fi
    # One verb at a time: an unlink racing an enable would start the daemon
    # between the stop and the delete.
    exec 9<"$LOCK_FILE"
    if ! flock -w "$LOCK_WAIT_S" 9; then
        echo '{"ok":false,"error":"busy"}'
        return 1
    fi
    case "$action" in
        enable)  hc_enable ;;
        disable) hc_disable ;;
        restart) hc_restart ;;
        unlink)  hc_unlink ;;
    esac
}

# <<< FUNCTIONS

hc_main "$@"
