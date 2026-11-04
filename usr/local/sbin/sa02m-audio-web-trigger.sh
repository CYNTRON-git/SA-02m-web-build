#!/bin/bash
# Privileged helper for the «Запись звука» card (www/network_config/cgi-bin/
# sa02m_audio_api.cgi): the three verbs the sudoers pin grants —
#   enable    unmask + enable + restart sa02m-audio.service
#   disable   stop + disable (the recordings STAY: Operator decision F10)
#   restart   restart the unit ONLY when the conf has enabled=true (the card's
#             «Перезапустить» after `conf_unreadable`: the unit's ExecStartPre
#             re-asserts the grant); a disabled recorder is left alone
# Contract: docs/contracts/audio-recorder.md §16. The CGI (www-data) has
# already validated and written the conf; this helper never reads caller data
# beyond argv[1], which is matched against a fixed `case` before anything runs.
#
# Every systemctl is bounded: the CGI calls this SYNCHRONOUSLY inside nginx's
# 20 s fastcgi budget (web-code-rigor "timeouts everywhere" floor).
#
# The helper touches no file in the recorder's directories: deletion is the
# daemon's (MQTT `delete/on`), and the trigger lock is the helper's own
# root-owned file, never a lock in a directory another account can write.
#
# Harness: scripts/dev/test-audio-trigger.sh extracts the block between the
# two FUNCTIONS markers below and runs it against a sandbox — keep every piece
# of logic inside it.
set -euo pipefail

UNIT=sa02m-audio.service
UNIT_FILE=/etc/systemd/system/sa02m-audio.service
CONF=/etc/sa02m-audio/sa02m-audio.conf
LOCK_FILE=/usr/local/sbin/sa02m-audio-web-trigger.sh
LOCK_WAIT_S=5
# The daemon's package (root:root, go-w — scripts/15-audio.sh): root imports
# its stdlib-only config module and nothing a non-root user can write.
PKG_DIR=/opt/sa02m-audio

# >>> FUNCTIONS (extracted by scripts/dev/test-audio-trigger.sh)

au_log() {
    logger -t sa02m-audio-web-trigger -- "$*" 2>/dev/null || true
}

# enabled=true in the conf? Decided by the daemon's OWN reader,
# sa02m_audio.config.load(), so the helper and the daemon cannot disagree on
# any input. Root safety: `env -i` + `python3 -I -B` (no PYTHON* env, no user
# site, no bytecode written); the conf path is fixed and passed explicitly; a
# non-regular conf (a FIFO would block the open) is refused first, and the
# read is bounded by `timeout`. Anything but a clean "1" is "not enabled";
# anything but a clean 0/1 is also logged once with its cause.
au_conf_enabled() {
    local out rc=0
    [ -f "$CONF" ] && [ -r "$CONF" ] || return 1
    out=$(timeout 5 env -i PATH=/usr/bin:/bin python3 -I -B -c '
import sys
try:
    sys.path.insert(0, sys.argv[1])
    from sa02m_audio import config
    print("1" if config.load(sys.argv[2]).enabled else "0")
except BaseException as exc:
    print("error: %s: %s" % (type(exc).__name__, exc))
' "$PKG_DIR" "$CONF" 2>/dev/null) || rc=$?
    case "$out" in
        1) return 0 ;;
        0) return 1 ;;
    esac
    out=${out//[^[:print:]]/ }
    au_log "conf read of $CONF gave no 0/1 answer (rc=$rc): ${out:0:200} — treated as not enabled"
    return 1
}

au_enable() {
    timeout 10 systemctl unmask "$UNIT" >/dev/null 2>&1 || true
    timeout 10 systemctl enable "$UNIT" >/dev/null 2>&1 || true
    timeout 10 systemctl restart "$UNIT" >/dev/null 2>&1 || true
    au_log "recorder enabled from the web card"
    echo '{"ok":true,"action":"enable"}'
}

au_disable() {
    timeout 10 systemctl stop "$UNIT" >/dev/null 2>&1 || true
    timeout 10 systemctl disable "$UNIT" >/dev/null 2>&1 || true
    au_log "recorder disabled from the web card (recordings kept)"
    echo '{"ok":true,"action":"disable"}'
}

au_restart() {
    if au_conf_enabled; then
        timeout 10 systemctl restart "$UNIT" >/dev/null 2>&1 || true
        echo '{"ok":true,"action":"restart","applied":"restart"}'
    else
        echo '{"ok":true,"action":"restart","applied":"skipped"}'
    fi
}

au_main() {
    local action="${1:-}"
    case "$action" in
        enable|disable|restart) ;;
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
    # One verb at a time: a disable racing an enable would leave the unit in
    # whichever state the slower one wrote last.
    exec 9<"$LOCK_FILE"
    if ! flock -w "$LOCK_WAIT_S" 9; then
        echo '{"ok":false,"error":"busy"}'
        return 1
    fi
    case "$action" in
        enable)  au_enable ;;
        disable) au_disable ;;
        restart) au_restart ;;
    esac
}

# <<< FUNCTIONS

au_main "$@"
