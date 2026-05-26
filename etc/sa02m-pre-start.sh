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

# ── Reboot reason logger ────────────────────────────────────────────────────
# Запускается первым — до любых операций с железом.
# Определяет причину перезагрузки и сохраняет диагностику в лог.
#
# Маркер чистого выключения: /run/sa02m-clean-shutdown
#   Создаётся сервисом sa02m-shutdown-marker при штатном halt/reboot.
#   Если маркера нет → предыдущий останов был аварийным.
SA02M_REBOOT_LOG=/var/log/sa02m-reboot-reason.log
SA02M_CLEAN_MARKER=/var/lib/sa02m-clean-shutdown

{
  echo ""
  echo "════════════════════════════════════════════════════════"
  echo "  Boot: $(date '+%Y-%m-%d %H:%M:%S')"

  # ── Причина предыдущего останова ──
  if [ -f "$SA02M_CLEAN_MARKER" ]; then
    prev_reason=$(cat "$SA02M_CLEAN_MARKER" 2>/dev/null)
    echo "  prev shutdown: CLEAN ($prev_reason)"
    logp "prev shutdown: CLEAN ($prev_reason)"
  else
    echo "  prev shutdown: UNEXPECTED (watchdog / power loss / kernel panic)"
    logp "REBOOT REASON: unexpected — no clean-shutdown marker (watchdog/panic/power loss)"
  fi
  rm -f "$SA02M_CLEAN_MARKER" 2>/dev/null || true

  # ── Watchdog bootstatus (если драйвер поддерживает) ──
  for wd_bs in /sys/class/watchdog/watchdog*/bootstatus; do
    [ -r "$wd_bs" ] || continue
    val=$(cat "$wd_bs" 2>/dev/null)
    if [ "$val" = "0" ]; then
      echo "  watchdog bootstatus: 0 (normal)"
    else
      echo "  watchdog bootstatus: $val  ← WATCHDOG HW RESET"
      logp "REBOOT REASON: hardware watchdog fired (bootstatus=$val)"
    fi
  done

  # ── Kernel crash/panic dump (pstore) ──
  if ls /sys/fs/pstore/*.txt 2>/dev/null | grep -q .; then
    echo "  pstore: CRASH DUMP FOUND"
    logp "REBOOT REASON: kernel crash dump in pstore"
    ls -la /sys/fs/pstore/ 2>/dev/null
  fi

  # ── I2C шины при старте (детектируем i2c-2 = верхняя плата подключена) ──
  i2c_buses=$(ls /sys/bus/i2c/devices/ 2>/dev/null | grep '^i2c-' | tr '\n' ' ')
  echo "  i2c buses: $i2c_buses"
  echo "$i2c_buses" | grep -q 'i2c-2' && \
    echo "  WARNING: i2c-2 present at boot (expansion board connected — may cause lockup if unbind fails)"

  # ── Хвост предыдущего boot-журнала ──
  echo "  --- prev boot last 80 lines ---"
  journalctl -b -1 --no-pager -n 80 -o short-monotonic 2>/dev/null \
    || echo "  (no previous boot journal available)"
  echo "  --- end ---"
  echo "════════════════════════════════════════════════════════"

} >> "$SA02M_REBOOT_LOG" 2>/dev/null || true

# Ротация: последние 512 КБ если файл > 1 МБ
{
  sz=$(stat -c%s "$SA02M_REBOOT_LOG" 2>/dev/null || echo 0)
  if [ "$sz" -gt 1048576 ]; then
    tmp="${SA02M_REBOOT_LOG}.tmp"
    tail -c 524288 "$SA02M_REBOOT_LOG" > "$tmp" 2>/dev/null && mv "$tmp" "$SA02M_REBOOT_LOG" 2>/dev/null || true
  fi
} || true

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

# ── LED eth0 (PB2, /sys/class/leds/eth0_link) — 3 моргания при старте ────────
_eth0_led_blink() {
  local led=/sys/class/leds/eth0_link i
  [ -d "$led" ] || return 0
  echo none > "$led/trigger" 2>/dev/null || true
  for i in 1 2 3; do
    echo 1 > "$led/brightness" 2>/dev/null || true
    sleep 0.5
    echo 0 > "$led/brightness" 2>/dev/null || true
    sleep 0.5
  done
}
_eth0_led_blink

# PCA9536 beeper handled by udev (sa02m-i2c2-unbind.sh) — fires before this service.

exit 0
