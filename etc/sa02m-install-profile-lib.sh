#!/bin/sh
# shellcheck shell=sh
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-install-profile-lib.sh — the board's install profile: product modules
# this board does not carry. The ONE home for the module table (which live
# paths a module owns, which of them OTA delivers, which data removal keeps),
# the /etc/sa02m_install_profile.conf format, its reader/writer and the
# removal of a skipped module. Contract: docs/contracts/install-profile.md.
#
# POSIX sh, SOURCED (never executed): by scripts/lib.sh (installer and
# update-www-only.sh, bash) from the repo tree; by the update runner in a
# SUBSHELL from /usr/local/lib (installed there by scripts/03-webserver.sh and
# delivered by both OTA paths); by the legacy sa02m-web-update-apply rsync path
# from its cloned tree. Mirrors etc/sa02m-stacks-policy.sh: every function
# validates its ID against the closed set BEFORE touching anything, and the
# file is awk-parsed, never `source`d — a missing file, a missing key and an
# unknown value all read as `keep`, the no-file behaviour, so tampering can
# never widen behaviour beyond it (and a tamper needs root anyway).
#
# Seams (harness only; empty / default on a board): SA02M_PROFILE_CONF (the
# file), SA02M_PROFILE_ROOT (prefix on every table path), SA02M_UPDATE_STATEDIR
# (where update.lock lives), SA02M_PROFILE_LOCK_WAIT (seconds, default 300).
# ═══════════════════════════════════════════════════════════════════════════

SA02M_PROFILE_CONF="${SA02M_PROFILE_CONF:-/etc/sa02m_install_profile.conf}"
SA02M_PROFILE_ROOT="${SA02M_PROFILE_ROOT:-}"
# Sorted; the file is written in exactly this order. An ID is here only when
# its row below is COMPLETE: every destination its installer module writes is
# an OTA pattern, an install-only path or kept data (gate: install-profile P7).
SA02M_PROFILE_IDS="AGENT_API"

# Internal: log through the caller's `log LEVEL MSG` when it has one (the
# installer), else stderr.
_sa02m_profile_log() {
    if command -v log >/dev/null 2>&1; then
        log "$1" "$2"
    else
        printf '[%s] %s\n' "$1" "$2" >&2
    fi
}

# sa02m_profile_ids → prints the supported IDs, one per line.
sa02m_profile_ids() {
    for _pi_id in $SA02M_PROFILE_IDS; do
        printf '%s\n' "$_pi_id"
    done
}

# sa02m_profile_id_valid <ID> → 0 iff <ID> is a supported module ID.
sa02m_profile_id_valid() {
    case "${1:-}" in
        AGENT_API) return 0 ;;
    esac
    return 1
}

# sa02m_profile_get <ID> → prints skip|keep. Missing file / key / unknown
# value ⇒ keep. rc 2 on an invalid ID (nothing printed).
sa02m_profile_get() {
    sa02m_profile_id_valid "${1:-}" || return 2
    _pg_v=""
    if [ -f "$SA02M_PROFILE_CONF" ]; then
        _pg_v=$(awk -F= -v k="MODULE_$1" '
            { gsub(/^[ \t]+|[ \t]+$/, "", $1) }
            $1 == k { gsub(/^[ \t"]+|[ \t"\r]+$/, "", $2); print $2; exit }' \
            "$SA02M_PROFILE_CONF" 2>/dev/null)
    fi
    case "$_pg_v" in
        skip) printf 'skip\n' ;;
        *) printf 'keep\n' ;;
    esac
    return 0
}

# sa02m_profile_set <ID> <skip|keep> → rewrites the whole file (header + every
# supported ID, sorted) atomically: tmp next to the target, chmod 0644, mv -f.
# Equal value ⇒ no-op (mtime kept). No-op under SA02M_ROOTFS_BUILD (an image
# must not bake a profile). rc 0; 1 write failure; 2 invalid args.
sa02m_profile_set() {
    sa02m_profile_id_valid "${1:-}" || return 2
    case "${2:-}" in
        skip|keep) ;;
        *) return 2 ;;
    esac
    [ -n "${SA02M_ROOTFS_BUILD:-}" ] && return 0
    if [ -f "$SA02M_PROFILE_CONF" ] && [ "$(sa02m_profile_get "$1")" = "$2" ]; then
        return 0
    fi
    _ps_tmp="${SA02M_PROFILE_CONF}.tmp.$$"
    {
        printf '%s\n' '# SA-02m install profile: product modules this board does not carry. Written by install.sh --skip-module/--keep-module; hand-editable.'
        printf '%s\n' '# MODULE_<ID>=skip|keep   skip = never installed, refreshed or delivered by OTA; install.sh removes it (data kept).'
        for _ps_k in $SA02M_PROFILE_IDS; do
            if [ "$_ps_k" = "$1" ]; then
                _ps_val=$2
            else
                _ps_val=$(sa02m_profile_get "$_ps_k")
            fi
            printf 'MODULE_%s=%s\n' "$_ps_k" "$_ps_val"
        done
    } > "$_ps_tmp" 2>/dev/null || {
        rm -f "$_ps_tmp" 2>/dev/null
        echo "sa02m-install-profile: cannot write $_ps_tmp" >&2
        return 1
    }
    if chmod 0644 "$_ps_tmp" 2>/dev/null && mv -f "$_ps_tmp" "$SA02M_PROFILE_CONF" 2>/dev/null; then
        return 0
    fi
    rm -f "$_ps_tmp" 2>/dev/null
    echo "sa02m-install-profile: cannot replace $SA02M_PROFILE_CONF" >&2
    return 1
}

# sa02m_profile_skipped → prints the IDs the FILE marks skip, one per line.
sa02m_profile_skipped() {
    for _pk_id in $SA02M_PROFILE_IDS; do
        [ "$(sa02m_profile_get "$_pk_id")" = skip ] && printf '%s\n' "$_pk_id"
    done
    return 0
}

# sa02m_profile_apply_cli "<skip IDs>" "<keep IDs>" → the installer's one call
# (install.sh --skip-module / --keep-module, space-separated lists). Validates
# every ID first (unknown, or one ID in both lists ⇒ ERR, rc 2, nothing
# written), writes the file (a write failure ⇒ ERR, rc 2; under
# SA02M_ROOTFS_BUILD nothing is written — WARN — but the run still honours this
# run's skips), then exports SA02M_SKIP_<ID>=1 for every module this run must
# not install and sets SA02M_PROFILE_SUMMARY for the banner.
sa02m_profile_apply_cli() {
    _pa_skip=${1:-}
    _pa_keep=${2:-}
    _pa_ids=$(sa02m_profile_ids | tr '\n' ' ')
    # IDs are [A-Z_] words: anything else (a missing ID arrives as `-`, a glob
    # character would expand in the loops below) is refused before any use.
    case "$_pa_skip$_pa_keep" in
        *[!A-Z_\ ]*)
            _sa02m_profile_log ERR "--skip-module/--keep-module: недопустимый ID «$(printf '%s' "$_pa_skip $_pa_keep" | tr -s ' ' | sed 's/^ //')»; поддерживаются: ${_pa_ids% }"
            return 2
            ;;
    esac
    for _pa_id in $_pa_skip; do
        sa02m_profile_id_valid "$_pa_id" || {
            _sa02m_profile_log ERR "--skip-module $_pa_id: неизвестный модуль; поддерживаются: ${_pa_ids% }"
            return 2
        }
        for _pa_k in $_pa_keep; do
            [ "$_pa_k" = "$_pa_id" ] && {
                _sa02m_profile_log ERR "Модуль $_pa_id указан и в --skip-module, и в --keep-module"
                return 2
            }
        done
    done
    for _pa_id in $_pa_keep; do
        sa02m_profile_id_valid "$_pa_id" || {
            _sa02m_profile_log ERR "--keep-module $_pa_id: неизвестный модуль; поддерживаются: ${_pa_ids% }"
            return 2
        }
    done
    if [ -n "${SA02M_ROOTFS_BUILD:-}" ]; then
        [ -n "$_pa_skip$_pa_keep" ] && _sa02m_profile_log WARN "Профиль установки в образ не записывается (SA02M_ROOTFS_BUILD); пропуски действуют только в этом прогоне"
    else
        for _pa_id in $_pa_skip; do
            sa02m_profile_set "$_pa_id" skip || {
                _sa02m_profile_log ERR "Не удалось записать $SA02M_PROFILE_CONF"
                return 2
            }
        done
        for _pa_id in $_pa_keep; do
            sa02m_profile_set "$_pa_id" keep || {
                _sa02m_profile_log ERR "Не удалось записать $SA02M_PROFILE_CONF"
                return 2
            }
        done
    fi
    _pa_off=$(sa02m_profile_skipped | tr '\n' ' ')
    [ -n "${SA02M_ROOTFS_BUILD:-}" ] && _pa_off="$_pa_off $_pa_skip"
    SA02M_PROFILE_SUMMARY=""
    for _pa_id in $SA02M_PROFILE_IDS; do
        case " $_pa_off " in *" $_pa_id "*) ;; *) continue ;; esac
        case " $_pa_keep " in *" $_pa_id "*) continue ;; esac
        export "SA02M_SKIP_${_pa_id}=1"
        SA02M_PROFILE_SUMMARY="${SA02M_PROFILE_SUMMARY:+$SA02M_PROFILE_SUMMARY, }без $_pa_id"
        _sa02m_profile_log INFO "Профиль установки: модуль $_pa_id не ставится ($SA02M_PROFILE_CONF); вернуть — install.sh --keep-module $_pa_id"
    done
    [ -n "$SA02M_PROFILE_SUMMARY" ] || SA02M_PROFILE_SUMMARY="все модули"
    return 0
}

# ── The module table ────────────────────────────────────────────────────────
# Every path is prefixed with $SA02M_PROFILE_ROOT. A pattern ending in `/` is a
# directory prefix; any other is one exact path. Validity rule (gate P7): no
# pattern may equal or contain /var/www/, /usr/local/libexec/, /etc/nginx/ or
# /usr/local/lib/ — a module row drops its own files, never the runner, the
# site file, the web tree or a shared lib.

# sa02m_profile_ota_patterns <ID> → the destinations OTA must not deliver.
sa02m_profile_ota_patterns() {
    _po_r=$SA02M_PROFILE_ROOT
    case "${1:-}" in
        AGENT_API)
            printf '%s\n' \
                "$_po_r/opt/sa02m-agent-api/" \
                "$_po_r/usr/local/sbin/sa02m-agent-api-ctl.sh" \
                "$_po_r/usr/local/sbin/sa02m-agent-token-store.sh" \
                "$_po_r/usr/local/sbin/sa02m-agent-root-cap.sh" \
                "$_po_r/usr/local/sbin/sa02m-agent-root-exec.sh" \
                "$_po_r/usr/local/sbin/sa02m-rules-store-apply.sh" \
                "$_po_r/usr/local/sbin/sa02m-user-unit.sh" \
                "$_po_r/usr/local/sbin/sa02m-agent-journal.sh" \
                "$_po_r/etc/systemd/system/sa02m-agent-api.service" \
                "$_po_r/etc/systemd/system/sa02m-user@.service" \
                "$_po_r/etc/sudoers.d/sa02m-agent-api" \
                "$_po_r/etc/tmpfiles.d/sa02m-agent-api.conf"
            ;;
        *) return 2 ;;
    esac
}

# sa02m_profile_install_only_paths <ID> → paths the installer writes that no
# OTA path delivers (removal still takes them).
sa02m_profile_install_only_paths() {
    case "${1:-}" in
        AGENT_API) printf '%s\n' "$SA02M_PROFILE_ROOT/etc/logrotate.d/sa02m-agent-api" ;;
        *) return 2 ;;
    esac
}

# sa02m_profile_kept_data <ID> → operator data removal never touches
# (`--keep-module` resumes with it). The system account is kept too.
sa02m_profile_kept_data() {
    case "${1:-}" in
        AGENT_API)
            printf '%s\n' \
                "$SA02M_PROFILE_ROOT/etc/sa02m-agent-api/" \
                "$SA02M_PROFILE_ROOT/opt/sa02m-user/" \
                "$SA02M_PROFILE_ROOT/var/log/sa02m-agent-api/"
            ;;
        *) return 2 ;;
    esac
}

# sa02m_profile_ota_excludes → `ID<TAB>pattern` for every skipped module.
sa02m_profile_ota_excludes() {
    for _px_id in $(sa02m_profile_skipped); do
        sa02m_profile_ota_patterns "$_px_id" | while IFS= read -r _px_p; do
            printf '%s\t%s\n' "$_px_id" "$_px_p"
        done
    done
    return 0
}

# sa02m_profile_path_excluded <DST> → 0 iff DST is an OTA destination of a
# skipped module (exact path, or under a `/`-terminated prefix). For the shell
# delivery loops (update-www-only.sh, the legacy rsync path).
sa02m_profile_path_excluded() {
    _pe_dst=${1:-}
    [ -n "$_pe_dst" ] || return 1
    for _pe_id in $(sa02m_profile_skipped); do
        for _pe_p in $(sa02m_profile_ota_patterns "$_pe_id"); do
            case "$_pe_p" in
                */) case "$_pe_dst" in "$_pe_p"*) return 0 ;; esac ;;
                *) [ "$_pe_dst" = "$_pe_p" ] && return 0 ;;
            esac
        done
    done
    return 1
}

# sa02m_profile_footprint_present <ID> → 0 iff any OTA or install-only path of
# the module exists.
sa02m_profile_footprint_present() {
    for _pf_p in $(sa02m_profile_ota_patterns "${1:-}") $(sa02m_profile_install_only_paths "${1:-}"); do
        [ -e "${_pf_p%/}" ] && return 0
    done
    return 1
}

# sa02m_profile_enforce <ID> → the installer's one call per ID: the FILE says
# skip (the one-shot SA02M_SKIP_<ID>=1 env never removes) and something of the
# module is on disk ⇒ remove it; otherwise a no-op. Never under a rootfs build.
sa02m_profile_enforce() {
    sa02m_profile_id_valid "${1:-}" || return 2
    [ -n "${SA02M_ROOTFS_BUILD:-}" ] && return 0
    [ "$(sa02m_profile_get "$1")" = skip ] || return 0
    if ! sa02m_profile_footprint_present "$1"; then
        _sa02m_profile_log INFO "Профиль установки: модуль $1 пропущен, удалять нечего"
        return 0
    fi
    sa02m_profile_remove_module "$1"
}

# sa02m_profile_remove_module <ID> → removes the module's code, units, sudoers
# grant and install-only files; keeps its data. Under the update lock (never
# interleaves with an OTA apply or a factory reset). Order is torn-run safe:
# the root grant goes first, then the running units, then the files. Any single
# failure ⇒ WARN with the path, continue, final rc 1. Lock timeout ⇒ nothing
# removed, rc 1.
sa02m_profile_remove_module() {
    sa02m_profile_id_valid "${1:-}" || return 2
    case "$SA02M_PROFILE_ROOT" in
        '') [ "$(id -u)" = 0 ] || { _sa02m_profile_log WARN "Профиль установки: удаление $1 требует root"; return 1; } ;;
        /*) case "$SA02M_PROFILE_ROOT" in *..*) _sa02m_profile_log WARN "Профиль установки: корень '$SA02M_PROFILE_ROOT' содержит '..' — удаление отказано"; return 1 ;; esac ;;
        *) _sa02m_profile_log WARN "Профиль установки: корень '$SA02M_PROFILE_ROOT' не абсолютный — удаление отказано"; return 1 ;;
    esac
    _pr_state="${SA02M_UPDATE_STATEDIR:-/var/lib/sa02m-update}"
    _pr_wait="${SA02M_PROFILE_LOCK_WAIT:-300}"
    case "$_pr_wait" in ''|*[!0-9]*) _pr_wait=300 ;; esac
    if ! command -v flock >/dev/null 2>&1; then
        _sa02m_profile_log WARN "Профиль установки: нет flock — удаление $1 отложено; повторите install.sh --refresh"
        return 1
    fi
    mkdir -p "$_pr_state" 2>/dev/null || true
    if [ ! -d "$_pr_state" ]; then
        _sa02m_profile_log WARN "Профиль установки: нет каталога блокировки $_pr_state — удаление $1 отложено"
        return 1
    fi
    # Subshell: the lock fd lives exactly as long as the removal.
    (
        if ! flock -w "$_pr_wait" 8; then
            _sa02m_profile_log WARN "Профиль установки: удаление $1 отложено — идёт обновление; повторите install.sh --refresh"
            exit 1
        fi
        _pr_rc=0
        _pr_sc=""
        command -v systemctl >/dev/null 2>&1 && _pr_sc=1
        # 1. The root grant first.
        for _pr_p in $(sa02m_profile_ota_patterns "$1"); do
            case "$_pr_p" in
                */etc/sudoers.d/*)
                    if [ -e "$_pr_p" ]; then
                        if rm -f "$_pr_p"; then
                            _sa02m_profile_log INFO "Профиль установки: удалён $_pr_p"
                        else
                            _sa02m_profile_log WARN "Профиль установки: не удалось удалить $_pr_p"
                            _pr_rc=1
                        fi
                    fi
                    ;;
            esac
        done
        if command -v visudo >/dev/null 2>&1 && [ -z "$SA02M_PROFILE_ROOT" ]; then
            visudo -c >/dev/null 2>&1 || _sa02m_profile_log WARN "visudo -c: в наборе sudoers есть ошибка — проверьте /etc/sudoers.d"
        fi
        # 2. Stop and disable the module's units (template units: every instance).
        for _pr_p in $(sa02m_profile_ota_patterns "$1"); do
            case "$_pr_p" in
                */etc/systemd/system/*.service) ;;
                *) continue ;;
            esac
            [ -e "$_pr_p" ] || continue
            [ -n "$_pr_sc" ] || continue
            _pr_u=${_pr_p##*/}
            case "$_pr_u" in
                *@.service)
                    _pr_units=$(timeout 10 systemctl list-units --all --plain --no-legend "${_pr_u%.service}*" 2>/dev/null \
                        | awk '{ print $1 }') || _pr_units=""
                    ;;
                *) _pr_units=$_pr_u ;;
            esac
            for _pr_x in $_pr_units; do
                timeout 30 systemctl stop "$_pr_x" >/dev/null 2>&1 \
                    || { _sa02m_profile_log WARN "Профиль установки: systemctl stop $_pr_x не удался"; _pr_rc=1; }
                timeout 30 systemctl disable "$_pr_x" >/dev/null 2>&1 \
                    || { _sa02m_profile_log WARN "Профиль установки: systemctl disable $_pr_x не удался"; _pr_rc=1; }
                _sa02m_profile_log INFO "Профиль установки: остановлен и выключен $_pr_x"
            done
        done
        # 3. Files (code, units, tmpfiles, install-only).
        for _pr_p in $(sa02m_profile_ota_patterns "$1") $(sa02m_profile_install_only_paths "$1"); do
            case "$_pr_p" in */) continue ;; esac
            [ -e "$_pr_p" ] || continue
            if rm -f "$_pr_p"; then
                _sa02m_profile_log INFO "Профиль установки: удалён $_pr_p"
            else
                _sa02m_profile_log WARN "Профиль установки: не удалось удалить $_pr_p"
                _pr_rc=1
            fi
        done
        # 4. Code directories — only the literal table paths.
        for _pr_p in $(sa02m_profile_ota_patterns "$1"); do
            case "$_pr_p" in */) ;; *) continue ;; esac
            _pr_d=${_pr_p%/}
            [ -e "$_pr_d" ] || continue
            if rm -rf "$_pr_d"; then
                _sa02m_profile_log INFO "Профиль установки: удалён каталог $_pr_d"
            else
                _sa02m_profile_log WARN "Профиль установки: не удалось удалить $_pr_d"
                _pr_rc=1
            fi
        done
        # 5. systemd forgets the removed units.
        if [ -n "$_pr_sc" ]; then
            timeout 30 systemctl daemon-reload >/dev/null 2>&1 \
                || { _sa02m_profile_log WARN "Профиль установки: systemctl daemon-reload не удался"; _pr_rc=1; }
        fi
        _sa02m_profile_log INFO "Профиль установки: модуль $1 удалён; данные сохранены: $(sa02m_profile_kept_data "$1" | tr '\n' ' ')— вернуть: install.sh --keep-module $1"
        exit "$_pr_rc"
    ) 8>"$_pr_state/update.lock"
}
