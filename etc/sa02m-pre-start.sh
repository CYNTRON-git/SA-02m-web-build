#!/bin/bash
# Ранний запуск (PRE-START): питание USB, i2c-2/PCA9536 (бипер+LED), RTC, eth0_link.
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

# ── i2c-2 (PCA9536): recovery + boot indication (сразу после USB, до journal/RTC) ──
# Раньше бипер/LED давал ca_02m.service (After=network.target ≈ 50+ с). Теперь здесь.
#
# PCA9536 active-LOW, reg 0x01: bit2=0 → buzzer, bit3=0 → blue OK (как ca_02m.sh / i2cset).
I2C2_PDEV="1c2b800.i2c"
I2C2_SCL_LINE=256
I2C2_SDA_LINE=257
PCA9536_OFF=0x0f

sa02m_pca9536_boot_indication() {
  local i
  [ -c /dev/i2c-2 ] || return 1

  i2cset -y 2 0x41 0x03 0x00 2>/dev/null \
    || i2cset -y 2 0x41 0x03 0xf0 2>/dev/null \
    || return 1

  i2cset -y 2 0x41 0x01 0x0B 2>/dev/null || true
  sleep 0.2
  i2cset -y 2 0x41 0x01 "$PCA9536_OFF" 2>/dev/null || true

  for i in 1 2 3; do
    i2cset -y 2 0x41 0x01 0x07 2>/dev/null || true
    sleep 0.5
    i2cset -y 2 0x41 0x01 "$PCA9536_OFF" 2>/dev/null || true
    sleep 0.5
  done

  i2cset -y 2 0x41 0x01 0xff 2>/dev/null || true
  logp "i2c-2: PCA9536 boot indication (beep + 3x blue blink)"
}

sa02m_i2c2_recover_and_init() {
  local drv try sda_val i

  for drv in /sys/bus/platform/drivers/mv64xxx_i2c \
             /sys/bus/platform/drivers/i2c-sunxi; do
    [ -d "${drv}/${I2C2_PDEV}" ] || continue
    logp "i2c-2: unbinding ${I2C2_PDEV} to stop IRQ storm"
    printf '%s' "$I2C2_PDEV" > "${drv}/unbind" 2>/dev/null || true
    break
  done
  sleep 0.15

  if command -v gpioset >/dev/null 2>&1; then
    sda_val=$(gpioget 0 $I2C2_SDA_LINE 2>/dev/null || echo "1")
    if [ "$sda_val" = "0" ]; then
      logp "i2c-2: SDA stuck low — GPIO recovery (9 SCL pulses)"
      for i in 1 2 3 4 5 6 7 8 9; do
        gpioset 0 ${I2C2_SCL_LINE}=0 2>/dev/null || true
      done
    fi
  fi

  touch /run/sa02m-i2c2-recovered

  for drv in /sys/bus/platform/drivers/mv64xxx_i2c \
             /sys/bus/platform/drivers/i2c-sunxi; do
    [ -f "${drv}/bind" ] || continue
    printf '%s' "$I2C2_PDEV" > "${drv}/bind" 2>/dev/null && \
      logp "i2c-2: driver rebound via $(basename "$drv")" && break
  done

  for try in 1 2 3 4 5; do
    [ -c /dev/i2c-2 ] && break
    sleep 0.3
  done
  [ -c /dev/i2c-2 ] || { logp "i2c-2: /dev/i2c-2 not available"; return 1; }

  sa02m_pca9536_boot_indication || logp "i2c-2: PCA9536 boot indication failed"
}

if [ -e "/sys/bus/platform/devices/${I2C2_PDEV}" ]; then
  sa02m_i2c2_recover_and_init || true
fi

# ── Reboot reason logger (фон — journalctl -b -1 тормозит ранний старт) ─────
SA02M_REBOOT_LOG=/var/log/sa02m-reboot-reason.log
SA02M_CLEAN_MARKER=/var/lib/sa02m-clean-shutdown

(
  {
    echo ""
    echo "════════════════════════════════════════════════════════"
    echo "  Boot: $(date '+%Y-%m-%d %H:%M:%S')"

    if [ -f "$SA02M_CLEAN_MARKER" ]; then
      prev_reason=$(cat "$SA02M_CLEAN_MARKER" 2>/dev/null)
      echo "  prev shutdown: CLEAN ($prev_reason)"
      logp "prev shutdown: CLEAN ($prev_reason)"
    else
      echo "  prev shutdown: UNEXPECTED (watchdog / power loss / kernel panic)"
      logp "REBOOT REASON: unexpected — no clean-shutdown marker"
    fi
    rm -f "$SA02M_CLEAN_MARKER" 2>/dev/null || true

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

    if ls /sys/fs/pstore/*.txt 2>/dev/null | grep -q .; then
      echo "  pstore: CRASH DUMP FOUND"
      logp "REBOOT REASON: kernel crash dump in pstore"
    fi

    i2c_buses=$(ls /sys/bus/i2c/devices/ 2>/dev/null | grep '^i2c-' | tr '\n' ' ')
    echo "  i2c buses: $i2c_buses"

    echo "  --- prev boot last 80 lines ---"
    journalctl -b -1 --no-pager -n 80 -o short-monotonic 2>/dev/null \
      || echo "  (no previous boot journal available)"
    echo "  --- end ---"
    echo "════════════════════════════════════════════════════════"
  } >> "$SA02M_REBOOT_LOG" 2>/dev/null || true

  sz=$(stat -c%s "$SA02M_REBOOT_LOG" 2>/dev/null || echo 0)
  if [ "$sz" -gt 1048576 ]; then
    tmp="${SA02M_REBOOT_LOG}.tmp"
    tail -c 524288 "$SA02M_REBOOT_LOG" > "$tmp" 2>/dev/null && mv "$tmp" "$SA02M_REBOOT_LOG" 2>/dev/null || true
  fi
) &

# ── RTC: DS3231 на i2c-1 ───────────────────────────────────────────────────
HCTOSYS_DEVICE=rtc0
HWC=/sbin/hwclock
[ -x "$HWC" ] || HWC=/usr/sbin/hwclock
[ -x "$HWC" ] || HWC=$(command -v hwclock 2>/dev/null || true)
[ -n "${HWC:-}" ] || HWC=""

if [ ! -e /dev/rtc1 ] && [ -w /sys/class/i2c-adapter/i2c-1/new_device ]; then
  if [ ! -d /sys/bus/i2c/devices/1-0068 ]; then
    echo ds3231 0x68 > /sys/class/i2c-adapter/i2c-1/new_device 2>/dev/null \
      || logp "ds3231 new_device failed"
  fi
  for try in 1 2 3; do
    [ -c /dev/rtc1 ] && break
    sleep 0.5
  done
fi
# Load DS3231 → system clock only if the year is plausible (2020–2035).
# fake-hwclock.service already loaded fake-hwclock.data earlier; DS3231
# overrides it when its time is valid and ≥ the fake-hwclock timestamp.
if [ -n "$HWC" ] && [ -c /dev/rtc1 ]; then
  DS3231_STR=$("$HWC" --show --rtc /dev/rtc1 2>/dev/null | head -1)
  DS3231_YEAR=$(echo "$DS3231_STR" | awk '{print substr($1,1,4); exit}')
  if [ -n "$DS3231_YEAR" ] && [ "$DS3231_YEAR" -ge 2020 ] && \
     [ "$DS3231_YEAR" -le 2035 ] 2>/dev/null; then
    if "$HWC" -s -f /dev/rtc1 >/dev/null 2>&1; then
      HCTOSYS_DEVICE=rtc1
      logp "RTC: DS3231 loaded — $(date '+%Y-%m-%d %H:%M:%S') (year=${DS3231_YEAR})"
    fi
  else
    logp "RTC: DS3231 year=${DS3231_YEAR:-?} invalid — keeping fake-hwclock time"
  fi
fi
# Point /dev/rtc symlink at the authoritative source (DS3231 when present).
if [ -c "/dev/${HCTOSYS_DEVICE}" ]; then
  rm -f /dev/rtc
  ln -sf "/dev/${HCTOSYS_DEVICE}" /dev/rtc 2>/dev/null || true
fi
# Write-back at boot is intentionally omitted: writing DS3231 with the just-
# loaded DS3231 time is a no-op. sa02m-rtc-sync.timer will write the
# NTP-corrected time 5 min after boot and then every 30 min.

# ── LED eth0 (on-board PHY link LED) — 3 моргания ───────────────────────────
# Использует netdev trigger: опрашивает carrier активно, загорается сразу.
# PHY trigger (mdio*:link) отклонён: при назначении не читает текущее
# состояние линка — LED остаётся выключен до следующей смены состояния.
eth0_led_set_link_trigger() {
  local led=/sys/class/leds/eth0_link
  local iface=${1:-eth0}
  [ -d "$led" ] || return 0
  echo "netdev" > "$led/trigger"     2>/dev/null || true
  echo "$iface"  > "$led/device_name" 2>/dev/null || true
  echo "1"       > "$led/link"        2>/dev/null || true
}

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
  eth0_led_set_link_trigger eth0
}
_eth0_led_blink

exit 0
