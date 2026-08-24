#!/bin/bash
# sa02m-usb-power.sh — the ONLY GPIO / USB-power capability granted to www-data
# via sudoers (replaces the former raw `tee` to sysfs + raw `gpioset`/`gpioget`
# + raw `kill`, audit B1). Exists so the «Дискретный выход, USB-питание и
# индикация» panel can drive the board's GPIO lines WITHOUT www-data holding
# arbitrary root file-write (tee), arbitrary GPIO (gpioset) or arbitrary kill.
#
# Closed verb set; every argument validated against the board's own
# /etc/sa02m_hw.conf (numeric pins/lines, config-matched) and every path built
# INTERNALLY from a numeric pin — no request byte reaches a raw tool or a path.
# The kill of a holder is internal and refuses any PID whose /proc cmdline is
# not our gpioset on our line. Domain: docs/agent-rules/sa02m-domain.md.
#
# Verbs:
#   sysfs-export-out <pin>          export + set direction out (/sys/class/gpio)
#   sysfs-write      <pin> <0|1>    write a value to gpio<pin>/value
#   gpiod-set        <chip> <line> <0|1>   stop old holder, spawn a persistent
#                                          gpioset holder, persist pid/state
#   gpiod-stop       <chip> <line>  kill our holder(s), clear pid/state
#   gpiod-get        <chip> <line>  print the line value (0/1) or nothing
#   gpiod-commit     <chip> <line> <pid> <raw>  persist a verified live holder
#
# Exit: 0 ok; 2 validation refused; 1 operation failed.
set -o pipefail

HW_CONF="${HW_CONF:-/etc/sa02m_hw.conf}"
LOG="/var/log/sa02m_install.log"

# Config is the allow-list source: the panel's known channel pins and the USB
# gpiod chip/line. Sourced (root-owned file) so a request cannot widen the set.
SA02M_GPIO_DO=""
SA02M_GPIO_BEEPER=""
SA02M_GPIO_ALARM_LED=""
SA02M_GPIO_USB_POWER=""
SA02M_GPIO_USB_GPIOD_CHIP=""
SA02M_GPIO_USB_GPIOD_LINE=""
# shellcheck disable=SC1090
[ -f "$HW_CONF" ] && . "$HW_CONF" 2>/dev/null || true

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-usb-power: $*" >> "$LOG" 2>&1 || true
}
refuse() { log "REFUSED ($1)"; exit 2; }

is_num() { [[ "$1" =~ ^[0-9]+$ ]]; }

# A sysfs pin is allowed only when it equals one of the four configured channel
# pins — never an arbitrary number. So even with the sudoers grant, www-data
# cannot drive a line the board's own config does not name.
pin_allowed() {
    local p=$1 c
    is_num "$p" || return 1
    for c in "$SA02M_GPIO_DO" "$SA02M_GPIO_BEEPER" "$SA02M_GPIO_ALARM_LED" "$SA02M_GPIO_USB_POWER"; do
        [ "$c" = "$p" ] && is_num "$c" && return 0
    done
    return 1
}

# The gpiod chip/line must match the board's configured USB-power line.
chipline_allowed() {
    local chip=$1 line=$2 cfg_chip="${SA02M_GPIO_USB_GPIOD_CHIP:-0}"
    is_num "$chip" || return 1
    is_num "$line" || return 1
    is_num "$cfg_chip" || cfg_chip=0
    [ "$chip" = "$cfg_chip" ] || return 1
    [ "$line" = "${SA02M_GPIO_USB_GPIOD_LINE:-}" ] && is_num "$SA02M_GPIO_USB_GPIOD_LINE"
}

pidfile() { printf '%s' "/tmp/sa02m-gpioset-usb-power-c${1}-l${2}.pid"; }
statefile() { printf '%s' "/tmp/sa02m-gpioset-usb-power-c${1}-l${2}.state"; }

# Is <pid> a gpioset process holding our chip/line? (with optional exact value)
holder_matches() {
    local pid=$1 chip=$2 line=$3 want=${4:-} cmd
    is_num "$pid" || return 1
    cmd=$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null) || return 1
    [[ "$cmd" =~ gpioset ]] || return 1
    [[ "$cmd" =~ (^|[[:space:]])${chip}[[:space:]] ]] || return 1
    if [[ "$cmd" =~ (^|[[:space:]])${line}=([01])([[:space:]]|$) ]]; then
        [ -z "$want" ] && return 0
        [ "${BASH_REMATCH[2]}" = "$want" ] && return 0
    fi
    return 1
}

kill_holders() {
    # holder_matches (reads /proc/<pid>/cmdline) runs IMMEDIATELY before the kill
    # — the tightest TOCTOU window shell allows. A PID recycled inside the `&&`
    # window is astronomically unlikely and its worst case is a TERM to an
    # unrelated local process (a local DoS, not an escalation): the helper runs
    # as root only over its own gpioset line, never a request-supplied PID.
    local chip=$1 line=$2 pid
    for pid in $(pgrep -x gpioset 2>/dev/null); do
        holder_matches "$pid" "$chip" "$line" && kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 0.12
    for pid in $(pgrep -x gpioset 2>/dev/null); do
        holder_matches "$pid" "$chip" "$line" && kill -KILL "$pid" 2>/dev/null || true
    done
}

# Spawn a detached, persistent holder that survives this helper's exit.
spawn_bg() {
    if command -v setsid >/dev/null 2>&1; then
        setsid "$@" </dev/null >/dev/null 2>&1 &
    else
        nohup "$@" </dev/null >/dev/null 2>&1 &
    fi
    return 0
}

wait_holder() {
    local chip=$1 line=$2 want=$3 tries=${4:-24} i pid
    for i in $(seq 1 "$tries"); do
        for pid in $(pgrep -x gpioset 2>/dev/null); do
            if holder_matches "$pid" "$chip" "$line" "$want"; then
                printf '%s' "$pid"; return 0
            fi
        done
        sleep 0.05
    done
    return 1
}

commit_holder() {
    local chip=$1 line=$2 pid=$3 raw=$4 pf sf
    holder_matches "$pid" "$chip" "$line" || return 1
    pf=$(pidfile "$chip" "$line"); sf=$(statefile "$chip" "$line")
    echo "$pid" > "$pf" 2>/dev/null || return 1
    printf '%s' "$raw" > "$sf" 2>/dev/null || return 1
    chmod 644 "$pf" "$sf" 2>/dev/null || true
    return 0
}

verb="${1:-}"
case "$verb" in
    sysfs-export-out)
        pin="${2:-}"
        pin_allowed "$pin" || refuse "sysfs pin not a configured channel: $pin"
        if [ ! -d "/sys/class/gpio/gpio${pin}" ]; then
            echo "$pin" > /sys/class/gpio/export 2>/dev/null || true
            sleep 0.08
        fi
        [ -d "/sys/class/gpio/gpio${pin}" ] || exit 1
        echo out > "/sys/class/gpio/gpio${pin}/direction" 2>/dev/null || exit 1
        exit 0
        ;;
    sysfs-write)
        pin="${2:-}"; val="${3:-}"
        pin_allowed "$pin" || refuse "sysfs pin not a configured channel: $pin"
        [ "$val" = "0" ] || [ "$val" = "1" ] || refuse "sysfs value not 0|1: $val"
        echo "$val" > "/sys/class/gpio/gpio${pin}/value" 2>/dev/null || exit 1
        exit 0
        ;;
    gpiod-set)
        chip="${2:-}"; line="${3:-}"; raw="${4:-}"
        chipline_allowed "$chip" "$line" || refuse "gpiod chip/line not the configured USB line: $chip/$line"
        [ "$raw" = "0" ] || [ "$raw" = "1" ] || refuse "gpiod value not 0|1: $raw"
        gs=$(command -v gpioset 2>/dev/null) || exit 1
        help=$("$gs" -h 2>&1 || true)
        kill_holders "$chip" "$line"
        _commit_after_spawn() {
            local hp
            hp=$(wait_holder "$chip" "$line" "$raw") || return 1
            commit_holder "$chip" "$line" "$hp" "$raw"
        }
        if echo "$help" | grep -q -- '-m'; then
            # -m signal holds the line until SIGTERM/SIGINT and does not drop on
            # stdin EOF; -m wait + /dev/null would release immediately.
            if echo "$help" | grep -qi 'signal'; then
                spawn_bg "$gs" -m signal "$chip" "${line}=${raw}" \
                    && _commit_after_spawn && exit 0
            fi
            if echo "$help" | grep -qi 'wait'; then
                spawn_bg "$gs" -m wait "$chip" "${line}=${raw}" \
                    && _commit_after_spawn && exit 0
            fi
            if echo "$help" | grep -qi 'time'; then
                if echo "$help" | grep -qE '\-\-sec|[[:space:]]-s[[:space:]]'; then
                    spawn_bg "$gs" -m time -s 604800 "$chip" "${line}=${raw}" \
                        && _commit_after_spawn && exit 0
                fi
                if echo "$help" | grep -qi usec; then
                    spawn_bg "$gs" -m time --usec=604800000000 "$chip" "${line}=${raw}" \
                        && _commit_after_spawn && exit 0
                fi
            fi
            # Never -m exit: the process ends and the line is often released.
        fi
        if spawn_bg "$gs" "$chip" "${line}=${raw}" && _commit_after_spawn; then
            exit 0
        fi
        exit 1
        ;;
    gpiod-stop)
        chip="${2:-}"; line="${3:-}"
        chipline_allowed "$chip" "$line" || refuse "gpiod chip/line not the configured USB line: $chip/$line"
        kill_holders "$chip" "$line"
        rm -f "$(pidfile "$chip" "$line")" 2>/dev/null || true
        exit 0
        ;;
    gpiod-get)
        chip="${2:-}"; line="${3:-}"
        chipline_allowed "$chip" "$line" || refuse "gpiod chip/line not the configured USB line: $chip/$line"
        gg=$(command -v gpioget 2>/dev/null) || exit 1
        v=$("$gg" "$chip" "$line" 2>/dev/null) || exit 1
        printf '%s' "$v"
        exit 0
        ;;
    gpiod-commit)
        chip="${2:-}"; line="${3:-}"; pid="${4:-}"; raw="${5:-}"
        chipline_allowed "$chip" "$line" || refuse "gpiod chip/line not the configured USB line: $chip/$line"
        [ "$raw" = "0" ] || [ "$raw" = "1" ] || refuse "gpiod value not 0|1: $raw"
        commit_holder "$chip" "$line" "$pid" "$raw" || exit 1
        exit 0
        ;;
    *)
        refuse "unknown verb: $verb"
        ;;
esac
