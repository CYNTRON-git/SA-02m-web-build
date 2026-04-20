#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# 04-flasher.sh  •  Установка демона sa02m-flasher (RS-485/MR-02m)
# ═══════════════════════════════════════════════════════════════════════════
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [04] Установка sa02m-flasher ==="

BASE_DIR="$SCRIPT_DIR/.."
ETC_DIR="$BASE_DIR/etc"
OPT_DIR="$BASE_DIR/opt/sa02m-flasher"

INSTALL_DIR="/opt/sa02m-flasher"
CACHE_DIR="/var/lib/sa02m-flasher/firmware"
LOG_DIR="/var/log/sa02m-flasher"
FLASHER_USER="sa02m-flasher"

# ── Системные зависимости ─────────────────────────────────────────────────
for pkg in python3 python3-venv python3-pip python3-serial psmisc sudo; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        log INFO "apt install $pkg"
        DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg" >> "$LOG_FILE" 2>&1 || \
            log WARN "Не удалось установить $pkg (продолжаем: pyserial может быть через venv)"
    fi
done

# ── Пользователь и группы ─────────────────────────────────────────────────
if ! id "$FLASHER_USER" >/dev/null 2>&1; then
    log INFO "Создаю системного пользователя $FLASHER_USER"
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$FLASHER_USER"
fi
usermod -aG dialout "$FLASHER_USER" >/dev/null 2>&1 || true
# Для доступа к /run/sa02m-flasher.sock из www-data (nginx) — общая группа.
usermod -aG www-data "$FLASHER_USER" >/dev/null 2>&1 || true

# ── Каталоги ──────────────────────────────────────────────────────────────
install -d -m 0755 -o "$FLASHER_USER" -g "$FLASHER_USER" "$INSTALL_DIR"
install -d -m 0755 -o "$FLASHER_USER" -g "$FLASHER_USER" "$CACHE_DIR"
install -d -m 0750 -o "$FLASHER_USER" -g "$FLASHER_USER" "$LOG_DIR"

# ── Код демона ────────────────────────────────────────────────────────────
log INFO "Копирую $OPT_DIR → $INSTALL_DIR"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
    "$OPT_DIR/" "$INSTALL_DIR/"
chown -R "$FLASHER_USER":"$FLASHER_USER" "$INSTALL_DIR"
find "$INSTALL_DIR" -type d -exec chmod 0755 {} \;
find "$INSTALL_DIR" -type f -exec chmod 0644 {} \;

# ── /etc конфигурация ────────────────────────────────────────────────────
if [ ! -f /etc/sa02m_flasher.conf ]; then
    log INFO "Создаю /etc/sa02m_flasher.conf"
    install -m 0640 -o root -g "$FLASHER_USER" "$ETC_DIR/sa02m_flasher.conf" /etc/sa02m_flasher.conf
else
    log INFO "/etc/sa02m_flasher.conf уже существует — оставляю без изменений"
fi

# sudoers для управления службами/fuser
install -m 0440 -o root -g root "$ETC_DIR/sudoers.d/sa02m-flasher" /etc/sudoers.d/sa02m-flasher
visudo -cf /etc/sudoers.d/sa02m-flasher >> "$LOG_FILE" 2>&1 && log OK "sudoers flasher OK" \
    || log WARN "visudo не принял sudoers.d/sa02m-flasher — проверьте вручную"

# logrotate
install -m 0644 -o root -g root "$ETC_DIR/logrotate.d/sa02m-flasher" /etc/logrotate.d/sa02m-flasher

# ── systemd unit ──────────────────────────────────────────────────────────
log INFO "Устанавливаю systemd unit sa02m-flasher.service"
install -m 0644 -o root -g root "$ETC_DIR/sa02m-flasher.service" /etc/systemd/system/sa02m-flasher.service
systemctl daemon-reload
systemctl enable sa02m-flasher.service >> "$LOG_FILE" 2>&1 || log WARN "enable sa02m-flasher не удался"
systemctl restart sa02m-flasher.service >> "$LOG_FILE" 2>&1 && log OK "sa02m-flasher запущен" \
    || log WARN "sa02m-flasher не стартовал (journalctl -u sa02m-flasher -n 100)"

# ── Проверки ──────────────────────────────────────────────────────────────
sleep 1
if [ -S /run/sa02m-flasher.sock ]; then
    log OK "Unix-сокет /run/sa02m-flasher.sock создан"
else
    log WARN "Сокет /run/sa02m-flasher.sock не создан — смотрите journalctl"
fi

log OK "=== [04] sa02m-flasher установлен ==="
