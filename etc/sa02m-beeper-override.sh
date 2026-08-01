#!/bin/sh
set -eu

CONF=${HW_CONF:-/etc/sa02m_hw.conf}
[ -f "$CONF" ] && . "$CONF" 2>/dev/null || true

OVERRIDE_FILE=${SA02M_BEEPER_OVERRIDE_FILE:-/run/sa02m-hw-override/beeper.env}
LOCK_FILE=${SA02M_I2C_LOCK_FILE:-/run/lock/sa02m-pca9536.lock}
BUS=${SA02M_I2C_EXP_BUS:-2}
ADDR=${SA02M_I2C_EXP_ADDR:-0x41}
BIT=${SA02M_I2C_BIT_BEEPER:-2}
INTERVAL=${SA02M_BEEPER_OVERRIDE_INTERVAL_SEC:-0.2}

I2CGET=/usr/sbin/i2cget
I2CSET=/usr/sbin/i2cset
[ -x "$I2CGET" ] || I2CGET=/usr/bin/i2cget
[ -x "$I2CSET" ] || I2CSET=/usr/bin/i2cset

i2c_get() {
    timeout 1 "$I2CGET" -y "$BUS" "$ADDR" "$1" 2>/dev/null \
        || timeout 1 sudo -n "$I2CGET" -y "$BUS" "$ADDR" "$1" 2>/dev/null
}

i2c_set() {
    timeout 1 "$I2CSET" -y "$BUS" "$ADDR" "$1" "$2" >/dev/null 2>&1 \
        || timeout 1 sudo -n "$I2CSET" -y "$BUS" "$ADDR" "$1" "$2" >/dev/null 2>&1
}

read_override() {
    value=
    expires_at=
    [ -f "$OVERRIDE_FILE" ] || return 1
    # shellcheck disable=SC1090
    . "$OVERRIDE_FILE" 2>/dev/null || return 1
    case "$value" in 0|1) ;; *) return 1 ;; esac
    case "$expires_at" in ''|*[!0-9]*) return 1 ;; esac
    [ "$(date +%s)" -lt "$expires_at" ]
}

apply_once() {
    reg=$(i2c_get 0x01) || return 1
    case "$reg" in 0x*) ;; *) return 1 ;; esac
    mask=$((1 << BIT))
    cur=$((reg & 0xFF))
    # PCA9536 outputs are active-low: value=1 means clear bit, value=0 means set bit.
    if [ "$value" = "1" ]; then
        next=$((cur & ~mask))
    else
        next=$((cur | mask))
    fi
    i2c_set 0x01 "$(printf '0x%02X' "$next")"
}

mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
touch "$LOCK_FILE" 2>/dev/null || true
chmod 666 "$LOCK_FILE" 2>/dev/null || true

while read_override; do
    (
        flock -n 9 || exit 0
        apply_once || true
    ) 9<>"$LOCK_FILE"
    sleep "$INTERVAL"
done
