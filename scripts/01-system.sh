#!/bin/bash
set -o pipefail  # catch masked failures in pipes (Y7); set -u deferred pending on-device install test
# ═══════════════════════════════════════════════════════════════════════════
# 01-system.sh  •  Base OS, users, packages, serial symlinks
# ═══════════════════════════════════════════════════════════════════════════
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

ETC_REPO="$SCRIPT_DIR/../etc"

log INFO "=== [01] Системная настройка ==="

# ── Armbian board branding (MOTD / release metadata) ───────────────────────
if [ -z "${SA02M_ROOTFS_BUILD:-}" ] && [ -f "$ETC_REPO/sa02m-armbian-branding.sh" ]; then
    install -m 755 "$ETC_REPO/sa02m-armbian-branding.sh" /usr/local/sbin/sa02m-armbian-branding
    sed -i 's/\r$//' /usr/local/sbin/sa02m-armbian-branding
    /usr/local/sbin/sa02m-armbian-branding >> "$LOG_FILE" 2>&1 \
        && log OK "Armbian branding: CYNTRON SA-02m" \
        || log WARN "Не удалось применить Armbian branding"
fi

# ── SA-02m MOTD summary (pam_motd → /run/motd.dynamic на SSH-логине) ───────
if [ -f "$ETC_REPO/update-motd.d/20-sa02m-summary" ]; then
    install -d -m 755 /etc/update-motd.d
    install -m 755 "$ETC_REPO/update-motd.d/20-sa02m-summary" \
        /etc/update-motd.d/20-sa02m-summary
    sed -i 's/\r$//' /etc/update-motd.d/20-sa02m-summary
    # Отключаем стандартный Debian `10-uname` (заменяется нашей сводкой).
    if [ -x /etc/update-motd.d/10-uname ]; then
        chmod -x /etc/update-motd.d/10-uname 2>/dev/null || true
    fi
    # Прегенерация /run/motd.dynamic, чтобы MOTD сразу был доступен без ожидания
    # первого SSH-логина (pam_motd всё равно перегенерирует его при login).
    run-parts /etc/update-motd.d > /run/motd.dynamic 2>/dev/null || true
    log OK "Установлен MOTD /etc/update-motd.d/20-sa02m-summary"
fi

# ── Locale & timezone ──────────────────────────────────────────────────────
log INFO "Настройка локали и таймзоны"
locale-gen ru_RU.UTF-8 en_US.UTF-8 >> "$LOG_FILE" 2>&1 || true
update-locale LANG=ru_RU.UTF-8 >> "$LOG_FILE" 2>&1 || true
timedatectl set-timezone Europe/Moscow >> "$LOG_FILE" 2>&1 || true

# ── SSH: direct service mode instead of socket activation ──────────────────
if [ -f "$ETC_REPO/sa02m-ssh-direct.sh" ]; then
    install -m 755 "$ETC_REPO/sa02m-ssh-direct.sh" /usr/local/sbin/sa02m-ssh-direct
    /usr/local/sbin/sa02m-ssh-direct >> "$LOG_FILE" 2>&1 || log WARN "Не удалось перевести SSH в direct service mode"
fi
if [ -f "$ETC_REPO/sa02m-dbus-recover.sh" ]; then
    install -m 755 "$ETC_REPO/sa02m-dbus-recover.sh" /usr/local/sbin/sa02m-dbus-recover
fi

# ── Persist hardware variant ───────────────────────────────────────────────
HW_VARIANT=$(sa02m_hw_variant)
if [ ! -f /etc/sa02m_hw_variant.conf ] || [ -n "${SA02M_HW_VARIANT:-}" ]; then
    printf 'SA02M_HW_VARIANT=%s\n' "$HW_VARIANT" > /etc/sa02m_hw_variant.conf
    chmod 644 /etc/sa02m_hw_variant.conf
    log INFO "Аппаратный вариант: $HW_VARIANT"
fi

SERIAL_PROFILE_CONF=/etc/sa02m_serial_profile.conf
if [ -n "${SA02M_SERIAL_PROFILE:-}" ]; then
    printf 'SA02M_SERIAL_PROFILE=%s\n' "$SA02M_SERIAL_PROFILE" > "$SERIAL_PROFILE_CONF"
    chmod 644 "$SERIAL_PROFILE_CONF"
    log INFO "Зафиксирован serial-профиль в $SERIAL_PROFILE_CONF: $SA02M_SERIAL_PROFILE"
elif [ ! -f "$SERIAL_PROFILE_CONF" ] && [ -f "$ETC_REPO/sa02m_serial_profile.conf" ]; then
    install -m 644 "$ETC_REPO/sa02m_serial_profile.conf" "$SERIAL_PROFILE_CONF"
    log INFO "Установлен шаблон $SERIAL_PROFILE_CONF"
fi

# ── Required packages ──────────────────────────────────────────────────────
log INFO "Установка пакетов"
apt-get update -qq >> "$LOG_FILE" 2>&1
pkg_install nginx fcgiwrap openssl net-tools psmisc exfatprogs \
    i2c-tools gpiod libgpiod2 python3-libgpiod \
    python3-paho-mqtt python3-yaml python3-serial

# ── User hmi ──────────────────────────────────────────────────────────────
if ! id hmi &>/dev/null; then
    log INFO "Создание пользователя hmi"
    useradd -m -s /bin/bash hmi >> "$LOG_FILE" 2>&1
fi

# ── Serial console policy (production) ─────────────────────────────────────
# На production-устройстве СА-02м ВСЕ /dev/ttyS* используются как RS-485 / COM
# для Modbus RTU (RS-485-0..4) и MR-02m flasher (ttyS1). Отладочный getty на
# любом ttyS ломает протокол — на serial-порту одновременно нельзя иметь
# login-prompt и Modbus-мастер.
#
# ttyGS0 (USB gadget serial через Type-C) — планируется как единственный
# отладочный serial, но требует CONFIG_USB_GADGET / CONFIG_USB_G_SERIAL /
# CONFIG_USB_CONFIGFS_ACM в ядре. Текущее WB 5.10.35-sa02m+ собрано без них
# (`# CONFIG_USB_GADGET is not set`) → /dev/ttyGS0 физически не создаётся.
# Пока (Фаза 1) — оставляем getty@ttyGS0/serial-getty@ttyGS0 masked; после
# пересборки ядра (Фаза 2) — enable serial-getty@ttyGS0.
#
# Единственный поддерживаемый способ отладки: SSH через Ethernet.

# Mask ВСЕХ serial-getty@ttyS* (S0..S7) — не должно быть login-prompt на COM.
for n in 0 1 2 3 4 5 6 7; do
    systemctl disable "serial-getty@ttyS${n}" 2>/dev/null || true
    systemctl mask    "serial-getty@ttyS${n}" 2>/dev/null || true
    systemctl disable "getty@ttyS${n}"        2>/dev/null || true
    systemctl mask    "getty@ttyS${n}"        2>/dev/null || true
done
# Убрать все wants-symlinks от предыдущих Armbian/upgrade поколений
rm -f /etc/systemd/system/getty.target.wants/getty@ttyS*.service \
      /etc/systemd/system/serial-getty.target.wants/serial-getty@ttyS*.service 2>/dev/null || true
# systemd-getty-generator тоже создаёт serial-getty@ttyS0.service при
# console=ttyS0 в cmdline. После смены bootargs на console=tty1 (см.
# etc/boot.cmd.sa02m) generator не будет запускать serial-getty на ttyS0 —
# но mask-состояние остаётся страховкой на случай отката bootargs.

# ttyGS0: пока kernel без USB_GADGET — mask, чтобы systemd не пытался запускать
# getty на несуществующем устройстве. При пересборке ядра — раскомментировать
# два systemctl unmask/enable ниже.
systemctl disable "serial-getty@ttyGS0" 2>/dev/null || true
systemctl mask    "serial-getty@ttyGS0" 2>/dev/null || true
systemctl disable "getty@ttyGS0"        2>/dev/null || true
systemctl mask    "getty@ttyGS0"        2>/dev/null || true
# TODO(Phase 2, после kernel rebuild с CONFIG_USB_GADGET):
#   systemctl unmask serial-getty@ttyGS0.service
#   systemctl enable serial-getty@ttyGS0.service
rm -f /etc/systemd/system/getty.target.wants/getty@ttyGS0.service \
      /etc/systemd/system/serial-getty.target.wants/serial-getty@ttyGS0.service 2>/dev/null || true

# ── RS-485 / COM symlinks ──────────────────────────────────────────────────
SERIAL_PROFILE=$(sa02m_serial_profile)
BOARD_MODEL=$(sa02m_board_model)
read -r -a SERIAL_TARGETS <<< "$(sa02m_serial_targets "$SERIAL_PROFILE")"

log INFO "Профиль serial/RS-485: ${SERIAL_PROFILE} (model='${BOARD_MODEL:-unknown}')"
log INFO "Создание симлинков RS-485 / COM"

for stale_idx in 0 1 2 3 4; do
    rm -f "/dev/RS-485-${stale_idx}" "/dev/COM$(( stale_idx + 1 ))"
done

for idx in "${!SERIAL_TARGETS[@]}"; do
    tty="${SERIAL_TARGETS[$idx]}"
    com_idx=$(( idx + 1 ))
    for lnk in "RS-485-${idx}" "COM${com_idx}"; do
        target="/dev/${tty}"
        if [ -e "$target" ]; then
            ln -sf "$target" "/dev/$lnk"
            log OK "  /dev/$lnk → $target"
        else
            log WARN "  /dev/$lnk пропущен: $target отсутствует"
        fi
    done
done

# ── Persist symlinks via udev ─────────────────────────────────────────────
UDEV_RULE="/etc/udev/rules.d/99-sa02m-serial.rules"
{
    echo "# Generated by SA-02m installer"
    for idx in "${!SERIAL_TARGETS[@]}"; do
        tty="${SERIAL_TARGETS[$idx]}"
        com_idx=$(( idx + 1 ))
        echo "KERNEL==\"${tty}\", SYMLINK+=\"RS-485-${idx} COM${com_idx}\""
    done
} > "$UDEV_RULE"
# Remove legacy 99-com-aliases.rules: it duplicates or conflicts with
# 99-sa02m-serial.rules (e.g. stale 1-eth ttyS0=COM1 on a 2-eth device).
rm -f /etc/udev/rules.d/99-com-aliases.rules
udevadm control --reload-rules 2>/dev/null || true
write_sa02m_serial_map_conf /etc/sa02m_serial_map.conf
log INFO "Карта serial-портов записана в /etc/sa02m_serial_map.conf"

# ── USB / microSD: убрать устаревшую связку 99-local.rules + usb-mount@ ─────
OLD_LOCAL=/etc/udev/rules.d/99-local.rules
if [ -f "$OLD_LOCAL" ] && grep -q 'usb-mount@' "$OLD_LOCAL" 2>/dev/null; then
    grep -v 'usb-mount@' "$OLD_LOCAL" > /tmp/99-local.sa02m-strip.tmp 2>/dev/null || true
    if [ -s /tmp/99-local.sa02m-strip.tmp ]; then
        mv /tmp/99-local.sa02m-strip.tmp "$OLD_LOCAL"
        log INFO "Из $OLD_LOCAL удалены строки с usb-mount@"
    else
        rm -f "$OLD_LOCAL" /tmp/99-local.sa02m-strip.tmp
        log INFO "Удалён устаревший $OLD_LOCAL (правила usb-mount)"
    fi
    udevadm control --reload-rules 2>/dev/null || true
fi
if [ -f /etc/systemd/system/usb-mount@.service ]; then
    log INFO "Удаление устаревшего usb-mount@.service"
    systemctl list-units --no-pager --type=service 2>/dev/null \
        | grep -oE 'usb-mount@[^[:space:]]+\.service' | sort -u \
        | while read -r u; do systemctl stop "$u" 2>/dev/null || true; done
    rm -f /etc/systemd/system/usb-mount@.service
    systemctl daemon-reload >> "$LOG_FILE" 2>&1 || true
fi
if [ -f /usr/local/bin/usb-mount.sh ]; then
    log INFO "Удаление устаревшего /usr/local/bin/usb-mount.sh"
    rm -f /usr/local/bin/usb-mount.sh
fi

# ── USB / microSD: udev + storage-mount (exFAT при пустой ФС или NTFS) ─────
if [ -f "$ETC_REPO/storage-mount.sh" ]; then
    log INFO "Установка storage-mount (USB / microSD)"
    install -m 755 "$ETC_REPO/storage-mount.sh" /usr/local/bin/storage-mount.sh
    install -m 755 "$ETC_REPO/sa02m-set-storage-auto-format" /usr/local/sbin/sa02m-set-storage-auto-format
    # Репозиторий часто синхронизируется с Windows: удаляем CRLF у shebang helper-скрипта.
    sed -i 's/\r$//' /usr/local/sbin/sa02m-set-storage-auto-format
    install -m 644 "$ETC_REPO/systemd/storage-mount@.service" /etc/systemd/system/storage-mount@.service
    install -m 644 "$ETC_REPO/udev/99-storage.rules" /etc/udev/rules.d/99-storage.rules
    if [ ! -f /etc/sa02m_storage.conf ]; then
        install -m 644 "$ETC_REPO/sa02m_storage.conf" /etc/sa02m_storage.conf
    else
        # Лечим случай, когда предыдущая правка через sed оставила хвост
        # вроде «STORAGE_AUTO_FORMAT=0n» — иначе скрипт парсит как «не 0
        # и не 1» и идёт в безопасный default, что путает диагностику.
        sed -i -E 's/^(STORAGE_AUTO_FORMAT=)([01])[A-Za-z]+/\1\2/' /etc/sa02m_storage.conf
    fi
    # ntfs-3g (FUSE) — userspace-фолбэк для NTFS, на случай если kernel
    # ntfs3 откажется монтировать «грязную» NTFS после Windows quick-eject.
    # На некоторых сборках Armbian пакет недоступен — установка опциональна.
    if ! command -v mount.ntfs-3g >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get -y install ntfs-3g >>"$LOG_FILE" 2>&1 || \
            log WARN "ntfs-3g недоступен в репозитории — оставляем только kernel ntfs3"
    fi
    systemctl daemon-reload >> "$LOG_FILE" 2>&1 || true
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger --subsystem-match=block --action=change 2>/dev/null || true
    log OK "storage-mount и udev 99-storage.rules установлены"
else
    log WARN "Нет $ETC_REPO/storage-mount.sh — пропуск установки съёмных носителей"
fi

# ── USB-модем: пакеты, udev-правила, сервисы, шаблон PPP ─────────────────────
log INFO "Настройка USB-модема"

# Необходимые пакеты.
# modemmanager   — управляет 3G/4G модемами (PPP, QMI, MBIM, signal, PIN).
# ppp            — PPP-стек для ttyUSB-модемов (AT-команды + соединение).
# isc-dhcp-client (dhclient) — DHCP для CDC-ECM/RNDIS/NCM USB-ethernet-модемов.
# usb-modeswitch + data — переключение модема из mass-storage в модемный режим
#                (Huawei/ZTE 4G-донглы после первого подключения).
# libqmi-utils   — qmicli, qmi-network для Quectel EC25 / Sierra QMI-модемов.
# libmbim-utils  — mbimcli, mbim-network для новых Fibocom / Quectel MBIM.
# usbutils       — lsusb для диагностики.
MODEM_PKGS="modemmanager ppp isc-dhcp-client usb-modeswitch usb-modeswitch-data libqmi-utils libmbim-utils usbutils"
for pkg in $MODEM_PKGS; do
    dpkg -s "$pkg" >/dev/null 2>&1 || \
        DEBIAN_FRONTEND=noninteractive apt-get -y install "$pkg" >>"$LOG_FILE" 2>&1 || \
        log WARN "Пакет $pkg не удалось установить (возможно, не в репозитории)"
done

# udev-правила для модемов.
if [ -f "$ETC_REPO/udev/99-modem.rules" ]; then
    install -m 644 "$ETC_REPO/udev/99-modem.rules" /etc/udev/rules.d/99-modem.rules
fi

# Сервис DHCP для CDC-ethernet модемов.
if [ -f "$ETC_REPO/systemd/sa02m-modem-dhcp@.service" ]; then
    install -m 644 "$ETC_REPO/systemd/sa02m-modem-dhcp@.service" \
        /etc/systemd/system/sa02m-modem-dhcp@.service
    # Migration: the unit is event-driven only (udev 99-modem.rules); a
    # statically ENABLED instance (a one-off manual `systemctl enable` on a
    # device) pins its BindsTo modem device job into the boot transaction and
    # holds multi-user.target for the 90 s JobTimeout when the modem is
    # absent (bench 2026-07-30). Idempotent: rm -f on the wants glob; the
    # daemon-reload below (end of the modem section) picks it up.
    rm -f /etc/systemd/system/multi-user.target.wants/sa02m-modem-dhcp@*.service 2>/dev/null || true
fi

# Сервис PPP для ttyUSB-модемов (не запускаем автоматически — только по udev).
if [ -f "$ETC_REPO/systemd/sa02m-modem-ppp.service" ]; then
    install -m 644 "$ETC_REPO/systemd/sa02m-modem-ppp.service" \
        /etc/systemd/system/sa02m-modem-ppp.service
fi

# PPP-конфигурация: peers/modem, ip-up/down хуки.
mkdir -p /etc/ppp/peers /etc/ppp/ip-up.d /etc/ppp/ip-down.d
if [ -f "$ETC_REPO/ppp/peers/modem" ] && [ ! -f /etc/ppp/peers/modem ]; then
    install -m 600 "$ETC_REPO/ppp/peers/modem" /etc/ppp/peers/modem
fi
if [ -f "$ETC_REPO/ppp/ip-up.d/sa02m-modem" ]; then
    install -m 755 "$ETC_REPO/ppp/ip-up.d/sa02m-modem" /etc/ppp/ip-up.d/sa02m-modem
fi
if [ -f "$ETC_REPO/ppp/ip-down.d/sa02m-modem" ]; then
    install -m 755 "$ETC_REPO/ppp/ip-down.d/sa02m-modem" /etc/ppp/ip-down.d/sa02m-modem
fi

# dhclient exit-hook: metric 100 для USB-модемных интерфейсов (enx*/usb*/eth1+).
# Предотвращает замену eth0-default маршрута (onlink) модемным маршрутом.
if [ -f "$ETC_REPO/dhcp/dhclient-exit-hooks.d/sa02m-modem-metric" ]; then
    mkdir -p /etc/dhcp/dhclient-exit-hooks.d
    install -m 755 "$ETC_REPO/dhcp/dhclient-exit-hooks.d/sa02m-modem-metric" \
        /etc/dhcp/dhclient-exit-hooks.d/sa02m-modem-metric
    sed -i 's/\r$//' /etc/dhcp/dhclient-exit-hooks.d/sa02m-modem-metric
fi

# Конфиг модема (только шаблон, не перезаписываем пользовательский).
if [ -f "$ETC_REPO/sa02m_modem.conf" ] && [ ! -f /etc/sa02m_modem.conf ]; then
    install -m 644 "$ETC_REPO/sa02m_modem.conf" /etc/sa02m_modem.conf
fi

# ModemManager: разрешаем управлять модемами, но НЕ перегружаем NetworkManager.
# ModemManager работает самостоятельно (mmcli, pppd) без NM.
sa02m_systemctl unmask ModemManager.service 2>/dev/null || true
sa02m_systemctl enable ModemManager.service >>"$LOG_FILE" 2>&1 || \
    log WARN "ModemManager не удалось включить"
sa02m_systemctl start ModemManager.service >>"$LOG_FILE" 2>&1 || \
    log WARN "ModemManager не запустился (возможно, не установлен)"

systemctl daemon-reload >>"$LOG_FILE" 2>&1 || true
udevadm control --reload-rules 2>/dev/null || true
log OK "USB-модем: пакеты, udev, сервисы установлены"

# ── Ранний PRE-START: USB, RTC (DS3231 при отсутствии rtc1), PCA9536 ────────
if [ -f "$ETC_REPO/sa02m-pre-start.sh" ]; then
    log INFO "Установка sa02m-pre-start.service"
    install -m 755 "$ETC_REPO/sa02m-pre-start.sh" /usr/local/sbin/sa02m-pre-start.sh
    if [ -f "$ETC_REPO/systemd/sa02m-pre-start.service" ]; then
        install -m 644 "$ETC_REPO/systemd/sa02m-pre-start.service" /etc/systemd/system/sa02m-pre-start.service
    fi
    if [ -f "$ETC_REPO/systemd/mplc4.service" ] && [ -x /etc/init.d/mplc4 ]; then
        install -m 644 "$ETC_REPO/systemd/mplc4.service" /etc/systemd/system/mplc4.service
    fi
    systemctl daemon-reload >> "$LOG_FILE" 2>&1 || true
    systemctl enable sa02m-pre-start.service >> "$LOG_FILE" 2>&1 || true
    systemctl enable mplc4.service >> "$LOG_FILE" 2>&1 || true
    log OK "sa02m-pre-start установлен и включён"
fi

# ── USB VBUS hold: отдельный Type=simple юнит с `gpioset -m signal 0 268=1` ──
# См. etc/systemd/sa02m-usb-vbus.service — исторически pre-start (oneshot) держал
# VBUS сам, но `KillMode=control-group` убивал backgrounded gpioset вместе с
# завершением скрипта. В результате dmesg показывал `usb0-vbus: disabling` через
# ~30 с после boot, а USB-модем/накопитель на USB-A порту SA-02m не поднимался
# без ручного «reset питания» из web-панели.
if [ -f "$ETC_REPO/systemd/sa02m-usb-vbus.service" ]; then
    log INFO "Установка sa02m-usb-vbus.service (гарантированное VBUS ON после boot)"
    install -m 644 "$ETC_REPO/systemd/sa02m-usb-vbus.service" /etc/systemd/system/sa02m-usb-vbus.service
    systemctl daemon-reload >> "$LOG_FILE" 2>&1 || true
    systemctl enable sa02m-usb-vbus.service >> "$LOG_FILE" 2>&1 || true
    log OK "sa02m-usb-vbus установлен и включён"
fi

# ── Kernel-conditional service policy (CODESYS/CodeMeter/docker) ───────────
# One policy home: /usr/local/sbin/sa02m-kernel-service-guard.sh
# (contract: docs/contracts/kernel-conditional-services.md). apply-policy is
# idempotent — safe on installer re-runs and on codesys-less devices.
if [ -f "$ETC_REPO/sa02m-kernel-service-guard.sh" ]; then
    log INFO "Установка sa02m-kernel-service-guard (kernel-политика служб)"
    install -m 755 "$ETC_REPO/sa02m-kernel-service-guard.sh" \
        /usr/local/sbin/sa02m-kernel-service-guard.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-kernel-service-guard.sh
    if [ -f "$ETC_REPO/systemd/system/sa02m-kernel-service-guard.service" ]; then
        install -m 644 "$ETC_REPO/systemd/system/sa02m-kernel-service-guard.service" \
            /etc/systemd/system/sa02m-kernel-service-guard.service
    fi
    if [ -f "$ETC_REPO/systemd/system/docker.service.d/sa02m-kernel-guard.conf" ]; then
        install -d -m 755 /etc/systemd/system/docker.service.d
        install -m 644 "$ETC_REPO/systemd/system/docker.service.d/sa02m-kernel-guard.conf" \
            /etc/systemd/system/docker.service.d/sa02m-kernel-guard.conf
    fi
    sa02m_systemctl daemon-reload >> "$LOG_FILE" 2>&1 || true
    sa02m_systemctl enable sa02m-kernel-service-guard.service >> "$LOG_FILE" 2>&1 || true
    /usr/local/sbin/sa02m-kernel-service-guard.sh apply-policy >> "$LOG_FILE" 2>&1 || true
    log OK "sa02m-kernel-service-guard установлен и включён (apply-policy применён)"
fi

# ── DS3231 RTC sync: периодическая запись NTP→DS3231 + сохранение при shutdown ──
# Алгоритм синхронизации времени:
#   Boot:     fake-hwclock.service → система; sa02m-pre-start → DS3231→система (если год≥2020)
#   Работа:   sa02m-rtc-sync.timer каждые 30 мин → DS3231 ← система (если NTP synced)
#   Shutdown: sa02m-pre-start.service ExecStop → DS3231 ← система (последнее актуальное время)
#             fake-hwclock.service ExecStop → fake-hwclock.data ← система
if [ -f "$ETC_REPO/sa02m-rtc-sync.sh" ]; then
    log INFO "Установка sa02m-rtc-sync (DS3231 periodic sync)"
    install -m 755 "$ETC_REPO/sa02m-rtc-sync.sh" /usr/local/sbin/sa02m-rtc-sync.sh
    install -m 644 "$ETC_REPO/systemd/sa02m-rtc-sync.service" /etc/systemd/system/sa02m-rtc-sync.service
    install -m 644 "$ETC_REPO/systemd/sa02m-rtc-sync.timer"   /etc/systemd/system/sa02m-rtc-sync.timer
    WWW_RTC_LIB="$SCRIPT_DIR/../www/network_config/cgi-bin/lib_rtc.sh"
    if [ -f "$WWW_RTC_LIB" ]; then
        install -d -m 755 /usr/local/lib
        install -m 755 "$WWW_RTC_LIB" /usr/local/lib/sa02m-lib-rtc.sh
        log OK "sa02m-lib-rtc.sh установлен в /usr/local/lib"
    fi
    sa02m_systemctl daemon-reload >> "$LOG_FILE" 2>&1 || true
    sa02m_systemctl enable sa02m-rtc-sync.timer >> "$LOG_FILE" 2>&1 \
        && log OK "sa02m-rtc-sync.timer включён" \
        || log WARN "sa02m-rtc-sync.timer не включился"
fi

# ── Postinst-hook для linux-image-*.deb (см. tools/kernel-wb/) ─────────────
# При установке .deb-пакета WB-ядра dpkg вызывает run-parts /etc/kernel/postinst.d/;
# хук копирует новый zImage в /usr/local/share/sa02m/kernel/zImage.<flavour>
# и (если flavour совпадает с running) обновляет /mnt/boot_fat/zImage +
# синхронизирует sun8i-a40i-sk.dtb на FAT-раздел.
if [ -f "$ETC_REPO/kernel-postinst.d/50-sa02m-fat-sync" ]; then
    install -d -m 755 /etc/kernel/postinst.d
    install -m 755 "$ETC_REPO/kernel-postinst.d/50-sa02m-fat-sync" \
        /etc/kernel/postinst.d/50-sa02m-fat-sync
    log OK "kernel-postinst hook установлен (WB linux-image-*.deb → FAT auto-sync)"
fi

# ca_02m.service (After=network.target) заменён ранним sa02m-pre-start — отключаем дубль
if [ -f "$ETC_REPO/ca_02m.sh" ]; then
    install -m 755 "$ETC_REPO/ca_02m.sh" /usr/local/sbin/ca_02m.sh
    [ -f /usr/local/bin/ca_02m.sh ] && install -m 755 "$ETC_REPO/ca_02m.sh" /usr/local/bin/ca_02m.sh
    if [ -f "$ETC_REPO/systemd/ca_02m.service" ]; then
        install -m 644 "$ETC_REPO/systemd/ca_02m.service" /etc/systemd/system/ca_02m.service
    fi
    systemctl disable --now ca_02m.service >> "$LOG_FILE" 2>&1 || true
    log OK "ca_02m: no-op, сервис отключён (индикация в sa02m-pre-start)"
fi

# ── Mask unnecessary timers ────────────────────────────────────────────────
for unit in apt-daily.timer apt-daily-upgrade.timer; do
    systemctl mask "$unit" 2>/dev/null || true
done

# ── logrotate: самопочинка конфигов после клонирования образа ───────────────
# После PiShrink-клона на .136 встречался NUL-обнулённый /etc/logrotate.d/wtmp
# (ext4 zero-fill: метаданные записаны, данные не доехали) и осиротевший
# sed-темпфайл. Один битый конфиг валит logrotate.service целиком (exit 1),
# и приёмка hardpy (test_66, systemctl --failed) корректно падает.
rm -f /etc/logrotate.d/sed?????? 2>/dev/null || true
for lr_cfg in /etc/logrotate.d/*; do
    [ -f "$lr_cfg" ] || continue
    # непустой файл без единой печатной строки = NUL-мусор
    if [ -s "$lr_cfg" ] && ! grep -q . "$lr_cfg"; then
        lr_base=$(basename "$lr_cfg")
        if [ -f "$ETC_REPO/logrotate.d/$lr_base" ]; then
            install -m 644 -o root -g root "$ETC_REPO/logrotate.d/$lr_base" "$lr_cfg"
            sed -i 's/\r$//' "$lr_cfg"
            log OK "logrotate: восстановлен битый $lr_cfg из репо"
        else
            mv -f "$lr_cfg" "/var/backups/$lr_base.logrotate.corrupt"
            log WARN "logrotate: $lr_cfg был NUL-мусором, убран в /var/backups (эталона в репо нет)"
        fi
    fi
done
if logrotate -d /etc/logrotate.conf >/dev/null 2>&1; then
    log OK "logrotate: конфигурация валидна (logrotate -d)"
else
    log WARN "logrotate -d нашёл ошибки — проверьте /etc/logrotate.d вручную"
fi

# ── SSH hardening: ClientAlive + UseDNS=no (без зависаний при потере линка) ──
if [ -f "$ETC_REPO/ssh/sshd_config.d/10-sa02m.conf" ]; then
    log INFO "Установка /etc/ssh/sshd_config.d/10-sa02m.conf"
    install -d -m 755 /etc/ssh/sshd_config.d
    install -m 644 "$ETC_REPO/ssh/sshd_config.d/10-sa02m.conf" /etc/ssh/sshd_config.d/10-sa02m.conf
    if sshd -t 2>>"$LOG_FILE"; then
        sa02m_systemctl reload ssh.service >>"$LOG_FILE" 2>&1 || sa02m_systemctl restart ssh.service >>"$LOG_FILE" 2>&1 || true
    fi
fi

# ── Hardware watchdog: используем встроенный PID1-фидер systemd ─────────────
# Старая ad-hoc реализация (printf 1 > /dev/watchdog в цикле) каждые 10 c
# открывала и закрывала устройство, и ядро спамило "watchdog did not stop!".
# systemd сам держит fd открытым и корректно закрывает на shutdown.
log INFO "Активация systemd PID1 watchdog (RuntimeWatchdogSec=10s)"
install -d -m 755 /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/sa02m-watchdog.conf <<'WDG'
[Manager]
# Раз в треть таймаута драйвера sunxi-wdt (~30s) кормим /dev/watchdog.
RuntimeWatchdogSec=10s
# Грейс-период во время shutdown: если что-то зависло — sunxi-wdt
# сделает hardware reset.
ShutdownWatchdogSec=4min
RebootWatchdogSec=4min
WDG

# Уменьшаем умолчательный stop-timeout сервисов: иначе при reboot, если
# какой-нибудь сервис «застрял», systemd ждёт 90 секунд прежде чем
# отправить SIGKILL и продолжить — итог: reboot занимает 2-3 минуты.
cat > /etc/systemd/system.conf.d/sa02m-timeouts.conf <<'TMO'
[Manager]
DefaultTimeoutStopSec=15s
DefaultTimeoutStartSec=30s
TMO
install -d -m 755 /etc/systemd/user.conf.d
cat > /etc/systemd/user.conf.d/sa02m-timeouts.conf <<'TMO'
[Manager]
DefaultTimeoutStopSec=15s
DefaultTimeoutStartSec=30s
TMO

# Если на устройстве остался устаревший feeder-юнит — отключаем и маскируем.
for u in sa02m-watchdog-feed.service watchdog.service software-watchdog.service; do
    sa02m_systemctl stop "$u" 2>/dev/null || true
    sa02m_systemctl disable "$u" 2>/dev/null || true
    sa02m_systemctl mask "$u" 2>/dev/null || true
done

# Расширение rootfs после PiShrink-клона (до userspace-watchdog).
# Не включать параллельно armbian-resize-filesystem — dual resize ломает
# first-boot сеть (udev settle / ifupdown-pre / PHY).
if [ -f "$ETC_REPO/sa02m-rootfs-expand.sh" ]; then
    log INFO "Установка sa02m-rootfs-expand (first-boot eMMC resize)"
    install -m 755 "$ETC_REPO/sa02m-rootfs-expand.sh" /usr/local/sbin/sa02m-rootfs-expand.sh
    install -m 644 "$ETC_REPO/systemd/sa02m-rootfs-expand.service" /etc/systemd/system/sa02m-rootfs-expand.service
    sa02m_systemctl stop armbian-resize-filesystem.service 2>/dev/null || true
    sa02m_systemctl disable armbian-resize-filesystem.service 2>/dev/null || true
    sa02m_systemctl mask armbian-resize-filesystem.service 2>/dev/null || true
    sa02m_systemctl daemon-reload >>"$LOG_FILE" 2>&1 || true
    sa02m_systemctl enable sa02m-rootfs-expand.service >>"$LOG_FILE" 2>&1 || true
fi

# sa02m-net-autolink — УСТАРЕЛО: ранее обновлял link-файлы 10-eth0.link/11-eth1.link
# при смене MAC при переносе образа. Начиная с этой версии используются стабильные
# предсказуемые имена eth0/eth1 без MAC-based переименования — link-файлы не нужны.
# Если сервис был установлен ранее — маскируем его.
for _u in sa02m-net-autolink.service; do
    systemctl stop    "$_u" 2>/dev/null || true
    systemctl disable "$_u" 2>/dev/null || true
    systemctl mask    "$_u" 2>/dev/null || true
done
# Удаляем link-файлы, чтобы ядро использовало предсказуемые имена eth0/eth1.
rm -f /etc/systemd/network/10-eth0.link /etc/systemd/network/11-eth1.link 2>/dev/null || true
log OK "sa02m-net-autolink отключён; link-файлы удалены — используются eth0/eth1"

# Userspace reboot-watchdogs конкурируют с resize2fs на FIRST boot — временный
# mask делает sa02m-rootfs-expand.sh и снимает его в finish_firstboot.
# net-watchdog НЕ маскируем навсегда: он чинит cold-boot PHY / grat-ARP
# (иначе после прошивки образа пинг появляется только после re-plug кабеля).
# RuntimeWatchdogSec (systemd PID1) — отдельно, в system.conf.d.
log INFO "Не маскируем watchdogs навсегда; first-boot mask — только в sa02m-rootfs-expand"
for u in sa02m-userspace-watchdog.service sa02m-failure-monitor.service net-watchdog.service; do
    # Снять stale mask от старых инсталляторов / FEL autorun.
    if [ -L "/etc/systemd/system/$u" ] \
       && [ "$(readlink -f "/etc/systemd/system/$u" 2>/dev/null)" = "/dev/null" ]; then
        rm -f "/etc/systemd/system/$u"
    fi
    sa02m_systemctl unmask "$u" 2>/dev/null || true
    sa02m_systemctl enable "$u" 2>/dev/null || true
done

# ── Маскировка NetworkManager: не управляет ни eth0 (ifupdown), ни can0,    ──
# ── ни eth1 (нет cable). Только тормозил boot на 6 секунд.                  ──
log INFO "Маскируем NetworkManager (eth0/can0/eth1 — unmanaged)"
for u in NetworkManager.service NetworkManager-wait-online.service NetworkManager-dispatcher.service; do
    sa02m_systemctl stop "$u" 2>/dev/null || true
    sa02m_systemctl disable "$u" 2>/dev/null || true
    sa02m_systemctl mask "$u" 2>/dev/null || true
done

# ── Маскировка network-online.target провайдеров ────────────────────────────
# На этой системе сеть поднимается через ifupdown (networking.service).
# systemd-networkd-wait-online и ifupdown-wait-online создают pending-job
# для network-online.target, который никогда не завершается — это замораживает
# очередь jobs systemd и делает все systemctl-вызовы недоступными (timeout).
log INFO "Маскируем systemd-networkd-wait-online и ifupdown-wait-online"
for u in systemd-networkd-wait-online.service ifupdown-wait-online.service; do
    sa02m_systemctl stop "$u" 2>/dev/null || true
    sa02m_systemctl mask "$u" 2>/dev/null || true
done

# ── Время: fake-hwclock + chrony (timesyncd в этом Armbian не пакетируется) ─
log INFO "Настройка времени: fake-hwclock + chrony"
if ! dpkg -l fake-hwclock 2>/dev/null | grep -q '^ii'; then
    DEBIAN_FRONTEND=noninteractive apt-get -y install fake-hwclock >>"$LOG_FILE" 2>&1 || true
fi
# В Armbian образе fake-hwclock.service замаскирован vendor-симлинком в
# /lib/systemd/system → /dev/null. Стандартный `systemctl unmask` НЕ снимает
# vendor-маску. Создаём собственный unit в /etc/systemd/system/ — он имеет
# приоритет над /lib/systemd/system/ и над /usr/lib/systemd/system/.
# fake-hwclock устанавливается по разным путям на Armbian и Debian:
#   Armbian (Ubuntu Noble) → /usr/sbin/fake-hwclock
#   Debian bullseye        → /sbin/fake-hwclock
# Определяем реальный путь и подставляем в unit, иначе systemd падает
# "Failed to start ... (No such file or directory)".
FHC_BIN=""
for cand in /usr/sbin/fake-hwclock /sbin/fake-hwclock /usr/bin/fake-hwclock; do
    if [ -x "$cand" ]; then FHC_BIN="$cand"; break; fi
done
if [ -z "$FHC_BIN" ]; then
    log WARN "fake-hwclock не найден — пропускаем создание unit"
else
    log INFO "fake-hwclock: $FHC_BIN"
    cat > /etc/systemd/system/fake-hwclock.service <<FHS
[Unit]
Description=Restore / save the current clock (SA-02m unmasked)
DefaultDependencies=no
Documentation=man:fake-hwclock(8)
Before=local-fs-pre.target
After=systemd-remount-fs.service
ConditionPathExists=!/run/systemd/fake-hwclock-loaded

[Service]
Type=oneshot
ExecStart=${FHC_BIN} load
ExecStart=/bin/touch /run/systemd/fake-hwclock-loaded
ExecStop=${FHC_BIN} save
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
FHS
    install -d -m 755 /etc/systemd/system/fake-hwclock.service.d
    cat > /etc/systemd/system/fake-hwclock.service.d/sa02m-save-onstop.conf <<FHC
[Service]
ExecStop=${FHC_BIN} save
RemainAfterExit=yes
FHC
fi
sa02m_systemctl daemon-reload >>"$LOG_FILE" 2>&1 || true
sa02m_systemctl enable fake-hwclock.service >>"$LOG_FILE" 2>&1 || true
# Запишем текущее время как fallback (если оно валидное).
if [ "$(date +%Y)" -ge 2024 ]; then
    fake-hwclock save 2>/dev/null || true
fi

# Chrony: лёгкий NTP-клиент. Без интернета не повредит — просто будет
# unsynced, время возьмётся из RTC/fake-hwclock.
if ! command -v chronyd >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get -y install chrony >>"$LOG_FILE" 2>&1 || true
fi
install -d -m 755 /etc/chrony/sources.d
cat > /etc/chrony/sources.d/sa02m.sources <<'NTP'
# SA-02m NTP sources
pool pool.ntp.org iburst maxsources 4
pool ru.pool.ntp.org iburst maxsources 4
server time.cloudflare.com iburst
NTP
if [ -f /etc/chrony/chrony.conf ]; then
    grep -qE '^[[:space:]]*sourcedir[[:space:]]+/etc/chrony/sources.d' /etc/chrony/chrony.conf \
        || echo 'sourcedir /etc/chrony/sources.d' >> /etc/chrony/chrony.conf
    grep -qE '^[[:space:]]*rtcsync'  /etc/chrony/chrony.conf || echo 'rtcsync'  >> /etc/chrony/chrony.conf
    grep -qE '^[[:space:]]*makestep' /etc/chrony/chrony.conf || echo 'makestep 1.0 3' >> /etc/chrony/chrony.conf
fi
sa02m_systemctl unmask chrony.service chronyd.service 2>/dev/null || true
sa02m_systemctl enable --now chrony.service 2>/dev/null \
    || sa02m_systemctl enable --now chronyd.service 2>/dev/null || true

# Если shadow-дата root в будущем (из-за прежних сбоев RTC) — выравниваем,
# иначе PAM пишет "account root has password changed in future" при каждом
# SSH-логине и эту запись потом сложно отличить от реальной проблемы.
ROOT_LAST=$(awk -F: '/^root:/{print $3}' /etc/shadow)
CUR_DAYS=$(( $(date +%s) / 86400 ))
if [ -n "$ROOT_LAST" ] && [ "$ROOT_LAST" -gt "$CUR_DAYS" ]; then
    log INFO "shadow root lastchange=$ROOT_LAST > today=$CUR_DAYS, выравниваем"
    chage -d "$(date +%Y-%m-%d)" root 2>/dev/null || true
fi

# ── DNS fallback через resolvconf ───────────────────────────────────────────
# ifupdown обновляет /etc/resolv.conf через resolvconf при поднятии eth0.
# При раздаче интернета с ПК (ICS) внешние DNS (8.8.8.8) часто недоступны —
# первым nameserver должен быть IP шлюза (ПК). Затем публичные DNS как запасной вариант.
if command -v resolvconf >/dev/null 2>&1 || [ -d /etc/resolvconf/resolv.conf.d ]; then
    mkdir -p /etc/resolvconf/resolv.conf.d
    GW_DNS="$(ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="via") {print $(i+1); exit}}')"
    if [ -n "$GW_DNS" ]; then
        cat > /etc/resolvconf/resolv.conf.d/head <<EOF
# SA-02m: DNS через шлюз (ICS / раздача интернета с ПК)
nameserver ${GW_DNS}
EOF
        log OK "DNS через шлюз ${GW_DNS} (resolvconf head)"
    fi
    cat > /etc/resolvconf/resolv.conf.d/base <<'DNS'
# SA-02m fallback DNS (если шлюз не отвечает как DNS)
nameserver 8.8.8.8
nameserver 8.8.4.4
DNS
    resolvconf -u 2>/dev/null || true
    log OK "DNS fallback 8.8.8.8/8.8.4.4 настроен через resolvconf"
fi

# Применить изменения PID1 без перезагрузки.
sa02m_systemctl daemon-reexec 2>/dev/null || true

log OK "=== [01] Системная настройка завершена ==="
