#!/bin/bash
# Privileged helper for the HomeKit card (www/network_config/cgi-bin/
# sa02m_homekit_api.cgi): the four verbs the sudoers pin grants —
#   enable         unmask + enable + restart sa02m-homekit.service
#   disable        stop + disable; writes the `disabled` status fallback
#   restart        restart the unit ONLY when the conf has enabled=true
#                  (interface/port change); a disabled bridge is left alone
#   reset-pairing  stop -> remove the pairing store (state.json + the .hk-*.tmp
#                  sidecars of a torn write) -> start again when enabled
# Contract: docs/contracts/homekit-bridge.md. The CGI (www-data) has already
# validated and written the conf; this helper never reads caller data beyond
# argv[1], which is matched against a fixed `case` before anything runs.
#
# Every systemctl is bounded: the CGI calls this SYNCHRONOUSLY inside nginx's
# 20 s fastcgi budget (web-code-rigor "timeouts everywhere" floor).
#
# Root writes into daemon-owned directories: /run/sa02m-homekit and
# /var/lib/sa02m-homekit belong to the unprivileged sa02m-homekit user, so a
# compromised daemon can plant a symlink under any name there, and swap a
# name between two root steps. Hence: the status fallback is created
# O_EXCL|O_NOFOLLOW relative to a directory fd, written and chmod'ed through
# its own fd, and renamed over (rename replaces a symlink, never its target) —
# root never re-opens a name there; other files are only ever unlinked (rm -f
# removes the link itself); and the trigger lock is the helper's own
# root-owned file, never a lock file in the daemon's directory.
#
# Harness: scripts/dev/test-homekit-trigger.sh extracts the block between the
# two FUNCTIONS markers below and runs it against a sandbox — keep every piece
# of logic inside it.
set -euo pipefail

UNIT=sa02m-homekit.service
UNIT_FILE=/etc/systemd/system/sa02m-homekit.service
CONF=/etc/sa02m-homekit/sa02m-homekit.conf
VAR_DIR=/var/lib/sa02m-homekit
RUN_DIR=/run/sa02m-homekit
STATUS_FILE=$RUN_DIR/status.json
SETUP_FILE=$RUN_DIR/setup.json
LOCK_FILE=/usr/local/sbin/sa02m-homekit-web-trigger.sh
LOCK_WAIT_S=5

# >>> FUNCTIONS (extracted by scripts/dev/test-homekit-trigger.sh)

# enabled=true in the conf? The same truth set as sa02m_homekit/config.py
# (`1|true|yes|on`, case-insensitive; `=` or `:`; no inline comments — the
# daemon's configparser does not strip them either). Read line by line, no
# pipe (quality-gate-rigor (f)).
hk_conf_enabled() {
    local line val
    [ -f "$CONF" ] && [ -r "$CONF" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        [[ $line =~ ^[[:blank:]]*enabled[[:blank:]]*[=:][[:blank:]]*(.*[^[:blank:]])?[[:blank:]]*$ ]] || continue
        val=${BASH_REMATCH[1]:-}
        case "${val,,}" in 1|true|yes|on) return 0 ;; *) return 1 ;; esac
    done < "$CONF"
    return 1
}

hk_log() {
    logger -t sa02m-homekit-web-trigger -- "$*" 2>/dev/null || true
}

# True only when systemd reports the unit fully down. `is-active` exits
# non-zero for `deactivating` too, so its exit code alone would let the delete
# run under a daemon still shutting down (it can persist the old keys once
# more); a timed-out or unreadable answer is "not known stopped" as well.
hk_unit_stopped() {
    local st
    st=$(timeout 10 systemctl is-active "$UNIT" 2>/dev/null) || true
    case "$st" in
        inactive|failed) return 0 ;;
        *) return 1 ;;
    esac
}

# The `disabled` status for the card when the daemon is stopped (it cannot
# write its own). A FALLBACK: written only when the file is absent or older
# than the call ($1 = the call's start epoch) — a fresher file is the
# daemon's own, richer payload and is never clobbered.
hk_write_disabled_status() {
    local since="${1:-0}" mtime now
    # A symlink is never the daemon's status (it writes regular files by
    # rename): replace it, whatever its age.
    if [ -f "$STATUS_FILE" ] && [ ! -L "$STATUS_FILE" ]; then
        mtime=$(stat -c %Y -- "$STATUS_FILE" 2>/dev/null) || mtime=0
        case "$mtime" in ''|*[!0-9]*) mtime=0 ;; esac
        [ "$mtime" -lt "$since" ] || return 0
    fi
    [ -d "$RUN_DIR" ] && [ ! -L "$RUN_DIR" ] || {
        hk_log "write_disabled_status: $RUN_DIR missing or a symlink — the card keeps its previous state"
        return 0
    }
    now=$(date +%s 2>/dev/null) || now=0
    case "$now" in ''|*[!0-9]*) now=0 ;; esac
    # fd-only: the temp is created O_EXCL|O_NOFOLLOW relative to a dir fd and
    # written + chmod'ed through ITS fd, then renamed within the dir — a name
    # the daemon swaps is never opened by root (a swapped-in symlink is moved,
    # not followed). -I: root never imports from the caller's cwd.
    if ! python3 -I - "$RUN_DIR" "$now" 2>/dev/null <<'PY'
import os, sys
run_dir, now = sys.argv[1], int(sys.argv[2])
body = ('{"state":"disabled","ts":%d,"reason":"","message":"HomeKit bridge disabled","enabled":false}\n' % now).encode()
dfd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
tmp = ".status.%d.%s" % (os.getpid(), os.urandom(6).hex())
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
try:
    try:
        os.write(fd, body)
        os.fchmod(fd, 0o644)
    finally:
        os.close(fd)
    os.rename(tmp, "status.json", src_dir_fd=dfd, dst_dir_fd=dfd)
except OSError:
    try:
        os.unlink(tmp, dir_fd=dfd)
    except OSError:
        pass
    raise
PY
    then
        hk_log "write_disabled_status: cannot write the fallback status in $RUN_DIR — the card keeps its previous state"
    fi
    return 0
}

hk_enable() {
    timeout 10 systemctl unmask "$UNIT" >/dev/null 2>&1 || true
    timeout 10 systemctl enable "$UNIT" >/dev/null 2>&1 || true
    timeout 10 systemctl restart "$UNIT" >/dev/null 2>&1 || true
    echo '{"ok":true,"action":"enable"}'
}

hk_disable() {
    local since
    since=$(date +%s 2>/dev/null) || since=0
    timeout 10 systemctl stop "$UNIT" >/dev/null 2>&1 || true
    timeout 10 systemctl disable "$UNIT" >/dev/null 2>&1 || true
    # A stopped daemon cannot withdraw its own setup code.
    rm -f -- "$SETUP_FILE" 2>/dev/null || true
    hk_write_disabled_status "$since"
    echo '{"ok":true,"action":"disable"}'
}

hk_restart() {
    if hk_conf_enabled; then
        timeout 10 systemctl restart "$UNIT" >/dev/null 2>&1 || true
        echo '{"ok":true,"action":"restart","applied":"restart"}'
    else
        echo '{"ok":true,"action":"restart","applied":"skipped"}'
    fi
}

# Remove the pairing store: exactly state.json and the `.hk-*.tmp` sidecars a
# torn atomic write can leave. Never aids.json (iOS automations bind to aids),
# never identity.json, never the conf; no recursive rm.
hk_remove_pairing_store() {
    local f
    rm -f -- "$VAR_DIR/state.json" 2>/dev/null || true
    for f in "$VAR_DIR"/.hk-*.tmp; do
        # an unmatched glob stays literal; a directory of that name is not ours
        if [ -L "$f" ] || [ -f "$f" ]; then
            rm -f -- "$f" 2>/dev/null || true
        fi
    done
    # Report what is TRUE afterwards, not what was attempted.
    if [ -e "$VAR_DIR/state.json" ] || [ -L "$VAR_DIR/state.json" ]; then
        return 1
    fi
    return 0
}

hk_reset_pairing() {
    local restarted=false
    if [ -L "$VAR_DIR" ] || [ ! -d "$VAR_DIR" ]; then
        echo '{"ok":false,"action":"reset-pairing","error":"state_dir_invalid"}'
        return 1
    fi
    timeout 10 systemctl stop "$UNIT" >/dev/null 2>&1 || true
    # Never delete under a live daemon: it would persist the old keys again.
    if ! hk_unit_stopped; then
        echo '{"ok":false,"action":"reset-pairing","error":"still_running"}'
        return 1
    fi
    rm -f -- "$SETUP_FILE" 2>/dev/null || true
    if ! hk_remove_pairing_store; then
        hk_log "reset-pairing: $VAR_DIR/state.json could not be removed"
        echo '{"ok":false,"action":"reset-pairing","error":"remove_failed"}'
        return 1
    fi
    hk_log "pairing store removed on request from the web card"
    if hk_conf_enabled; then
        timeout 10 systemctl start "$UNIT" >/dev/null 2>&1 || true
        restarted=true
    fi
    echo "{\"ok\":true,\"action\":\"reset-pairing\",\"restarted\":$restarted}"
}

hk_main() {
    local action="${1:-}"
    case "$action" in
        enable|disable|restart|reset-pairing) ;;
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
    # One verb at a time: a reset-pairing racing an enable would start the
    # daemon between the stop and the delete.
    exec 9<"$LOCK_FILE"
    if ! flock -w "$LOCK_WAIT_S" 9; then
        echo '{"ok":false,"error":"busy"}'
        return 1
    fi
    case "$action" in
        enable)        hk_enable ;;
        disable)       hk_disable ;;
        restart)       hk_restart ;;
        reset-pairing) hk_reset_pairing ;;
    esac
}

# <<< FUNCTIONS

hk_main "$@"
