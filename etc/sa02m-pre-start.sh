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
  # Дать udev время создать /dev/rtc*
  sleep 0.3
  I=0
  while [ "$I" -lt 5 ]; do
    if [ -n "$HWC" ] && [ -c /dev/rtc1 ] && "$HWC" -s -f /dev/rtc1 >/dev/null 2>&1; then
      HCTOSYS_DEVICE=rtc1
      I=5
    else
      I=$((I + 1))
      sleep 1
    fi
  done
  if [ -c "/dev/${HCTOSYS_DEVICE}" ]; then
    rm -f /dev/rtc
    ln -sf "/dev/${HCTOSYS_DEVICE}" /dev/rtc 2>/dev/null || logp "could not symlink /dev/rtc → ${HCTOSYS_DEVICE}"
  fi
fi

# Системное время → аппаратные часы: предпочтительно внешний rtc1 (PCF8563/DS3231 на СА-02м)
if [ -n "$HWC" ]; then
  if [ -c /dev/rtc1 ]; then
    "$HWC" -w -f /dev/rtc1 >/dev/null 2>&1 || true
  else
    "$HWC" -w >/dev/null 2>&1 || true
  fi
fi

# ── PCA9536 — как в MasterPLC: PCA9536-driver-for-MasterPLC (mplc_fb_ca02m /
# simple_test_protocol): шина 2, 0x41, reg 0x03 = направление, 0x01 = выходы;
# активный низкий уровень: «вкл» = сброс бита в маске, стартовая маска 0xFF.
PCA9536_ADDR=0x41
PCA9536_REG_CFG=0x03
PCA9536_REG_OUT=0x01
# Все линии 0..3 — выходы (драйвер: i2cset … 0x03 0x00).
PCA9536_MASK_ALL_OFF=0xFF
# Бипер — bit2: вкл = ~ (1<<2) & 0xFF = 0xFB
PCA9536_MASK_BUZZ_ON=0xFB

if command -v i2cset >/dev/null 2>&1 && command -v i2cget >/dev/null 2>&1; then
  if timeout 2 i2cget -y 2 "$PCA9536_ADDR" >/dev/null 2>&1; then
    i2cset -y 2 "$PCA9536_ADDR" "$PCA9536_REG_CFG" 0x00 2>/dev/null || true
    i2cset -y 2 "$PCA9536_ADDR" "$PCA9536_REG_OUT" "$PCA9536_MASK_BUZZ_ON" 2>/dev/null || true
    sleep 0.1
    i2cset -y 2 "$PCA9536_ADDR" "$PCA9536_REG_OUT" "$PCA9536_MASK_ALL_OFF" 2>/dev/null || true
  else
    logp "PCA9536 (0x41 on i2c-2) not reachable, skip"
  fi
else
  logp "i2c-tools missing, skip PCA9536"
fi

exit 0
