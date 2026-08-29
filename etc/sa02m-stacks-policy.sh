#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-stacks-policy.sh — third-party stack policy: the ONE home for the stack
# ID set, the /etc/sa02m_stacks.conf format, its reader/writer, live detection
# and the installer verdict table. Contract: docs/contracts/installer-refresh-policy.md
#
# POSIX sh, SOURCED (never executed): by scripts/lib.sh (installer, bash) from
# the repo tree, and softly by /usr/local/sbin/sa02m-web-service-ctl.sh from
# /usr/local/lib (installed there by scripts/03-webserver.sh). Every function
# validates its arguments against the closed ID / value sets BEFORE touching the
# file — a caller string is never interpolated into it. The file is never
# `source`d either: it is awk-parsed and an unknown value reads as `absent`,
# exactly what a missing file yields, so tampering can never widen behaviour
# beyond the no-file case.
#
# Writers: install.sh (migration: create-if-absent only), the install-time
# modules 07/08/09/12 (`present` after a successful install), the ctl's
# install/uninstall (`present` / `disabled`). Nothing writes it under
# SA02M_ROOTFS_BUILD — an image must not bake a policy; the first on-device
# run derives it. Readers: the same modules + the ctl.
# ═══════════════════════════════════════════════════════════════════════════

SA02M_STACKS_CONF="${SA02M_STACKS_CONF:-/etc/sa02m_stacks.conf}"
# Sorted; the file is written in exactly this order. Third-party only (the
# sa02m stacks are the product: never uninstallable from the UI, their run-state
# is covered by the unit-state capture in scripts/lib.sh, so no key for them).
SA02M_STACK_IDS="CODESYS DOCKER KLOGIC MPLC NODERED"
# Root prefix for the live-detection file probes (test seam; empty on a board).
SA02M_STACK_PROBE_ROOT="${SA02M_STACK_PROBE_ROOT:-}"

# sa02m_stack_id_valid <ID> → 0 iff <ID> is one of the policy IDs.
sa02m_stack_id_valid() {
    case "${1:-}" in
        CODESYS|DOCKER|KLOGIC|MPLC|NODERED) return 0 ;;
    esac
    return 1
}

# sa02m_stack_is_thirdparty <ID> → 0 for every policy ID (exists so the
# service-state helper never hard-codes the list).
sa02m_stack_is_thirdparty() {
    sa02m_stack_id_valid "${1:-}"
}

# sa02m_stack_policy_get <ID> → prints present|absent|disabled. A missing
# file, a missing key or an unknown value all read as `absent` (the no-file
# behaviour); rc 2 on an invalid ID (nothing printed).
sa02m_stack_policy_get() {
    sa02m_stack_id_valid "${1:-}" || return 2
    _sp_v=""
    if [ -f "$SA02M_STACKS_CONF" ]; then
        _sp_v=$(awk -F= -v k="STACK_$1" '
            { gsub(/^[ \t]+|[ \t]+$/, "", $1) }
            $1 == k { gsub(/^[ \t"]+|[ \t"\r]+$/, "", $2); print $2; exit }' \
            "$SA02M_STACKS_CONF" 2>/dev/null)
    fi
    case "$_sp_v" in
        present|absent|disabled) printf '%s\n' "$_sp_v" ;;
        *) printf 'absent\n' ;;
    esac
    return 0
}

# Internal: rewrite the whole file atomically. Args: ID=VALUE overrides (already
# validated by the caller); every other key keeps its current (normalised)
# value; unknown lines are dropped. tmp lives next to the target (same fs ⇒
# atomic mv). rc 1 on a write failure (stderr; caller decides).
_sa02m_stacks_write() {
    _sw_tmp="${SA02M_STACKS_CONF}.tmp.$$"
    {
        printf '%s\n' '# SA-02m third-party stacks policy. Written by install.sh and sa02m-web-service-ctl.sh; hand-editable.'
        printf '%s\n' '# STACK_<ID>=present|absent|disabled   disabled = removed/refused by the operator: never auto-installed.'
        for _sw_k in $SA02M_STACK_IDS; do
            _sw_val=""
            for _sw_arg in "$@"; do
                case "$_sw_arg" in
                    "$_sw_k"=*) _sw_val=${_sw_arg#*=} ;;
                esac
            done
            [ -n "$_sw_val" ] || _sw_val=$(sa02m_stack_policy_get "$_sw_k")
            printf 'STACK_%s=%s\n' "$_sw_k" "$_sw_val"
        done
    } > "$_sw_tmp" 2>/dev/null || {
        rm -f "$_sw_tmp" 2>/dev/null
        echo "sa02m-stacks-policy: cannot write $_sw_tmp" >&2
        return 1
    }
    if chmod 0644 "$_sw_tmp" 2>/dev/null && mv -f "$_sw_tmp" "$SA02M_STACKS_CONF" 2>/dev/null; then
        return 0
    fi
    rm -f "$_sw_tmp" 2>/dev/null
    echo "sa02m-stacks-policy: cannot replace $SA02M_STACKS_CONF" >&2
    return 1
}

# sa02m_stack_policy_set <ID> <present|absent|disabled> → rewrites the file
# (header + all keys sorted). No-op when the key already carries the value
# (keeps mtime honest). rc 0; 2 invalid args; 1 write failure. Never writes
# under SA02M_ROOTFS_BUILD (the image must not bake a policy).
sa02m_stack_policy_set() {
    sa02m_stack_id_valid "${1:-}" || return 2
    case "${2:-}" in
        present|absent|disabled) ;;
        *) return 2 ;;
    esac
    [ -n "${SA02M_ROOTFS_BUILD:-}" ] && return 0
    if [ -f "$SA02M_STACKS_CONF" ] && [ "$(sa02m_stack_policy_get "$1")" = "$2" ]; then
        return 0
    fi
    _sa02m_stacks_write "$1=$2"
}

# Internal: 0 iff a unit file with this name exists in the standard unit dirs
# (no systemctl — the ctl is POSIX sh and may run where the bus is down).
_sa02m_stack_unit_file() {
    for _su_d in /etc/systemd/system /lib/systemd/system /usr/lib/systemd/system /run/systemd/system; do
        [ -f "$SA02M_STACK_PROBE_ROOT$_su_d/$1" ] && return 0
    done
    return 1
}

# sa02m_stack_installed <ID> → 0 iff the stack is live on the box (installer-
# side one home; the ctl's service_present keeps its own UI-oriented resolver —
# an accepted, cross-referenced parallel: delegating it would break `list` on
# boards without this lib). rc 2 on an invalid ID.
sa02m_stack_installed() {
    _si_r=$SA02M_STACK_PROBE_ROOT
    case "${1:-}" in
        NODERED)
            [ -f "$_si_r/usr/lib/node_modules/node-red/package.json" ] && return 0
            [ -f "$_si_r/usr/local/lib/node_modules/node-red/package.json" ] && return 0
            command -v node-red >/dev/null 2>&1
            ;;
        CODESYS)
            dpkg -s codesyscontrol >/dev/null 2>&1 && return 0
            [ -x "$_si_r/etc/init.d/codesyscontrol" ]
            ;;
        MPLC)
            # The runtime payload, not its wrappers: /etc/init.d/mplc4 and
            # mplc4.service only exec start_mplc4.sh, and a leftover wrapper
            # (or bare /opt/mplc4) without it is a half-removed install that
            # must read NOT installed — the refresh verdict was overlaying a
            # corpse and the panel offered a dead «Пуск» (bench 1.135,
            # 2026-08-29).
            [ -x "$_si_r/opt/mplc4/start_mplc4.sh" ]
            ;;
        DOCKER)
            command -v docker >/dev/null 2>&1
            ;;
        KLOGIC)
            _sa02m_stack_unit_file klogicd.service || _sa02m_stack_unit_file klogic.service
            ;;
        *) return 2 ;;
    esac
}

# sa02m_stack_policy_derive [--write]
#   no flag : prints `ID=present|absent` per ID from live detection.
#   --write : creates the policy file from that ONLY if it is absent (never
#             overwrites an operator decision) and prints ONE RU line for the
#             installer log; prints nothing when the file already exists.
#             Silent no-op under SA02M_ROOTFS_BUILD.
sa02m_stack_policy_derive() {
    _sd_write=0
    [ "${1:-}" = --write ] && _sd_write=1
    if [ "$_sd_write" = 1 ]; then
        [ -n "${SA02M_ROOTFS_BUILD:-}" ] && return 0
        [ -f "$SA02M_STACKS_CONF" ] && return 0
    fi
    _sd_set=""
    _sd_line=""
    for _sd_k in $SA02M_STACK_IDS; do
        if sa02m_stack_installed "$_sd_k"; then _sd_s=present; else _sd_s=absent; fi
        if [ "$_sd_write" = 1 ]; then
            _sd_set="$_sd_set $_sd_k=$_sd_s"
            _sd_line="$_sd_line $_sd_k=$_sd_s"
        else
            printf '%s=%s\n' "$_sd_k" "$_sd_s"
        fi
    done
    if [ "$_sd_write" = 1 ]; then
        # shellcheck disable=SC2086  # $_sd_set is a deliberate word list of ID=value
        _sa02m_stacks_write $_sd_set || return 1
        printf '%s создан по текущему состоянию:%s\n' "$SA02M_STACKS_CONF" "$_sd_line"
    fi
    return 0
}

# sa02m_stack_verdict <ID> → prints one of skip-disabled | skip-absent |
# overlay | install, reading SA02M_INSTALL_MODE (empty ⇒ full) and
# SA02M_WITH_OPTIONAL (=1 ⇒ the explicit third-party opt-in, overrides a
# persisted `disabled`). rc 2 on an invalid ID.
#
#   policy          installed   full      refresh        --with-optional
#   disabled        any         skip-disabled skip-disabled install
#   present/absent  yes         install   overlay        install
#   present/absent  no          install   skip-absent    install
sa02m_stack_verdict() {
    sa02m_stack_id_valid "${1:-}" || return 2
    if [ "${SA02M_WITH_OPTIONAL:-0}" = 1 ]; then
        printf 'install\n'
        return 0
    fi
    if [ "$(sa02m_stack_policy_get "$1")" = disabled ]; then
        printf 'skip-disabled\n'
        return 0
    fi
    case "${SA02M_INSTALL_MODE:-full}" in
        refresh)
            if sa02m_stack_installed "$1"; then printf 'overlay\n'; else printf 'skip-absent\n'; fi
            ;;
        *)
            printf 'install\n'
            ;;
    esac
    return 0
}
