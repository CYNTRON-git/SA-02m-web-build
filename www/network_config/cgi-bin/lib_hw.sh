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

SA02M_I2C_EXP_BUS="${SA02M_I2C_EXP_BUS:-2}"
SA02M_I2C_EXP_ADDR="${SA02M_I2C_EXP_ADDR:-0x41}"
SA02M_I2C_LOCK_FILE="${SA02M_I2C_LOCK_FILE:-/run/lock/sa02m-pca9536.lock}"
SA02M_I2C_LOCK_WAIT_SEC="${SA02M_I2C_LOCK_WAIT_SEC:-1}"
SA02M_I2C_TIMEOUT_SEC="${SA02M_I2C_TIMEOUT_SEC:-1}"
SA02M_I2C_ACTIVE_LOW_MASK="${SA02M_I2C_ACTIVE_LOW_MASK:-auto}"
SA02M_I2C_OWNER_UNITS="${SA02M_I2C_OWNER_UNITS:-mplc.service mplc4.service klogic.service klogicd.service}"
SA02M_I2C_OWNER_PROCS="${SA02M_I2C_OWNER_PROCS:-mplc mplc4 klogic klogicd}"
SA02M_I2C_RESPECT_OWNER="${SA02M_I2C_RESPECT_OWNER:-1}"
SA02M_I2C_BIT_DO="${SA02M_I2C_BIT_DO:-1}"
SA02M_I2C_BIT_BEEPER="${SA02M_I2C_BIT_BEEPER:-2}"
SA02M_I2C_BIT_ALARM_LED="${SA02M_I2C_BIT_ALARM_LED:-0}"
SA02M_I2C_BIT_USB_POWER="${SA02M_I2C_BIT_USB_POWER:-}"

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

sa02m_hw_usb_gpiod_stop_holder() {
    local pf pid
    pf=$(sa02m_hw_usb_gpiod_pidfile)
    [ -f "$pf" ] || return 0
    pid=$(tr -d ' \r\n\t' <"$pf" 2>/dev/null)
    [[ "$pid" =~ ^[0-9]+$ ]] || { rm -f "$pf"; return 0; }
    sudo -n kill -TERM "$pid" 2>/dev/null || sudo kill -TERM "$pid" 2>/dev/null || true
    sleep 0.08
    sudo -n kill -KILL "$pid" 2>/dev/null || sudo kill -KILL "$pid" 2>/dev/null || true
    rm -f "$pf"
}

sa02m_hw_usb_gpiod_write() {
    local logical=$1 chip line gs help pf sf raw
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

    # Вспомогательная: записать PID и state-файл, вернуть 0.
    _usb_gpiod_save_and_ok() {
        echo $1 >"$pf" 2>/dev/null && chmod 644 "$pf" 2>/dev/null || true
        printf '%s' "$raw" >"$sf" 2>/dev/null && chmod 644 "$sf" 2>/dev/null || true
        return 0
    }

    if echo "$help" | grep -q -- '-m'; then
        # Предпочитаем -m signal: держит линию до SIGTERM/SIGINT, не падает от EOF stdin.
        # -m wait + /dev/null = немедленный выход, линия отпускается, питание гаснет.
        if echo "$help" | grep -qi 'signal'; then
            if sudo -n "$gs" -m signal "$chip" "${line}=${raw}" </dev/null >/dev/null 2>&1 & then
                _usb_gpiod_save_and_ok $!; return 0
            fi
            if sudo "$gs" -m signal "$chip" "${line}=${raw}" </dev/null >/dev/null 2>&1 & then
                _usb_gpiod_save_and_ok $!; return 0
            fi
        fi
        if echo "$help" | grep -qi 'wait'; then
            if sudo -n "$gs" -m wait "$chip" "${line}=${raw}" </dev/null >/dev/null 2>&1 & then
                _usb_gpiod_save_and_ok $!; return 0
            fi
            if sudo "$gs" -m wait "$chip" "${line}=${raw}" </dev/null >/dev/null 2>&1 & then
                _usb_gpiod_save_and_ok $!; return 0
            fi
        fi
        if echo "$help" | grep -qi 'time'; then
            if echo "$help" | grep -qE '\-\-sec|[[:space:]]-s[[:space:]]'; then
                if sudo -n "$gs" -m time -s 604800 "$chip" "${line}=${raw}" </dev/null >/dev/null 2>&1 & then
                    _usb_gpiod_save_and_ok $!; return 0
                fi
                if sudo "$gs" -m time -s 604800 "$chip" "${line}=${raw}" </dev/null >/dev/null 2>&1 & then
                    _usb_gpiod_save_and_ok $!; return 0
                fi
            fi
            if echo "$help" | grep -qi usec; then
                if sudo -n "$gs" -m time --usec=604800000000 "$chip" "${line}=${raw}" </dev/null >/dev/null 2>&1 & then
                    _usb_gpiod_save_and_ok $!; return 0
                fi
                if sudo "$gs" -m time --usec=604800000000 "$chip" "${line}=${raw}" </dev/null >/dev/null 2>&1 & then
                    _usb_gpiod_save_and_ok $!; return 0
                fi
            fi
        fi
        # Не использовать -m exit: процесс завершается — линия часто отпускается (USB гаснет).
    fi
    if sa02m_hw_timeout_run sudo -n "$gs" "$chip" "${line}=${raw}" 2>/dev/null; then
        printf '%s' "$raw" >"$sf" 2>/dev/null || true; return 0
    fi
    if sa02m_hw_timeout_run sudo "$gs" "$chip" "${line}=${raw}" 2>/dev/null; then
        printf '%s' "$raw" >"$sf" 2>/dev/null || true; return 0
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
    # gpioget провалился (линия занята gpioset -m signal).
    # Читаем последнее записанное raw-значение из state-файла.
    pf=$(sa02m_hw_usb_gpiod_pidfile)
    sf=$(sa02m_hw_usb_gpiod_statefile)
    if [ -f "$pf" ] && [ -f "$sf" ]; then
        pid=$(tr -d ' \r\n\t' <"$pf" 2>/dev/null)
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
            v=$(tr -d ' \r\n\t' <"$sf" 2>/dev/null)
            case "$v" in
                0|1) sa02m_hw_usb_line_to_user_logical "$v" ; return 0 ;;
            esac
        fi
    fi
    # PID мёртв, но gpioget тоже упал — значит линия всё ещё занята:
    # gpioset был переусыновлён PID 1 (sudo-родитель умер, gpioset жив).
    # Если state-файл существует — доверяем последнему записанному значению.
    if [ -f "$sf" ]; then
        v=$(tr -d ' \r\n\t' <"$sf" 2>/dev/null)
        case "$v" in
            0|1) sa02m_hw_usb_line_to_user_logical "$v" ; return 0 ;;
        esac
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

sa02m_hw_i2c_output_mask_dec() {
    local out=0 mask
    for _ch in do beeper alarm_led usb_power; do
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
    printf '%d' $(( (~outputs) & 0x0F ))
}

sa02m_hw_i2c_default_output_dec() {
    local outputs active_low
    outputs=$(sa02m_hw_i2c_output_mask_dec)
    active_low=$(sa02m_hw_i2c_active_low_mask_dec)
    printf '%d' $(( 0x0F & ~(outputs & ~active_low) ))
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

    exec 9>"$lock_file" || return "$SA02M_HW_RC_IO"
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
    new_reg=$(( reg & 0x0F ))

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

sa02m_hw_collect_metrics() {
    HW_BACKEND=$(sa02m_hw_backend)
    HW_CFG=0
    HW_I2C_EXP_ABS=0
    HW_I2C_BUSY=0

    HW_DO=-1
    HW_BEEP=-1
    HW_LED=-1
    HW_USB=-1

    PIN_DO=0
    PIN_BEEP=0
    PIN_LED=0
    PIN_USB=0

    if [ "$HW_BACKEND" = "disabled" ]; then
        return 0
    fi

    if sa02m_hw_use_i2c; then
        local reg rc

        sa02m_hw_channel_available do && PIN_DO=1
        sa02m_hw_channel_available beeper && PIN_BEEP=1
        sa02m_hw_channel_available alarm_led && PIN_LED=1
        sa02m_hw_channel_available usb_power && PIN_USB=1

        (( PIN_DO || PIN_BEEP || PIN_LED || PIN_USB )) && HW_CFG=1
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

    [[ "${SA02M_GPIO_DO:-}" =~ ^[0-9]+$ ]] && PIN_DO=1 && HW_CFG=1
    [[ "${SA02M_GPIO_BEEPER:-}" =~ ^[0-9]+$ ]] && PIN_BEEP=1 && HW_CFG=1
    [[ "${SA02M_GPIO_ALARM_LED:-}" =~ ^[0-9]+$ ]] && PIN_LED=1 && HW_CFG=1
    if sa02m_hw_usb_power_use_gpiod; then
        PIN_USB=1
        HW_CFG=1
    elif [[ "${SA02M_GPIO_USB_POWER:-}" =~ ^[0-9]+$ ]]; then
        PIN_USB=1
        HW_CFG=1
    fi

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
