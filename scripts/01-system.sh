#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# 01-system.sh  •  Base OS, users, packages, serial symlinks
# ═══════════════════════════════════════════════════════════════════════════
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [01] Системная настройка ==="

# ── Locale & timezone ──────────────────────────────────────────────────────
log INFO "Настройка локали и таймзоны"
locale-gen ru_RU.UTF-8 en_US.UTF-8 >> "$LOG_FILE" 2>&1 || true
update-locale LANG=ru_RU.UTF-8 >> "$LOG_FILE" 2>&1 || true
timedatectl set-timezone Europe/Moscow >> "$LOG_FILE" 2>&1 || true

# ── Required packages ──────────────────────────────────────────────────────
log INFO "Установка пакетов"
apt-get update -qq >> "$LOG_FILE" 2>&1
pkg_install nginx fcgiwrap openssl net-tools psmisc

# ── User hmi ──────────────────────────────────────────────────────────────
if ! id hmi &>/dev/null; then
    log INFO "Создание пользователя hmi"
    useradd -m -s /bin/bash hmi >> "$LOG_FILE" 2>&1
fi

# ── Disable serial console getty ─────────────────────────────────────────
for tty in ttyS0 ttyS1; do
    systemctl disable "serial-getty@${tty}" 2>/dev/null || true
    systemctl mask    "serial-getty@${tty}" 2>/dev/null || true
done

# ── RS-485 / COM symlinks ──────────────────────────────────────────────────
log INFO "Создание симлинков RS-485 / COM"
declare -A LINKS=(
    [RS-485-0]=ttyS0  [COM1]=ttyS0
    [RS-485-1]=ttyS3  [COM2]=ttyS3
    [RS-485-2]=ttyS4  [COM3]=ttyS4
    [RS-485-3]=ttyS5  [COM4]=ttyS5
    [RS-485-4]=ttyS7  [COM5]=ttyS7
)
for lnk in "${!LINKS[@]}"; do
    target="/dev/${LINKS[$lnk]}"
    [ -e "$target" ] && ln -sf "$target" "/dev/$lnk" && log OK "  /dev/$lnk → $target"
done

# ── Persist symlinks via udev ─────────────────────────────────────────────
UDEV_RULE="/etc/udev/rules.d/99-sa02m-serial.rules"
cat > "$UDEV_RULE" <<'UDEV'
KERNEL=="ttyS0", SYMLINK+="RS-485-0 COM1"
KERNEL=="ttyS3", SYMLINK+="RS-485-1 COM2"
KERNEL=="ttyS4", SYMLINK+="RS-485-2 COM3"
KERNEL=="ttyS5", SYMLINK+="RS-485-3 COM4"
KERNEL=="ttyS7", SYMLINK+="RS-485-4 COM5"
UDEV
udevadm control --reload-rules 2>/dev/null || true

# ── Mask unnecessary timers ────────────────────────────────────────────────
for unit in apt-daily.timer apt-daily-upgrade.timer; do
    systemctl mask "$unit" 2>/dev/null || true
done

log OK "=== [01] Системная настройка завершена ==="
