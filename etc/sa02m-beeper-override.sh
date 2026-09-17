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

# The override file is DATA and is parsed as data — never sourced, never
# eval'd, no expansion of anything it contains. Its directory is
# `0775 www-data www-data` (the tmpfiles.d entry in scripts/03-webserver.sh),
# so www-data can rename any content over it; since 1.0.6.43 the root
# sa02m-modbus-mqtt daemon also spawns this worker, which made a `.` of that
# file arbitrary code execution as root, ~35 times over one 7 s TTL. Reading
# it line by line against a closed grammar is what removes that, so nothing
# here may grow back into a shell evaluation of the file's bytes.
#
# The grammar is exactly what both producers write (lib_hw.sh
# sa02m_hw_beeper_override_write and sa02m_telemetry.py
# _beeper_override_write): `value=<0|1>` and `expires_at=<digits>`, in any
# order, plus blank lines. ANY other line refuses the whole file rather than
# being skipped — a file carrying a line neither producer can emit is not a
# command this worker should half-honour. Gate: quality row
# beeper-override-no-exec, which runs this worker over planted payloads.
#
# Do NOT make a rejected line observable — no log, no echo, no journal entry.
# www-data can point this path at any file root can read (`-f` follows a
# symlink), so the parser is deliberately a mute yes/no: it reads such a file,
# matches nothing and refuses. A "line 3 is invalid: <text>" message would turn
# that same reach into a file-disclosure oracle running as root.
read_override() {
    value=
    expires_at=
    line=
    lines=0
    # -r as well as -f: the read below is a redirect on a compound command, and
    # under `set -e` a failed redirect ends the script rather than this
    # function. Same outcome either way (no beep, worker stops), but the
    # refusal should be this function's to make.
    [ -f "$OVERRIDE_FILE" ] && [ -r "$OVERRIDE_FILE" ] || return 1
    # `|| [ -n "$line" ]` keeps the last line when the file ends without a
    # newline; the loop is redirected (not piped), so the assignments below
    # survive into the caller — a pipeline would parse in a subshell and this
    # function would always report "no override".
    while IFS= read -r line || [ -n "$line" ]; do
        lines=$((lines + 1))
        # A bound on a file an unprivileged process can rewrite at will: two
        # lines are the contract, sixteen is slack, a planted megabyte is not
        # something a 0.2 s loop should read.
        [ "$lines" -le 16 ] || return 1
        case "$line" in
            '') ;;
            value=0|value=1) value=${line#value=} ;;
            expires_at=*)
                exp=${line#expires_at=}
                case "$exp" in ''|*[!0-9]*) return 1 ;; esac
                expires_at=$exp
                ;;
            *) return 1 ;;                     # any other line: refuse the file
        esac
    done < "$OVERRIDE_FILE"
    # Both keys must have been seen: a file with only one of them is a torn
    # write, and honouring half of it is how a beep with no deadline latches.
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
