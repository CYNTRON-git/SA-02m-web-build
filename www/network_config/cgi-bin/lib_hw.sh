#!/bin/bash

HW_CONF="${HW_CONF:-/etc/sa02m_hw.conf}"

SA02M_HW_BACKEND="${SA02M_HW_BACKEND:-auto}"

SA02M_GPIO_DO="${SA02M_GPIO_DO:-}"
SA02M_GPIO_BEEPER="${SA02M_GPIO_BEEPER:-}"
SA02M_GPIO_ALARM_LED="${SA02M_GPIO_ALARM_LED:-}"
SA02M_GPIO_USB_POWER="${SA02M_GPIO_USB_POWER:-}"
# Питание USB через libgpiod (когда линия не в sysfs), например: gpioset 0 268=1
SA02M_GPIO_USB_GPIOD_CHIP="${SA02M_GPIO_USB_GPIOD_CHIP:-}"
SA02M_GPIO_USB_GPIOD_LINE="${SA02M_GPIO_USB_GPIOD_LINE:-}"
# Сброс USB: test_fb.cpp (MasterPLC) — USB_Counter > 100 при периоде FB 100 ms → 10 s.
SA02M_USB_POWER_RESET_CYCLES="${SA02M_USB_POWER_RESET_CYCLES:-100}"
SA02M_USB_POWER_RESET_CYCLE_MS="${SA02M_USB_POWER_RESET_CYCLE_MS:-100}"

SA02M_I2C_EXP_BUS="${SA02M_I2C_EXP_BUS:-2}"
SA02M_I2C_EXP_ADDR="${SA02M_I2C_EXP_ADDR:-0x41}"
SA02M_I2C_LOCK_FILE="${SA02M_I2C_LOCK_FILE:-/run/lock/sa02m-pca9536.lock}"
SA02M_I2C_LOCK_WAIT_SEC="${SA02M_I2C_LOCK_WAIT_SEC:-1}"
SA02M_I2C_TIMEOUT_SEC="${SA02M_I2C_TIMEOUT_SEC:-1}"
SA02M_I2C_ACTIVE_LOW_MASK="${SA02M_I2C_ACTIVE_LOW_MASK:-auto}"
SA02M_I2C_OWNER_UNITS="${SA02M_I2C_OWNER_UNITS:-mplc.service mplc4.service klogic.service klogicd.service}"
SA02M_I2C_OWNER_PROCS="${SA02M_I2C_OWNER_PROCS:-mplc mplc4 klogic klogicd klogic-sa02}"
SA02M_I2C_RESPECT_OWNER="${SA02M_I2C_RESPECT_OWNER:-1}"
SA02M_BEEPER_WEB_OVERRIDE_SEC="${SA02M_BEEPER_WEB_OVERRIDE_SEC:-7}"
SA02M_BEEPER_OVERRIDE_FILE="${SA02M_BEEPER_OVERRIDE_FILE:-/run/sa02m-hw-override/beeper.env}"
SA02M_BEEPER_OVERRIDE_WORKER="${SA02M_BEEPER_OVERRIDE_WORKER:-/usr/local/sbin/sa02m-beeper-override.sh}"
SA02M_I2C_BIT_DO="${SA02M_I2C_BIT_DO:-1}"
SA02M_I2C_BIT_BEEPER="${SA02M_I2C_BIT_BEEPER:-2}"
SA02M_I2C_BIT_ALARM_LED="${SA02M_I2C_BIT_ALARM_LED:-0}"
SA02M_I2C_BIT_USB_POWER="${SA02M_I2C_BIT_USB_POWER:-}"
SA02M_I2C_EXTRA_OUTPUT_MASK="${SA02M_I2C_EXTRA_OUTPUT_MASK:-0x08}"

[ -f "$HW_CONF" ] && . "$HW_CONF" 2>/dev/null || true

SA02M_HW_RC_BUSY=75
SA02M_HW_RC_TIMEOUT=76
SA02M_HW_RC_IO=77
SA02M_HW_RC_TOOL=78

sa02m_hw_backend() {
    case "${SA02M_HW_BACKEND:-auto}" in
        off|disabled|none) echo "disabled" ;;
        i2c|i2c_expander) echo "i2c_expander" ;;
        gpio|gpio_sysfs)  echo "gpio_sysfs" ;;
        *)
            if [[ "${SA02M_GPIO_DO:-}" =~ ^[0-9]+$ ]] \
                || [[ "${SA02M_GPIO_BEEPER:-}" =~ ^[0-9]+$ ]] \
                || [[ "${SA02M_GPIO_ALARM_LED:-}" =~ ^[0-9]+$ ]] \
                || [[ "${SA02M_GPIO_USB_POWER:-}" =~ ^[0-9]+$ ]]; then
                echo "gpio_sysfs"
            else
                echo "i2c_expander"
            fi
            ;;
    esac
}

sa02m_hw_use_i2c() {
    [ "$(sa02m_hw_backend)" = "i2c_expander" ]
}

sa02m_hw_use_gpio() {
    [ "$(sa02m_hw_backend)" = "gpio_sysfs" ]
}

sa02m_hw_gpio_pin() {
    case "$1" in
        do)        printf '%s' "${SA02M_GPIO_DO:-}" ;;
        beeper)    printf '%s' "${SA02M_GPIO_BEEPER:-}" ;;
        alarm_led) printf '%s' "${SA02M_GPIO_ALARM_LED:-}" ;;
        usb_power) printf '%s' "${SA02M_GPIO_USB_POWER:-}" ;;
        *) return 1 ;;
    esac
}

# ── USB power через libgpiod (линия не обязана совпадать с sysfs gpioN), напр. gpioset 0 268=1
sa02m_hw_usb_power_use_gpiod() {
    [[ "${SA02M_GPIO_USB_GPIOD_LINE:-}" =~ ^[0-9]+$ ]]
}

# Смысл в UI/Modbus: 1 = питание USB включено, 0 = выключено (сброс линии).
# Опрос: при SA02M_USB_POWER_INVERT=1 сырой уровень gpio в инверсии к подписи UI.
# Запись: то же отображение — UI «ВКЛ»(1) преобразуется в сырое значение для gpioset (см. sa02m_hw_usb_logical_to_raw_line).
# На типичной СА-02м VBUS включено при gpioset …=1 → держите SA02M_USB_POWER_INVERT=0 (см. pre-start).
sa02m_hw_usb_power_read_invert() {
    case "${SA02M_USB_POWER_INVERT:-0}" in
        1|yes|true|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

sa02m_hw_usb_line_to_user_logical() {
    local raw=$1
    if sa02m_hw_usb_power_read_invert; then
        case "$raw" in
            0) echo 1 ;;
            1) echo 0 ;;
            *) echo "$raw" ;;
        esac
    else
        printf '%s\n' "$raw"
    fi
}

# UI-логика → значение для gpioset (линия 268=1 = питание на типичной плате при INVERT=0).
sa02m_hw_usb_logical_to_raw_line() {
    local logical=$1
    if sa02m_hw_usb_power_read_invert; then
        case "$logical" in
            1) printf '0' ;;
            0) printf '1' ;;
            *) printf '%s' "$logical" ;;
        esac
    else
        printf '%s' "$logical"
    fi
}

sa02m_hw_usb_gpiod_chip() {
    printf '%s' "${SA02M_GPIO_USB_GPIOD_CHIP:-0}"
}

# Pid в /tmp (www-data не пишет в /run). Остановка процесса root — через sudo kill (см. sudoers).
sa02m_hw_usb_gpiod_pidfile() {
    local chip line
    chip=$(sa02m_hw_usb_gpiod_chip)
    line=${SA02M_GPIO_USB_GPIOD_LINE:-}
    printf '%s' "/tmp/sa02m-gpioset-usb-power-c${chip}-l${line}.pid"
}

# Файл состояния: хранит последнее записанное raw-значение линии (0 или 1).
# Используется в read когда gpioget не может прочитать занятую линию (-m signal держит её).
sa02m_hw_usb_gpiod_statefile() {
    local chip line
    chip=$(sa02m_hw_usb_gpiod_chip)
    line=${SA02M_GPIO_USB_GPIOD_LINE:-}
    printf '%s' "/tmp/sa02m-gpioset-usb-power-c${chip}-l${line}.state"
}

# Найти gpioset-процессы, удерживающие нашу линию (chip/offset=value).
sa02m_hw_usb_gpiod_foreach_holder() {
    local chip line _gs_pid _gs_cmd _gs_val
    chip=$(sa02m_hw_usb_gpiod_chip)
    line=${SA02M_GPIO_USB_GPIOD_LINE:-}
    [[ "$line" =~ ^[0-9]+$ ]] || return 0
    for _gs_pid in $(pgrep -x gpioset 2>/dev/null); do
        _gs_cmd=$(tr '\0' ' ' < "/proc/${_gs_pid}/cmdline" 2>/dev/null) || continue
        [[ "$_gs_cmd" =~ gpioset ]] || continue
        [[ "$_gs_cmd" =~ (^|[[:space:]])${chip}[[:space:]] ]] || continue
        if [[ "$_gs_cmd" =~ (^|[[:space:]])${line}=([01])([[:space:]]|$) ]]; then
            _gs_val="${BASH_REMATCH[2]}"
            "$1" "$_gs_pid" "$_gs_val"
        fi
    done
}

sa02m_hw_usb_gpiod_kill_one_holder() {
    local pid=$1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 0
    sudo -n kill -TERM "$pid" 2>/dev/null || sudo kill -TERM "$pid" 2>/dev/null || true
}

sa02m_hw_usb_gpiod_kill_holders() {
    local pf
    pf=$(sa02m_hw_usb_gpiod_pidfile)
    sa02m_hw_usb_gpiod_foreach_holder sa02m_hw_usb_gpiod_kill_one_holder
    sleep 0.12
    sa02m_hw_usb_gpiod_foreach_holder sa02m_hw_usb_gpiod_force_kill_one_holder
    sudo -n rm -f "$pf" 2>/dev/null || sudo rm -f "$pf" 2>/dev/null || rm -f "$pf" 2>/dev/null || true
}

sa02m_hw_usb_gpiod_force_kill_one_holder() {
    local pid=$1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 0
    sudo -n kill -KILL "$pid" 2>/dev/null || sudo kill -KILL "$pid" 2>/dev/null || true
}

sa02m_hw_usb_gpiod_stop_holder() {
    sa02m_hw_usb_gpiod_kill_holders
    sleep 0.2
}

sa02m_hw_usb_gpiod_find_holder_pid_for_raw() {
    local want_raw=$1 chip line _gs_pid _gs_cmd _gs_val
    chip=$(sa02m_hw_usb_gpiod_chip)
    line=${SA02M_GPIO_USB_GPIOD_LINE:-}
    [[ "$line" =~ ^[0-9]+$ ]] || return 1
    for _gs_pid in $(pgrep -x gpioset 2>/dev/null); do
        _gs_cmd=$(tr '\0' ' ' < "/proc/${_gs_pid}/cmdline" 2>/dev/null) || continue
        [[ "$_gs_cmd" =~ gpioset ]] || continue
        [[ "$_gs_cmd" =~ (^|[[:space:]])${chip}[[:space:]] ]] || continue
        if [[ "$_gs_cmd" =~ (^|[[:space:]])${line}=([01])([[:space:]]|$) ]]; then
            _gs_val="${BASH_REMATCH[2]}"
            if [ "$_gs_val" = "$want_raw" ]; then
                printf '%s' "$_gs_pid"
                return 0
            fi
        fi
    done
    return 1
}

sa02m_hw_usb_gpiod_wait_holder() {
    local want_raw=$1 tries=${2:-24} i holder_pid
    [ "$want_raw" = "0" ] || [ "$want_raw" = "1" ] || return 1
    for i in $(seq 1 "$tries"); do
        if holder_pid=$(sa02m_hw_usb_gpiod_find_holder_pid_for_raw "$want_raw"); then
            printf '%s' "$holder_pid"
            return 0
        fi
        sleep 0.05
    done
    return 1
}

sa02m_hw_usb_gpiod_commit_holder() {
    local holder_pid=$1 raw=$2 pf sf cmd
    pf=$(sa02m_hw_usb_gpiod_pidfile)
    sf=$(sa02m_hw_usb_gpiod_statefile)
    [[ "$holder_pid" =~ ^[0-9]+$ ]] || return 1
    cmd=$(tr '\0' ' ' < "/proc/${holder_pid}/cmdline" 2>/dev/null) || return 1
    [[ "$cmd" =~ gpioset ]] || return 1
    echo "$holder_pid" | sudo -n tee "$pf" >/dev/null 2>&1 \
        || echo "$holder_pid" | sudo tee "$pf" >/dev/null 2>&1 \
        || return 1
    printf '%s' "$raw" | sudo -n tee "$sf" >/dev/null 2>&1 \
        || printf '%s' "$raw" | sudo tee "$sf" >/dev/null 2>&1 \
        || return 1
    sudo -n chmod 644 "$pf" "$sf" 2>/dev/null || sudo chmod 644 "$pf" "$sf" 2>/dev/null || true
    return 0
}

sa02m_hw_usb_gpiod_spawn_bg() {
    if sudo -n "$@" </dev/null >/dev/null 2>&1 & then
        return 0
    fi
    sudo "$@" </dev/null >/dev/null 2>&1 &
}

sa02m_hw_usb_gpiod_write() {
    local logical=$1 chip line gs help pf sf raw holder_pid
    chip=$(sa02m_hw_usb_gpiod_chip)
    line=${SA02M_GPIO_USB_GPIOD_LINE:-}
    [[ "$line" =~ ^[0-9]+$ ]] || return 1
    [ "$logical" = "0" ] || [ "$logical" = "1" ] || return 1
    raw=$(sa02m_hw_usb_logical_to_raw_line "$logical")
    [ "$raw" = "0" ] || [ "$raw" = "1" ] || return 1
    gs=$(command -v gpioset 2>/dev/null) || return 1
    help=$("$gs" -h 2>&1 || true)

    sa02m_hw_usb_gpiod_stop_holder
    pf=$(sa02m_hw_usb_gpiod_pidfile)
    sf=$(sa02m_hw_usb_gpiod_statefile)

    _usb_gpiod_commit_after_spawn() {
        holder_pid=$(sa02m_hw_usb_gpiod_wait_holder "$raw") || return 1
        sa02m_hw_usb_gpiod_commit_holder "$holder_pid" "$raw"
    }

    if echo "$help" | grep -q -- '-m'; then
        # Предпочитаем -m signal: держит линию до SIGTERM/SIGINT, не падает от EOF stdin.
        # -m wait + /dev/null = немедленный выход, линия отпускается, питание гаснет.
        if echo "$help" | grep -qi 'signal'; then
            sa02m_hw_usb_gpiod_spawn_bg "$gs" -m signal "$chip" "${line}=${raw}" \
                && _usb_gpiod_commit_after_spawn && return 0
        fi
        if echo "$help" | grep -qi 'wait'; then
            sa02m_hw_usb_gpiod_spawn_bg "$gs" -m wait "$chip" "${line}=${raw}" \
                && _usb_gpiod_commit_after_spawn && return 0
        fi
        if echo "$help" | grep -qi 'time'; then
            if echo "$help" | grep -qE '\-\-sec|[[:space:]]-s[[:space:]]'; then
                sa02m_hw_usb_gpiod_spawn_bg "$gs" -m time -s 604800 "$chip" "${line}=${raw}" \
                    && _usb_gpiod_commit_after_spawn && return 0
            fi
            if echo "$help" | grep -qi usec; then
                sa02m_hw_usb_gpiod_spawn_bg "$gs" -m time --usec=604800000000 "$chip" "${line}=${raw}" \
                    && _usb_gpiod_commit_after_spawn && return 0
            fi
        fi
        # Не использовать -m exit: процесс завершается — линия часто отпускается (USB гаснет).
    fi
    if sa02m_hw_usb_gpiod_spawn_bg "$gs" "$chip" "${line}=${raw}" \
        && _usb_gpiod_commit_after_spawn; then
        return 0
    fi
    return 1
}

sa02m_hw_usb_gpiod_read() {
    local chip line gg v pf sf pid
    chip=$(sa02m_hw_usb_gpiod_chip)
    line=${SA02M_GPIO_USB_GPIOD_LINE:-}
    [[ "$line" =~ ^[0-9]+$ ]] || { echo -1; return; }
    gg=$(command -v gpioget 2>/dev/null) || { echo -1; return; }
    v=$(sa02m_hw_timeout_run sudo -n "$gg" "$chip" "$line" 2>/dev/null) \
        || v=$(sa02m_hw_timeout_run sudo "$gg" "$chip" "$line" 2>/dev/null) \
        || v=""
    v=$(printf '%s' "$v" | tr -d '\r\n\t ')
    case "$v" in
        0|1) sa02m_hw_usb_line_to_user_logical "$v" ; return 0 ;;
    esac
    if [[ "$v" =~ (^|.*[=:])([01])($|[^0-9].*) ]]; then
        sa02m_hw_usb_line_to_user_logical "${BASH_REMATCH[2]}"
        return 0
    fi
    # gpioget провалился (линия занята gpioset -m signal) — читаем из cmdline живого gpioset.
    local _gs_pid _gs_cmd _gs_val
    for _gs_pid in $(pgrep -x gpioset 2>/dev/null); do
        _gs_cmd=$(tr '\0' ' ' < "/proc/${_gs_pid}/cmdline" 2>/dev/null) || continue
        [[ "$_gs_cmd" =~ gpioset ]] || continue
        [[ "$_gs_cmd" =~ (^|[[:space:]])${chip}[[:space:]] ]] || continue
        if [[ "$_gs_cmd" =~ (^|[[:space:]])${line}=([01])([[:space:]]|$) ]]; then
            _gs_val="${BASH_REMATCH[2]}"
            case "$_gs_val" in
                0|1)
                    sa02m_hw_usb_gpiod_commit_holder "$_gs_pid" "$_gs_val" 2>/dev/null || true
                    sa02m_hw_usb_line_to_user_logical "$_gs_val"
                    return 0
                    ;;
            esac
        fi
    done
    # Фоллбэк: pid/state только если процесс из pidfile жив и совпадает с gpioset.
    pf=$(sa02m_hw_usb_gpiod_pidfile)
    sf=$(sa02m_hw_usb_gpiod_statefile)
    if [ -f "$pf" ] && [ -f "$sf" ]; then
        pid=$(tr -d ' \r\n\t' <"$pf" 2>/dev/null)
        if [[ "$pid" =~ ^[0-9]+$ ]] && [ -r "/proc/${pid}/cmdline" ]; then
            v=$(tr -d ' \r\n\t' <"$sf" 2>/dev/null)
            case "$v" in
                0|1) sa02m_hw_usb_line_to_user_logical "$v" ; return 0 ;;
            esac
        fi
    fi
    echo -1
}

sa02m_hw_i2c_channel_bit() {
    case "$1" in
        do)        printf '%s' "${SA02M_I2C_BIT_DO:-}" ;;
        beeper)    printf '%s' "${SA02M_I2C_BIT_BEEPER:-}" ;;
        alarm_led) printf '%s' "${SA02M_I2C_BIT_ALARM_LED:-}" ;;
        usb_power) printf '%s' "${SA02M_I2C_BIT_USB_POWER:-}" ;;
        *) return 1 ;;
    esac
}

sa02m_hw_i2c_channel_mask() {
    local bit
    bit=$(sa02m_hw_i2c_channel_bit "$1") || return 1
    [[ "$bit" =~ ^[0-3]$ ]] || return 1
    printf '%d' $(( 1 << bit ))
}

sa02m_hw_i2c_extra_output_mask_dec() {
    local raw=${SA02M_I2C_EXTRA_OUTPUT_MASK:-0}
    case "$raw" in
        0x[0-9a-fA-F]|0x[0-9a-fA-F][0-9a-fA-F]|[0-9]|[0-9][0-9]|1[0-5])
            printf '%d' $(( raw & 0x0F ))
            ;;
        *)
            printf '0'
            ;;
    esac
}

sa02m_hw_i2c_output_mask_dec() {
    local out=0 mask
    out=$(sa02m_hw_i2c_extra_output_mask_dec)
    for _ch in "do" beeper alarm_led usb_power; do
        mask=$(sa02m_hw_i2c_channel_mask "$_ch" 2>/dev/null) || continue
        out=$(( out | mask ))
    done
    printf '%d' $(( out & 0x0F ))
}

sa02m_hw_i2c_active_low_mask_dec() {
    local raw=${SA02M_I2C_ACTIVE_LOW_MASK:-auto}
    if [ "$raw" = "auto" ] || [ -z "$raw" ]; then
        sa02m_hw_i2c_output_mask_dec
        return 0
    fi
    printf '%d' $(( raw & 0x0F ))
}

sa02m_hw_i2c_config_mask_dec() {
    local outputs
    outputs=$(sa02m_hw_i2c_output_mask_dec)
    printf '%d' $(( 0xF0 | ((~outputs) & 0x0F) ))
}

sa02m_hw_i2c_default_output_dec() {
    local outputs active_low
    outputs=$(sa02m_hw_i2c_output_mask_dec)
    active_low=$(sa02m_hw_i2c_active_low_mask_dec)
    printf '%d' $(( 0xF0 | (0x0F & ~(outputs & ~active_low)) ))
}

sa02m_hw_channel_available() {
    if [ "$(sa02m_hw_backend)" = "disabled" ]; then
        return 1
    fi

    if [ "$1" = "usb_power" ] && sa02m_hw_usb_power_use_gpiod; then
        command -v gpioset >/dev/null 2>&1 || return 1
        return 0
    fi

    if sa02m_hw_use_i2c; then
        sa02m_hw_i2c_channel_mask "$1" >/dev/null 2>&1
        return $?
    fi

    local pin
    pin=$(sa02m_hw_gpio_pin "$1") || return 1
    [[ "$pin" =~ ^[0-9]+$ ]]
}

sa02m_hw_timeout_run() {
    if command -v timeout >/dev/null 2>&1; then
        timeout "${SA02M_I2C_TIMEOUT_SEC:-1}" "$@"
    else
        "$@"
    fi
}

sa02m_hw_i2c_owner_active() {
    local proc
    case "${SA02M_I2C_RESPECT_OWNER:-1}" in
        0|no|false|off|OFF|N) return 1 ;;
    esac

    if command -v pgrep >/dev/null 2>&1; then
        for proc in ${SA02M_I2C_OWNER_PROCS:-}; do
            [ -n "$proc" ] || continue
            if pgrep -x "$proc" >/dev/null 2>&1; then
                return 0
            fi
        done
    fi

    return 1
}

sa02m_hw_beeper_override_write() {
    local logical=$1 ttl=${SA02M_BEEPER_WEB_OVERRIDE_SEC:-7}
    local file=${SA02M_BEEPER_OVERRIDE_FILE:-/run/sa02m-hw-override/beeper.env}
    local dir tmp now exp
    [ "$logical" = "0" ] || [ "$logical" = "1" ] || return 1
    [[ "$ttl" =~ ^[0-9]+$ ]] || ttl=7
    dir=$(dirname "$file")
    mkdir -p "$dir" 2>/dev/null || return 1
    chmod 775 "$dir" 2>/dev/null || true
    now=$(date +%s 2>/dev/null) || return 1
    exp=$(( now + ttl ))
    tmp="${file}.$$"
    {
        printf 'value=%s\n' "$logical"
        printf 'expires_at=%s\n' "$exp"
    } >"$tmp" || return 1
    mv -f "$tmp" "$file" || return 1
    chmod 664 "$file" 2>/dev/null || true
    SA02M_HW_OVERRIDE_SEC="$ttl"
    return 0
}

sa02m_hw_beeper_override_start_worker() {
    local worker=${SA02M_BEEPER_OVERRIDE_WORKER:-/usr/local/sbin/sa02m-beeper-override.sh}
    [ -x "$worker" ] || return 0
    nohup "$worker" </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
    return 0
}

sa02m_hw_i2c_write_channel_web() {
    local channel=$1 logical=$2

    SA02M_HW_OVERRIDE_SEC=""

    if ! sa02m_hw_i2c_owner_active; then
        sa02m_hw_i2c_write_channel "$channel" "$logical"
        return $?
    fi

    if [ "$channel" = "beeper" ]; then
        sa02m_hw_beeper_override_write "$logical" || return "$SA02M_HW_RC_IO"
        sa02m_hw_beeper_override_start_worker
        return 0
    fi

    return "$SA02M_HW_RC_BUSY"
}

sa02m_hw_i2c_run_tool() {
    local tool=$1
    shift

    local bin rc
    bin=$(command -v "$tool" 2>/dev/null) || return "$SA02M_HW_RC_TOOL"

    sa02m_hw_timeout_run "$bin" "$@" 2>/dev/null
    rc=$?
    case "$rc" in
        0) return 0 ;;
        124|137) return "$SA02M_HW_RC_TIMEOUT" ;;
    esac

    if command -v sudo >/dev/null 2>&1; then
        sa02m_hw_timeout_run sudo -n "$bin" "$@" 2>/dev/null
        rc=$?
        case "$rc" in
            0) return 0 ;;
            124|137) return "$SA02M_HW_RC_TIMEOUT" ;;
        esac
    fi

    return "$SA02M_HW_RC_IO"
}

sa02m_hw_i2c_with_lock() {
    local lock_file="${SA02M_I2C_LOCK_FILE:-/run/lock/sa02m-pca9536.lock}"
    local lock_dir rc

    lock_dir=$(dirname "$lock_file")
    mkdir -p "$lock_dir" >/dev/null 2>&1 || true

    if ! command -v flock >/dev/null 2>&1; then
        "$@"
        return $?
    fi

    # /run/lock is sticky tmpfs: bash `exec 9>"$file"` uses O_CREAT|O_TRUNC and
    # gets EACCES on a lock owned by another uid (e.g. www-data vs root).
    # Create the file if missing, then open RDWR without O_CREAT.
    if [ ! -e "$lock_file" ]; then
        ( umask 000; : >"$lock_file" ) 2>/dev/null || touch "$lock_file" 2>/dev/null || true
        chmod 666 "$lock_file" 2>/dev/null || true
    fi
    exec 9<>"$lock_file" || return "$SA02M_HW_RC_IO"
    flock -w "${SA02M_I2C_LOCK_WAIT_SEC:-0.4}" 9 >/dev/null 2>&1 || {
        exec 9>&-
        return "$SA02M_HW_RC_BUSY"
    }

    "$@"
    rc=$?
    flock -u 9 >/dev/null 2>&1 || true
    exec 9>&-
    return $rc
}

sa02m_hw_i2c_normalize_byte() {
    local raw=${1,,}
    case "$raw" in
        0x[0-9a-f]|0x[0-9a-f][0-9a-f]) printf '%d' $(( raw & 0xFF )) ;;
        [0-9]|[0-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5]) printf '%d' "$raw" ;;
        *) return 1 ;;
    esac
}

sa02m_hw_i2c_read_reg_unlocked() {
    local reg=$1 raw
    raw=$(sa02m_hw_i2c_run_tool i2cget -y "${SA02M_I2C_EXP_BUS}" "${SA02M_I2C_EXP_ADDR}" "$reg") || return $?
    sa02m_hw_i2c_normalize_byte "$raw"
}

sa02m_hw_i2c_write_reg_unlocked() {
    local reg=$1 value=$2
    local hex
    printf -v hex '0x%02X' $(( value & 0xFF ))
    sa02m_hw_i2c_run_tool i2cset -y "${SA02M_I2C_EXP_BUS}" "${SA02M_I2C_EXP_ADDR}" "$reg" "$hex"
}

sa02m_hw_i2c_prepare_unlocked() {
    local desired_cfg desired_out cfg inv

    desired_cfg=$(sa02m_hw_i2c_config_mask_dec)
    desired_out=$(sa02m_hw_i2c_default_output_dec)

    cfg=$(sa02m_hw_i2c_read_reg_unlocked 0x03) || return $?
    inv=$(sa02m_hw_i2c_read_reg_unlocked 0x02) || return $?

    if (( inv != 0 )); then
        sa02m_hw_i2c_write_reg_unlocked 0x02 0x00 || return $?
    fi

    if (( cfg != desired_cfg )); then
        sa02m_hw_i2c_write_reg_unlocked 0x01 "$desired_out" || return $?
        sa02m_hw_i2c_write_reg_unlocked 0x03 "$desired_cfg" || return $?
    fi

    return 0
}

sa02m_hw_i2c_read_output_locked() {
    SA02M_HW_RESULT=""
    sa02m_hw_i2c_prepare_unlocked || return $?
    SA02M_HW_RESULT=$(sa02m_hw_i2c_read_reg_unlocked 0x01) || return $?
    SA02M_HW_RESULT=$(( SA02M_HW_RESULT & 0x0F ))
    return 0
}

sa02m_hw_i2c_read_output_dec() {
    SA02M_HW_RESULT=""
    if sa02m_hw_i2c_owner_active; then
        return "$SA02M_HW_RC_BUSY"
    fi
    sa02m_hw_i2c_with_lock sa02m_hw_i2c_read_output_locked
}

sa02m_hw_i2c_write_channel_locked() {
    local channel=$1 logical=$2
    local channel_mask active_low reg new_reg verify

    channel_mask=$(sa02m_hw_i2c_channel_mask "$channel") || return 1
    active_low=$(sa02m_hw_i2c_active_low_mask_dec)

    sa02m_hw_i2c_prepare_unlocked || return $?
    reg=$(sa02m_hw_i2c_read_reg_unlocked 0x01) || return $?
    new_reg=$(( reg & 0xFF ))

    if (( active_low & channel_mask )); then
        if [ "$logical" = "1" ]; then
            new_reg=$(( new_reg & ~channel_mask ))
        else
            new_reg=$(( new_reg | channel_mask ))
        fi
    else
        if [ "$logical" = "1" ]; then
            new_reg=$(( new_reg | channel_mask ))
        else
            new_reg=$(( new_reg & ~channel_mask ))
        fi
    fi

    sa02m_hw_i2c_write_reg_unlocked 0x01 "$new_reg" || return $?
    verify=$(sa02m_hw_i2c_read_reg_unlocked 0x01) || return $?
    [ $(( verify & channel_mask )) -eq $(( new_reg & channel_mask )) ] || return "$SA02M_HW_RC_IO"
    return 0
}

sa02m_hw_i2c_write_channel() {
    local channel=$1 logical=$2
    if [ "$channel" = "usb_power" ] && sa02m_hw_usb_power_use_gpiod; then
        sa02m_hw_usb_gpiod_write "$logical"
        return $?
    fi
    if sa02m_hw_i2c_owner_active; then
        return "$SA02M_HW_RC_BUSY"
    fi
    sa02m_hw_i2c_with_lock sa02m_hw_i2c_write_channel_locked "$channel" "$logical"
}

sa02m_hw_channel_logical_from_reg() {
    local channel=$1 reg=$2 mask active_low
    mask=$(sa02m_hw_i2c_channel_mask "$channel") || return 1
    active_low=$(sa02m_hw_i2c_active_low_mask_dec)

    if (( active_low & mask )); then
        if (( reg & mask )); then
            echo 0
        else
            echo 1
        fi
    else
        if (( reg & mask )); then
            echo 1
        else
            echo 0
        fi
    fi
}

sa02m_hw_gpio_state() {
    local n=$1 v
    if [ -z "$n" ] || ! [[ "$n" =~ ^[0-9]+$ ]]; then
        echo -1
        return
    fi
    if [ -r "/sys/class/gpio/gpio${n}/value" ]; then
        IFS= read -r v < "/sys/class/gpio/gpio${n}/value"
        printf '%s\n' "${v:-0}"
    else
        echo -1
    fi
}

sa02m_hw_gpio_export_out() {
    local n=$1
    if [ ! -d "/sys/class/gpio/gpio${n}" ]; then
        echo "$n" | sudo tee /sys/class/gpio/export >/dev/null 2>&1 || true
        sleep 0.08
    fi
    [ -d "/sys/class/gpio/gpio${n}" ] || return 1
    echo out | sudo tee "/sys/class/gpio/gpio${n}/direction" >/dev/null 2>&1 || return 1
    return 0
}

sa02m_hw_gpio_write_channel() {
    local channel=$1 logical=$2 pin
    if [ "$channel" = "usb_power" ] && sa02m_hw_usb_power_use_gpiod; then
        sa02m_hw_usb_gpiod_write "$logical"
        return $?
    fi
    pin=$(sa02m_hw_gpio_pin "$channel") || return 1
    [[ "$pin" =~ ^[0-9]+$ ]] || return 1
    sa02m_hw_gpio_export_out "$pin" || return 1
    echo "$logical" | sudo tee "/sys/class/gpio/gpio${pin}/value" >/dev/null 2>&1
}

sa02m_hw_usb_reset_duration_sec() {
    echo $(( (SA02M_USB_POWER_RESET_CYCLES * SA02M_USB_POWER_RESET_CYCLE_MS + 999) / 1000 ))
}

sa02m_hw_usb_power_reset_busy() {
    [ -d /run/lock/sa02m-usb-power-reset.lock ]
}

# Асинхронный сброс: VBUS off → sleep N s → on (как test_fb.cpp, без блокировки CGI).
sa02m_hw_usb_power_reset_async() {
    local sec lock=/run/lock/sa02m-usb-power-reset.lock log=/var/log/sa02m_install.log
    if sa02m_hw_usb_power_reset_busy; then
        return 2
    fi
    sec=$(sa02m_hw_usb_reset_duration_sec)
    if ! mkdir "$lock" 2>/dev/null; then
        return 2
    fi
    (
        trap 'rmdir "$lock" 2>/dev/null || true' EXIT
        if ! sa02m_hw_gpio_write_channel usb_power 0; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] usb_power reset: OFF failed" >>"$log" 2>&1
            exit 1
        fi
        sa02m_hw_metrics_cache_patch_channel usb_power 0 2>/dev/null || true
        sleep "$sec"
        if ! sa02m_hw_gpio_write_channel usb_power 1; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] usb_power reset: ON failed after ${sec}s" >>"$log" 2>&1
            sa02m_hw_metrics_cache_patch_channel usb_power 0 2>/dev/null || true
            exit 1
        fi
        sa02m_hw_metrics_cache_patch_channel usb_power 1 2>/dev/null || true
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] usb_power reset: restored ON after ${sec}s" >>"$log" 2>&1
    ) >/dev/null 2>&1 &
    disown 2>/dev/null || true
    return 0
}

# Только проверка /etc/sa02m_hw.conf — без I2C/GPIO опроса (для UI при отключённом status block).
sa02m_hw_detect_channel_pins() {
    HW_CFG=0
    PIN_DO=0
    PIN_BEEP=0
    PIN_LED=0
    PIN_USB=0

    if [ "$(sa02m_hw_backend)" = "disabled" ]; then
        if sa02m_hw_usb_power_use_gpiod && command -v gpioset >/dev/null 2>&1; then
            PIN_USB=1
            HW_CFG=1
        fi
        return 0
    fi

    if sa02m_hw_use_i2c; then
        sa02m_hw_channel_available do && PIN_DO=1
        sa02m_hw_channel_available beeper && PIN_BEEP=1
        sa02m_hw_channel_available alarm_led && PIN_LED=1
        sa02m_hw_channel_available usb_power && PIN_USB=1
        (( PIN_DO || PIN_BEEP || PIN_LED || PIN_USB )) && HW_CFG=1
        return 0
    fi

    [[ "${SA02M_GPIO_DO:-}" =~ ^[0-9]+$ ]] && PIN_DO=1 && HW_CFG=1
    [[ "${SA02M_GPIO_BEEPER:-}" =~ ^[0-9]+$ ]] && PIN_BEEP=1 && HW_CFG=1
    [[ "${SA02M_GPIO_ALARM_LED:-}" =~ ^[0-9]+$ ]] && PIN_LED=1 && HW_CFG=1
    if sa02m_hw_usb_power_use_gpiod && command -v gpioset >/dev/null 2>&1; then
        PIN_USB=1
        HW_CFG=1
    elif [[ "${SA02M_GPIO_USB_POWER:-}" =~ ^[0-9]+$ ]]; then
        PIN_USB=1
        HW_CFG=1
    fi
}

# Снимок метрик для UI при SA02M_STATUS_ENABLE_HARDWARE=0 (без I2C на каждый main).
SA02M_HW_METRICS_CACHE="${SA02M_HW_METRICS_CACHE:-/tmp/sa02m_status_cache/hw_metrics.snapshot}"
SA02M_HW_METRICS_CACHE_TTL="${SA02M_HW_METRICS_CACHE_TTL:-15}"

sa02m_hw_metrics_cache_dir() {
    dirname "$SA02M_HW_METRICS_CACHE"
}

sa02m_hw_metrics_cache_ensure_dir() {
    mkdir -p "$(sa02m_hw_metrics_cache_dir)" 2>/dev/null || true
}

sa02m_hw_metrics_cache_fresh() {
    local now mtime ttl
    ttl=${SA02M_HW_METRICS_CACHE_TTL:-15}
    [ -f "$SA02M_HW_METRICS_CACHE" ] || return 1
    now=$(date +%s 2>/dev/null || echo 0)
    mtime=$(stat -c %Y "$SA02M_HW_METRICS_CACHE" 2>/dev/null || echo 0)
    (( mtime > now )) && return 1
    (( now - mtime < ttl ))
}

sa02m_hw_metrics_cache_save() {
    sa02m_hw_metrics_cache_ensure_dir
    local tmp="${SA02M_HW_METRICS_CACHE}.$$"
    cat > "$tmp" <<EOF
HW_DO=${HW_DO:--1}
HW_BEEP=${HW_BEEP:--1}
HW_LED=${HW_LED:--1}
HW_USB=${HW_USB:--1}
HW_I2C_BUSY=${HW_I2C_BUSY:-0}
HW_I2C_EXP_ABS=${HW_I2C_EXP_ABS:-0}
HW_BACKEND=${HW_BACKEND:-unknown}
EOF
    mv "$tmp" "$SA02M_HW_METRICS_CACHE" 2>/dev/null \
        || cp "$tmp" "$SA02M_HW_METRICS_CACHE" 2>/dev/null \
        || return 1
    rm -f "$tmp"
    chmod 644 "$SA02M_HW_METRICS_CACHE" 2>/dev/null || true
}

sa02m_hw_metrics_cache_load() {
    local snap=$1
    snap=${snap:-$SA02M_HW_METRICS_CACHE}
    [ -f "$snap" ] || return 1
    # shellcheck disable=SC1090
    . "$snap" 2>/dev/null || return 1
    return 0
}

# Опрос I2C/GPIO с TTL-кэшем; при status-block hardware=0 — единственный путь к живым значениям.
sa02m_hw_metrics_cache_refresh() {
    HW_BACKEND=$(sa02m_hw_backend)
    sa02m_hw_detect_channel_pins

    if sa02m_hw_metrics_cache_fresh && sa02m_hw_metrics_cache_load; then
        return 0
    fi

    sa02m_hw_metrics_cache_ensure_dir
    local lock_file="${SA02M_HW_METRICS_CACHE}.lock"
    if command -v flock >/dev/null 2>&1; then
        exec 7>"$lock_file" 2>/dev/null || {
            sa02m_hw_collect_metrics
            sa02m_hw_metrics_cache_save
            return 0
        }
        if ! flock -w 2 7 >/dev/null 2>&1; then
            sa02m_hw_metrics_cache_load && return 0
            exec 7>&-
            sa02m_hw_collect_metrics
            sa02m_hw_metrics_cache_save
            return 0
        fi
        if sa02m_hw_metrics_cache_fresh && sa02m_hw_metrics_cache_load; then
            flock -u 7 >/dev/null 2>&1 || true
            exec 7>&-
            return 0
        fi
        sa02m_hw_collect_metrics
        sa02m_hw_metrics_cache_save
        flock -u 7 >/dev/null 2>&1 || true
        exec 7>&-
        return 0
    fi

    sa02m_hw_collect_metrics
    sa02m_hw_metrics_cache_save
}

sa02m_hw_metrics_cache_patch_channel() {
    local channel=$1 val=$2
    [ "$val" = "0" ] || [ "$val" = "1" ] || return 1
    sa02m_hw_metrics_cache_ensure_dir
    HW_BACKEND=$(sa02m_hw_backend)
    sa02m_hw_detect_channel_pins
    if ! sa02m_hw_metrics_cache_load 2>/dev/null; then
        sa02m_hw_collect_metrics
    fi
    case "$channel" in
        do) HW_DO=$val ;;
        beeper) HW_BEEP=$val ;;
        alarm_led) HW_LED=$val ;;
        usb_power) HW_USB=$val ;;
        *) return 1 ;;
    esac
    HW_I2C_BUSY=0
    sa02m_hw_metrics_cache_save
}

sa02m_hw_collect_metrics() {
    HW_BACKEND=$(sa02m_hw_backend)
    HW_I2C_EXP_ABS=0
    HW_I2C_BUSY=0

    HW_DO=-1
    HW_BEEP=-1
    HW_LED=-1
    HW_USB=-1

    sa02m_hw_detect_channel_pins

    if [ "$HW_BACKEND" = "disabled" ]; then
        # USB power через gpiod не зависит от backend — читаем в любом случае.
        if sa02m_hw_usb_power_use_gpiod; then
            PIN_USB=1
            HW_CFG=1
            HW_USB=$(sa02m_hw_usb_gpiod_read)
        fi
        return 0
    fi

    if sa02m_hw_use_i2c; then
        local reg rc

        (( HW_CFG )) || return 0

        sa02m_hw_i2c_read_output_dec
        rc=$?
        case "$rc" in
            0)
                reg=$SA02M_HW_RESULT
                (( PIN_DO )) && HW_DO=$(sa02m_hw_channel_logical_from_reg do "$reg")
                (( PIN_BEEP )) && HW_BEEP=$(sa02m_hw_channel_logical_from_reg beeper "$reg")
                (( PIN_LED )) && HW_LED=$(sa02m_hw_channel_logical_from_reg alarm_led "$reg")
                if (( PIN_USB )) && ! sa02m_hw_usb_power_use_gpiod; then
                    HW_USB=$(sa02m_hw_channel_logical_from_reg usb_power "$reg")
                fi
                ;;
            "$SA02M_HW_RC_BUSY"|"$SA02M_HW_RC_TIMEOUT")
                HW_I2C_BUSY=1
                ;;
            *)
                HW_I2C_EXP_ABS=1
                ;;
        esac
        if (( PIN_USB )) && sa02m_hw_usb_power_use_gpiod; then
            HW_USB=$(sa02m_hw_usb_gpiod_read)
        fi
        return 0
    fi

    (( HW_CFG )) || return 0

    HW_DO=$(sa02m_hw_gpio_state "${SA02M_GPIO_DO:-}")
    HW_BEEP=$(sa02m_hw_gpio_state "${SA02M_GPIO_BEEPER:-}")
    HW_LED=$(sa02m_hw_gpio_state "${SA02M_GPIO_ALARM_LED:-}")
    if sa02m_hw_usb_power_use_gpiod; then
        HW_USB=$(sa02m_hw_usb_gpiod_read)
    else
        HW_USB=$(sa02m_hw_gpio_state "${SA02M_GPIO_USB_POWER:-}")
        case "$HW_USB" in
            0|1) HW_USB=$(sa02m_hw_usb_line_to_user_logical "$HW_USB") ;;
        esac
    fi
}
