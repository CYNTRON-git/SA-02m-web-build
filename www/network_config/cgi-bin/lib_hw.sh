#!/bin/bash

HW_CONF="${HW_CONF:-/etc/sa02m_hw.conf}"

SA02M_HW_BACKEND="${SA02M_HW_BACKEND:-auto}"

SA02M_GPIO_DO="${SA02M_GPIO_DO:-}"
SA02M_GPIO_BEEPER="${SA02M_GPIO_BEEPER:-}"
SA02M_GPIO_ALARM_LED="${SA02M_GPIO_ALARM_LED:-}"
SA02M_GPIO_USB_POWER="${SA02M_GPIO_USB_POWER:-}"

SA02M_I2C_EXP_BUS="${SA02M_I2C_EXP_BUS:-2}"
SA02M_I2C_EXP_ADDR="${SA02M_I2C_EXP_ADDR:-0x41}"
SA02M_I2C_LOCK_FILE="${SA02M_I2C_LOCK_FILE:-/run/lock/sa02m-pca9536.lock}"
SA02M_I2C_LOCK_WAIT_SEC="${SA02M_I2C_LOCK_WAIT_SEC:-0.4}"
SA02M_I2C_TIMEOUT_SEC="${SA02M_I2C_TIMEOUT_SEC:-1}"
SA02M_I2C_ACTIVE_LOW_MASK="${SA02M_I2C_ACTIVE_LOW_MASK:-auto}"
SA02M_I2C_OWNER_UNITS="${SA02M_I2C_OWNER_UNITS:-mplc.service mplc4.service}"
SA02M_I2C_OWNER_PROCS="${SA02M_I2C_OWNER_PROCS:-mplc mplc4}"
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
    local unit proc
    case "${SA02M_I2C_RESPECT_OWNER:-1}" in
        0|no|false|off|OFF|N) return 1 ;;
    esac

    if command -v systemctl >/dev/null 2>&1; then
        for unit in ${SA02M_I2C_OWNER_UNITS:-}; do
            [ -n "$unit" ] || continue
            if systemctl is-active --quiet "$unit" 2>/dev/null; then
                return 0
            fi
        done
    fi

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
                (( PIN_USB )) && HW_USB=$(sa02m_hw_channel_logical_from_reg usb_power "$reg")
                ;;
            "$SA02M_HW_RC_BUSY"|"$SA02M_HW_RC_TIMEOUT")
                HW_I2C_BUSY=1
                ;;
            *)
                HW_I2C_EXP_ABS=1
                ;;
        esac
        return 0
    fi

    [[ "${SA02M_GPIO_DO:-}" =~ ^[0-9]+$ ]] && PIN_DO=1 && HW_CFG=1
    [[ "${SA02M_GPIO_BEEPER:-}" =~ ^[0-9]+$ ]] && PIN_BEEP=1 && HW_CFG=1
    [[ "${SA02M_GPIO_ALARM_LED:-}" =~ ^[0-9]+$ ]] && PIN_LED=1 && HW_CFG=1
    [[ "${SA02M_GPIO_USB_POWER:-}" =~ ^[0-9]+$ ]] && PIN_USB=1 && HW_CFG=1

    HW_DO=$(sa02m_hw_gpio_state "${SA02M_GPIO_DO:-}")
    HW_BEEP=$(sa02m_hw_gpio_state "${SA02M_GPIO_BEEPER:-}")
    HW_LED=$(sa02m_hw_gpio_state "${SA02M_GPIO_ALARM_LED:-}")
    HW_USB=$(sa02m_hw_gpio_state "${SA02M_GPIO_USB_POWER:-}")
}
