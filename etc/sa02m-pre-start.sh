#!/bin/bash
# Ранний запуск (PRE-START): питание USB, при необходимости DS3231 на i2c-1 → rtc1, hwclock, PCA9536 + бипер.
# Не использует set -e: сбой опциональных шагов не должен ломать загрузку.
set -u

logp() {
  logger -t sa02m-pre-start -- "$*" 2>/dev/null || true
}

# Держим VBUS как в веб-backend; pid-файл совпадает с lib_hw.sh для последующих hw_set.
sa02m_boot_usb_vbus_on() {
  local chip=0 line=268 raw=1 gs help pf sf pid
  gs=$(command -v gpioset 2>/dev/null) || { logp "gpioset missing"; return; }
  pf="/tmp/sa02m-gpioset-usb-power-c${chip}-l${line}.pid"
  sf="/tmp/sa02m-gpioset-usb-power-c${chip}-l${line}.state"
  if [ -f "$pf" ]; then
    pid=$(tr -d ' \r\n\t' <"$pf" 2>/dev/null)
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
      kill -TERM "$pid" 2>/dev/null || true
      sleep 0.06
      kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$pf"
  fi
  help=$("$gs" -h 2>&1 || true)
  # Предпочитаем -m signal: процесс держит линию до SIGTERM/SIGINT и не завершается при EOF stdin.
  # -m wait с /dev/null читает EOF немедленно и выходит — линия отпускается, питание гаснет.
  _save_and_return() {
    echo $1 >"$pf"
    chmod 644 "$pf" 2>/dev/null || true
    printf '%s' "$raw" >"$sf"
    chmod 644 "$sf" 2>/dev/null || true
    disown 2>/dev/null || true
  }
  if echo "$help" | grep -q -- '-m' && echo "$help" | grep -qi signal; then
    if "$gs" -m signal "$chip" "${line}=${raw}" </dev/null >/dev/null 2>&1 & then
      _save_and_return $!; return
    fi
  fi
  if echo "$help" | grep -q -- '-m' && echo "$help" | grep -qi time && echo "$help" | grep -qE '\-\-sec|[[:space:]]-s[[:space:]]'; then
    if "$gs" -m time -s 604800 "$chip" "${line}=${raw}" </dev/null >/dev/null 2>&1 & then
      _save_and_return $!; return
    fi
  fi
  if echo "$help" | grep -q -- '-m' && echo "$help" | grep -qi wait; then
    if "$gs" -m wait "$chip" "${line}=${raw}" </dev/null >/dev/null 2>&1 & then
      _save_and_return $!; return
    fi
  fi
  if "$gs" "$chip" "${line}=${raw}" 2>/dev/null; then
    printf '%s' "$raw" >"$sf" 2>/dev/null || true; return
  fi
  logp "gpioset legacy usb power failed"
}

sa02m_boot_usb_vbus_on

# ── RTC: если нет rtc1 — пробуем объявить DS3231 на шине i2c-1, адрес 0x68 ───
HCTOSYS_DEVICE=rtc0
HWC=/sbin/hwclock
[ -x "$HWC" ] || HWC=/usr/sbin/hwclock
[ -x "$HWC" ] || HWC=$(command -v hwclock 2>/dev/null || true)
[ -n "${HWC:-}" ] || HWC=""

if [ ! -e /dev/rtc1 ] && [ -w /sys/class/i2c-adapter/i2c-1/new_device ]; then
  if [ ! -d /sys/bus/i2c/devices/1-0068 ]; then
    if ! echo ds3231 0x68 > /sys/class/i2c-adapter/i2c-1/new_device 2>/dev/null; then
      logp "ds3231 new_device failed (no chip or already registered)"
    fi
  fi
  # Ждём появления /dev/rtc1 не более 1.5 с (3 × 0.5). Без чипа дальше ждать
  # бессмысленно — раньше было 5 × 1 с = +4 с к каждой загрузке.
  for try in 1 2 3; do
    [ -c /dev/rtc1 ] && break
    sleep 0.5
  done
fi
# Если rtc1 существует (после probe выше или с прошлой загрузки) — читаем
# время из него и кладём в системные часы. Делаем это всегда, не только при
# первой регистрации: иначе rtc1 с правильным временем не использовался бы.
if [ -n "$HWC" ] && [ -c /dev/rtc1 ]; then
  if "$HWC" -s -f /dev/rtc1 >/dev/null 2>&1; then
    HCTOSYS_DEVICE=rtc1
  fi
fi
if [ -c "/dev/${HCTOSYS_DEVICE}" ]; then
  rm -f /dev/rtc
  ln -sf "/dev/${HCTOSYS_DEVICE}" /dev/rtc 2>/dev/null || logp "could not symlink /dev/rtc → ${HCTOSYS_DEVICE}"
fi

# Системное время → аппаратные часы: предпочтительно внешний rtc1 (PCF8563/DS3231 на СА-02м)
if [ -n "$HWC" ]; then
  if [ -c /dev/rtc1 ]; then
    "$HWC" -w -f /dev/rtc1 >/dev/null 2>&1 || true
  else
    "$HWC" -w >/dev/null 2>&1 || true
  fi
fi

# ── PCA9536 beeper (i2c-2, 0x41) ──────────────────────────────────────────────
# ВАЖНО: когда верхняя плата (expansion board) подключена, её чип удерживает
# SDA в низком состоянии при включении питания. Попытка i2cget без
# предварительного восстановления шины блокирует mv64xxx I2C-контроллер
# (kernel: "i2c i2c-2: mv64xxx: I2C bus locked"), который затем повторяет
# попытки каждые 5 с, сжигая CPU (load >3) → hardware watchdog не успевает
# кормиться → hard reset каждые ~60 с.
#
# Восстановление шины требует GPIO bit-bang на PB20 (SCL, GPIO 52) и
# PB21 (SDA, GPIO 53). До реализации recovery — i2c-2 unbind перед
# обращением: если после rebind шина снова блокируется, доступ пропускается.
PCA9536_ADDR=0x41
PCA9536_REG_CFG=0x03
PCA9536_REG_OUT=0x01
PCA9536_MASK_ALL_OFF=0xFF
PCA9536_MASK_BUZZ_ON=0xFB
I2C2_DEV="1c2b800.i2c"
I2C2_DRIVER_PATH="/sys/bus/platform/drivers/mv64xxx_i2c"

_i2c2_is_bound() {
  [ -d "/sys/bus/i2c/devices/i2c-2" ]
}

_i2c2_unbind() {
  [ -w "${I2C2_DRIVER_PATH}/unbind" ] && \
    echo "$I2C2_DEV" > "${I2C2_DRIVER_PATH}/unbind" 2>/dev/null || true
}

_i2c2_bind() {
  [ -w "${I2C2_DRIVER_PATH}/bind" ] && \
    echo "$I2C2_DEV" > "${I2C2_DRIVER_PATH}/bind" 2>/dev/null || true
  sleep 0.2
}

if command -v i2cset >/dev/null 2>&1 && command -v i2cget >/dev/null 2>&1; then
  # Unbind i2c-2, then rebind — clears any pending lock from previous state
  _i2c2_unbind
  sleep 0.1
  _i2c2_bind

  # Check if bus is healthy: probe with very short timeout
  # If the bus is still locked after rebind (expansion board holds SDA low),
  # the probe will fail/hang → we unbind permanently and skip PCA9536.
  if timeout 0.3 i2cget -y 2 "$PCA9536_ADDR" >/dev/null 2>&1; then
    i2cset -y 2 "$PCA9536_ADDR" "$PCA9536_REG_CFG" 0x00 2>/dev/null || true
    i2cset -y 2 "$PCA9536_ADDR" "$PCA9536_REG_OUT" "$PCA9536_MASK_BUZZ_ON" 2>/dev/null || true
    sleep 0.1
    i2cset -y 2 "$PCA9536_ADDR" "$PCA9536_REG_OUT" "$PCA9536_MASK_ALL_OFF" 2>/dev/null || true
    logp "PCA9536 beep OK"
  else
    # Bus locked or chip absent — unbind i2c-2 to prevent CPU-burning lockup retries
    logp "PCA9536 (0x41 on i2c-2) not reachable — unbinding i2c-2 to prevent lockup"
    _i2c2_unbind
  fi
else
  logp "i2c-tools missing, skip PCA9536"
fi

exit 0
