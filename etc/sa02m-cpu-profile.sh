#!/bin/bash
# SA-02m: профили частоты CPU через sysfs cpufreq (только SMP, не RT)
# /usr/local/sbin/sa02m-cpu-profile.sh {probe|status|apply|set PROFILE|init}
set -euo pipefail

CONF=/etc/sa02m_cpu.conf
LOG=/var/log/sa02m_install.log
VALID_PROFILES="performance high medium low adaptive"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-cpu-profile: $*" >>"$LOG" 2>&1 || true; }

is_rt_kernel() {
    local kr
    kr=$(uname -r 2>/dev/null || true)
    case "$kr" in
        *-rt*|*rt[0-9]*) return 0 ;;
    esac
    grep -q 'PREEMPT_RT' /proc/version 2>/dev/null
}

cpufreq_present() {
    [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ]
}

load_conf() {
    SA02M_CPU_PROFILE="${SA02M_CPU_PROFILE:-adaptive}"
    if [ -f "$CONF" ]; then
        # shellcheck disable=SC1090
        . "$CONF" 2>/dev/null || true
    fi
}

write_conf() {
    local prof=$1
    umask 022
    cat >"$CONF" <<EOF
# Управляется веб-панелью СА-02m. Сохраняется после перезагрузки.
# Применяется на boot только на SMP-ядре (не RT).
SA02M_CPU_PROFILE=$prof
EOF
    chmod 644 "$CONF"
}

invalidate_status_cache() {
    rm -f /tmp/sa02m_status_cache/system.json /tmp/sa02m_status_cache/core.json 2>/dev/null || true
}

read_opps() {
    local f=/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_frequencies
    if [ ! -f "$f" ]; then
        return 1
    fi
    tr ' \t' '\n' <"$f" | sed '/^$/d' | sort -n
}

pick_opp_index() {
    local pct=$1 n idx
    mapfile -t _opps < <(read_opps)
    n=${#_opps[@]}
    [ "$n" -gt 0 ] || return 1
    idx=$(( (n - 1) * pct / 100 ))
    printf '%s' "${_opps[$idx]}"
}

pick_max_opp() {
    read_opps | tail -n1
}

pick_min_opp() {
    read_opps | head -n1
}

governor_available() {
    local want=$1
    grep -qw "$want" /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors 2>/dev/null
}

pick_adaptive_governor() {
    if governor_available schedutil; then echo schedutil; return; fi
    if governor_available ondemand; then echo ondemand; return; fi
    if governor_available conservative; then echo conservative; return; fi
    echo performance
}

apply_to_cpu() {
    local cpu=$1 prof=$2 gov max_k min_k cpuinfo_max
    local base="/sys/devices/system/cpu/cpu${cpu}/cpufreq"
    [ -d "$base" ] || return 0

    cpuinfo_max=$(cat "$base/cpuinfo_max_freq" 2>/dev/null || pick_max_opp)

    case "$prof" in
        performance)
            if governor_available performance; then
                echo performance >"$base/scaling_governor" 2>/dev/null || true
            fi
            [ -n "$cpuinfo_max" ] && echo "$cpuinfo_max" >"$base/scaling_max_freq" 2>/dev/null || true
            ;;
        adaptive)
            gov=$(pick_adaptive_governor)
            echo "$gov" >"$base/scaling_governor" 2>/dev/null || true
            min_k=$(pick_min_opp)
            max_k=$(pick_max_opp)
            [ -n "$min_k" ] && echo "$min_k" >"$base/scaling_min_freq" 2>/dev/null || true
            [ -n "$max_k" ] && echo "$max_k" >"$base/scaling_max_freq" 2>/dev/null || true
            ;;
        high)
            gov=$(pick_adaptive_governor)
            echo "$gov" >"$base/scaling_governor" 2>/dev/null || true
            max_k=$(pick_max_opp)
            [ -n "$max_k" ] && echo "$max_k" >"$base/scaling_max_freq" 2>/dev/null || true
            ;;
        medium)
            gov=$(pick_adaptive_governor)
            echo "$gov" >"$base/scaling_governor" 2>/dev/null || true
            max_k=$(pick_opp_index 66)
            [ -n "$max_k" ] && echo "$max_k" >"$base/scaling_max_freq" 2>/dev/null || true
            ;;
        low)
            if governor_available powersave; then
                echo powersave >"$base/scaling_governor" 2>/dev/null || true
            else
                gov=$(pick_adaptive_governor)
                echo "$gov" >"$base/scaling_governor" 2>/dev/null || true
                max_k=$(pick_min_opp)
                [ -n "$max_k" ] && echo "$max_k" >"$base/scaling_max_freq" 2>/dev/null || true
            fi
            ;;
        *)
            return 1
            ;;
    esac
}

apply_profile() {
    local prof=${1:-}
    local cpu n=0

    if is_rt_kernel; then
        log "apply skipped: RT kernel active"
        return 0
    fi
    if ! cpufreq_present; then
        log "apply failed: cpufreq unavailable"
        return 1
    fi
    load_conf
    [ -n "$prof" ] || prof="$SA02M_CPU_PROFILE"
    case " $VALID_PROFILES " in
        *" $prof "*) ;;
        *) log "apply failed: invalid profile $prof"; return 1 ;;
    esac

    while [ -d "/sys/devices/system/cpu/cpu${n}" ]; do
        if [ -f "/sys/devices/system/cpu/cpu${n}/cpufreq/scaling_governor" ]; then
            apply_to_cpu "$n" "$prof" || true
        fi
        n=$((n + 1))
    done
    log "apply profile=$prof on ${n} cpu(s)"
    invalidate_status_cache
    return 0
}

detect_profile_from_hw() {
    local gov
    gov=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo adaptive)
    case "$gov" in
        performance) echo performance ;;
        powersave) echo low ;;
        *) echo adaptive ;;
    esac
}

cmd_probe() {
    if ! cpufreq_present; then
        echo "cpufreq: unavailable"
        return 1
    fi
    echo "governors: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors 2>/dev/null)"
    echo "opps_khz: $(read_opps | tr '\n' ' ')"
    echo "rt_kernel: $(is_rt_kernel && echo yes || echo no)"
}

cmd_status_json() {
    load_conf
    local gov cur min max min_m max_m cur_m ui=0 rt=0 cp=0
    is_rt_kernel && rt=1
    cpufreq_present && cp=1
    if [ "$cp" = 1 ] && [ "$rt" = 0 ]; then
        ui=1
    fi
    gov=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "")
    cur=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo 0)
    min=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq 2>/dev/null || echo 0)
    max=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq 2>/dev/null || echo 0)
    min_m=$(( min / 1000 ))
    max_m=$(( max / 1000 ))
    cur_m=$(( cur / 1000 ))
    printf '{"ok":true,"profile":"%s","governor":"%s","cur_mhz":%s,"min_mhz":%s,"max_mhz":%s,"cpufreq_present":%s,"kernel_is_rt":%s,"cpu_profile_ui_available":%s}\n' \
        "$SA02M_CPU_PROFILE" "$gov" "$cur_m" "$min_m" "$max_m" "$cp" "$rt" "$ui"
}

cmd_set() {
    local prof=${1:-}
    case " $VALID_PROFILES " in
        *" $prof "*) ;;
        *)
            printf '{"ok":false,"error":"bad_profile"}\n'
            return 1
            ;;
    esac
    if is_rt_kernel; then
        printf '{"ok":false,"error":"rt_kernel_active"}\n'
        return 1
    fi
    if ! cpufreq_present; then
        printf '{"ok":false,"error":"cpufreq_unavailable"}\n'
        return 1
    fi
    write_conf "$prof"
    if ! apply_profile "$prof"; then
        printf '{"ok":false,"error":"apply_failed","profile":"%s"}\n' "$prof"
        return 1
    fi
    printf '{"ok":true,"profile":"%s"}\n' "$prof"
}

main() {
    case "${1:-status}" in
        probe) cmd_probe ;;
        status) cmd_status_json ;;
        apply) apply_profile "${2:-}" ;;
        set) cmd_set "${2:-}" ;;
        init)
            if [ ! -f "$CONF" ]; then
                local det=adaptive
                if cpufreq_present && ! is_rt_kernel; then
                    det=$(detect_profile_from_hw)
                fi
                write_conf "$det"
                log "init: created conf profile=$det"
            fi
            if ! is_rt_kernel; then
                apply_profile "" || true
            fi
            ;;
        *)
            printf '{"ok":false,"error":"usage"}\n'
            return 1
            ;;
    esac
}

main "$@"
