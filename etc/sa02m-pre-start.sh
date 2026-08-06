#!/bin/bash
# Ранний запуск (PRE-START): i2c-2/PCA9536 (бипер+LED), RTC, eth_link индикация.
# Питание USB VBUS теперь держится отдельным юнитом sa02m-usb-vbus.service
# (Type=simple, gpioset -m signal 0 268=1). Так `gpioset` не убивается вместе
# с oneshot-скриптом (KillMode=control-group), как это было раньше в
# `sa02m_boot_usb_vbus_on`, и линия 268 удерживается на всём boot pipeline.
# Не использует set -e: сбой опциональных шагов не должен ломать загрузку.
set -u

logp() {
  logger -t sa02m-pre-start -- "$*" 2>/dev/null || true
}

# ── i2c-2 (PCA9536): recovery + boot indication (сразу после USB, до journal/RTC) ──
# Раньше бипер/LED давал ca_02m.service (After=network.target ≈ 50+ с). Теперь здесь.
#
# PCA9536 active-LOW, reg 0x01: bit2=0 → buzzer, bit3=0 → blue OK (как ca_02m.sh / i2cset).
I2C2_PDEV="1c2b800.i2c"
I2C2_SCL_LINE=256
I2C2_SDA_LINE=257
PCA9536_OFF=0x0f

sa02m_hw_backend_disabled() {
  local backend=""
  if [ -f /etc/sa02m_hw.conf ]; then
    # shellcheck source=/dev/null
    . /etc/sa02m_hw.conf 2>/dev/null || true
    backend="${SA02M_HW_BACKEND:-}"
  fi
  [ "$backend" = "disabled" ]
}

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

if sa02m_hw_backend_disabled; then
  logp "i2c-2: skipped (SA02M_HW_BACKEND=disabled)"
elif [ -e "/sys/bus/platform/devices/${I2C2_PDEV}" ]; then
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

# ── MAC: постоянные адреса из SID (eFUSE) ──────────────────────────────────
# У платы нет аппаратного MAC: DT-ячейки nvmem у &emac/&gmac закомментированы,
# а u-boot подставляет в local-mac-address СЛУЧАЙНЫЙ адрес при каждой загрузке
# (видно в /proc/device-tree/soc/ethernet@*/local-mac-address). Следствия:
#   • MPLC Protect выводит SystemKey из MAC → Request key «плавает», и выданная
#     лицензия становится недействительной после первой же перезагрузки;
#   • стенд/учёт не могут опознать плату между прогонами.
# SID (sunxi-sid0) — eFUSE процессора: уникален для каждого кристалла и не
# меняется, поэтому MAC считаем из него. Адрес постоянен для платы и уникален
# в партии. eth0 — чётный последний байт, eth1 = eth0 + 1 (пары плат не
# пересекаются). Явный override — SA02M_MAC_ETH0/ETH1 в /etc/sa02m-mac.conf.
SA02M_SID_NVMEM=/sys/bus/nvmem/devices/sunxi-sid0/nvmem

sa02m_mac_from_sid() {
  local sid_hex b2 b3 b4 b5
  [ -r "$SA02M_SID_NVMEM" ] || return 1
  sid_hex=$(od -An -tx1 -N16 "$SA02M_SID_NVMEM" 2>/dev/null | tr -d ' \n')
  [ ${#sid_hex} -eq 32 ] || return 1
  # последние 4 байта SID (word3) уникальны для кристалла
  b2=${sid_hex:24:2}; b3=${sid_hex:26:2}; b4=${sid_hex:28:2}; b5=${sid_hex:30:2}
  case "$b2$b3$b4$b5" in 00000000|ffffffff) return 1 ;; esac
  b5=$(printf '%02x' $(( 0x$b5 & 0xfe )))   # чётный: eth1 = +1 без пересечений
  printf '02:53:%s:%s:%s:%s\n' "$b2" "$b3" "$b4" "$b5"
}

sa02m_mac_next() {   # 02:53:aa:bb:cc:dd → 02:53:aa:bb:cc:(dd+1)
  local mac=$1 last
  last=$(printf '%02x' $(( 0x${mac##*:} + 1 )))
  printf '%s:%s\n' "${mac%:*}" "$last"
}

sa02m_mac_apply() {
  local eth0_mac="" eth1_mac="" iface want cur
  if [ -f /etc/sa02m-mac.conf ]; then
    # shellcheck source=/dev/null
    . /etc/sa02m-mac.conf 2>/dev/null || true
  fi
  eth0_mac=${SA02M_MAC_ETH0:-$(sa02m_mac_from_sid || true)}
  if [ -z "${eth0_mac:-}" ]; then
    logp "MAC: SID недоступен и override не задан — адреса не трогаем"
    return 1
  fi
  eth1_mac=${SA02M_MAC_ETH1:-$(sa02m_mac_next "$eth0_mac")}
  for iface in eth0 eth1; do
    [ -e "/sys/class/net/$iface/address" ] || continue
    if [ "$iface" = eth0 ]; then want=$eth0_mac; else want=$eth1_mac; fi
    cur=$(cat "/sys/class/net/$iface/address" 2>/dev/null)
    [ "$cur" = "$want" ] && continue
    ip link set dev "$iface" down 2>/dev/null || true
    if ip link set dev "$iface" address "$want" 2>/dev/null; then
      logp "MAC: $iface $cur → $want (из SID)"
    else
      logp "MAC: не удалось задать $want на $iface"
    fi
    ip link set dev "$iface" up 2>/dev/null || true
  done
}

sa02m_mac_apply || true

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
elif [ -f /usr/local/lib/sa02m-lib-rtc.sh ]; then
  # shellcheck source=/dev/null
  . /usr/local/lib/sa02m-lib-rtc.sh
  if DS3231_STR=$(read_rtc_datetime 2>/dev/null); then
    DS3231_YEAR=${DS3231_STR%%-*}
    if [ -n "$DS3231_YEAR" ] && [ "$DS3231_YEAR" -ge 2020 ] && \
       [ "$DS3231_YEAR" -le 2035 ] 2>/dev/null; then
      # Чип держит UTC (как и /etc/adjtime): раньше здесь был `date -s`,
      # то есть значение трактовалось как местное время — при TZ=MSK это
      # давало расхождение в 3 часа между путём hwclock (/dev/rtc1) и
      # запасным путём i2cget. Единый формат — UTC.
      if date -u -s "$DS3231_STR" >/dev/null 2>&1; then
        logp "RTC: DS3231 loaded via I2C (UTC) — $(date '+%Y-%m-%d %H:%M:%S %Z') (year=${DS3231_YEAR})"
      fi
    else
      logp "RTC: DS3231 I2C year=${DS3231_YEAR:-?} invalid — keeping fake-hwclock time"
    fi
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

# ── Link LEDs (platform gpio-led) — 3 моргания основного, затем sync по variant ─
# sa02m-1eth: eth0_link←eth0; sa02m-2eth: eth0_link←eth1, eth1_link←eth0.
LED_LIB=/usr/local/lib/sa02m-eth-led-lib.sh
if [ -f "$LED_LIB" ]; then
  # shellcheck disable=SC1090
  . "$LED_LIB"
  led=/sys/class/leds/eth0_link
  if [ -d "$led" ]; then
    echo none > "$led/trigger" 2>/dev/null || true
    for i in 1 2 3; do
      echo 1 > "$led/brightness" 2>/dev/null || true
      sleep 0.5
      echo 0 > "$led/brightness" 2>/dev/null || true
      sleep 0.5
    done
    sa02m_led_sync_all
  fi
fi

# ── Kernel desired vs running (diagnostic after reboot) ─────────────────────
if [ -f /etc/sa02m_kernel.conf ]; then
  # shellcheck disable=SC1091
  . /etc/sa02m_kernel.conf 2>/dev/null || true
  _kr=$(uname -r 2>/dev/null || true)
  _run=smp
  case "$_kr" in *-rt*|*rt[0-9]*) _run=rt ;; esac
  if [ -n "${SA02M_KERNEL_DESIRED:-}" ] && [ "$SA02M_KERNEL_DESIRED" != "$_run" ]; then
    logp "kernel mismatch: running=$_run desired=$SA02M_KERNEL_DESIRED (reboot pending or switch failed?)"
  fi
fi

exit 0
