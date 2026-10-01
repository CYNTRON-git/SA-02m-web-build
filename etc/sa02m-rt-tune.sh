#!/bin/bash
# SA-02m: единственный дом привязки UART IRQ.
# План: .ai-dev/plans/rt-core-affinity.md (sa02m-rt-tune.sh).
# Редакция 2026-10-01, плата 192.168.1.135: только шаг, который сдвинул oe
# (лестница E2) — прерывания ttyS* с CPU0 на SA02M_RT_SERIAL_CPU (по умолчанию 3).
# Не делает: Ethernet, eMMC, default_smp_affinity, chrt (приоритет остаётся
# ядерным SCHED_FIFO 50), регулятор частоты. В образ по умолчанию не включено:
# приёмка «ноль oe за 30 мин» не закрыта.
# /usr/local/sbin/sa02m-rt-tune.sh {apply|status}
set -u

CONF=/etc/sa02m_rt.conf
LOCK=/run/sa02m-rt-tune.lock

log() { logger -t sa02m-rt-tune -- "$*" 2>/dev/null || true; }

read_conf() {
    SA02M_RT_TUNE=on
    SA02M_RT_SERIAL_CPU=3
    [ -f "$CONF" ] || return 0
    while IFS= read -r line || [ -n "${line:-}" ]; do
        case "$line" in
            ''|\#*) continue ;;
            SA02M_RT_TUNE=on|SA02M_RT_TUNE=off)
                SA02M_RT_TUNE=${line#*=}
                ;;
            SA02M_RT_SERIAL_CPU=[0-3])
                SA02M_RT_SERIAL_CPU=${line#*=}
                ;;
            *)
                log "ignored conf line (this revision only TUNE and SERIAL_CPU)"
                ;;
        esac
    done <"$CONF"
}

cpu_online() {
    local n=$1
    local base="/sys/devices/system/cpu/cpu${n}"
    local f="${base}/online"
    [ -d "$base" ] || return 1
    if [ -f "$f" ]; then
        [ "$(cat "$f" 2>/dev/null || true)" = "1" ]
        return
    fi
    return 0
}

each_ttys_irq() {
    awk '
        $NF ~ /^ttyS[0-9]+$/ {
            irq = $1
            sub(":", "", irq)
            if (irq ~ /^[0-9]+$/) print irq, $NF
        }
    ' /proc/interrupts
}

pin_one() {
    local irq=$1 name=$2 want=$3 f eff cur
    f="/proc/irq/${irq}/smp_affinity_list"
    if [ ! -w "$f" ]; then
        log "skip ${name} irq ${irq}: affinity not writable"
        return 0
    fi
    cur=$(tr -d '[:space:]' <"$f" 2>/dev/null || true)
    if [ "$cur" = "$want" ]; then
        return 0
    fi
    if ! printf '%s\n' "$want" >"$f" 2>/dev/null; then
        log "write failed ${name} irq ${irq}"
        return 0
    fi
    eff=$(tr -d '[:space:]' <"/proc/irq/${irq}/effective_affinity_list" 2>/dev/null || true)
    if [ "$eff" != "$want" ]; then
        log "controller ignored affinity ${name} irq ${irq} eff=${eff} want=${want}"
    else
        log "pinned ${name} irq ${irq} cpu ${want}"
    fi
}

apply() {
    local irq name want
    exec 9>"$LOCK" || { log "lock open failed"; return 0; }
    flock -w 5 9 || { log "lock busy"; return 0; }
    read_conf
    if [ "$SA02M_RT_TUNE" = "off" ]; then
        want="0-3"
    else
        want="$SA02M_RT_SERIAL_CPU"
        if ! cpu_online "$want"; then
            log "skip: cpu ${want} absent or offline"
            return 0
        fi
    fi
    while read -r irq name; do
        [ -n "${irq:-}" ] || continue
        pin_one "$irq" "$name" "$want"
    done < <(each_ttys_irq)
}

status() {
    local irq name aff eff
    read_conf
    printf 'tune=%s serial_cpu=%s prio=untouched eth=untouched mmc=untouched governor=untouched\n' \
        "$SA02M_RT_TUNE" "$SA02M_RT_SERIAL_CPU"
    while read -r irq name; do
        [ -n "${irq:-}" ] || continue
        aff=$(tr -d '[:space:]' <"/proc/irq/${irq}/smp_affinity_list" 2>/dev/null || true)
        eff=$(tr -d '[:space:]' <"/proc/irq/${irq}/effective_affinity_list" 2>/dev/null || true)
        printf '%s irq=%s aff=%s eff=%s\n' "$name" "$irq" "$aff" "$eff"
    done < <(each_ttys_irq)
}

cmd=${1:-apply}
case "$cmd" in
    apply) apply ;;
    status) status ;;
    *) log "unknown command: $cmd" ;;
esac
exit 0
