#!/bin/bash
set -o pipefail  # catch masked failures in pipes (Y7); set -u deferred pending on-device install test
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
SCAN_CACHE_DIR="/var/lib/sa02m-flasher/last_scan"
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

# ── Каталоги ──────────────────────────────────────────────────────────────
install -d -m 0755 -o "$FLASHER_USER" -g "$FLASHER_USER" "$INSTALL_DIR"
install -d -m 0755 -o "$FLASHER_USER" -g "$FLASHER_USER" "$CACHE_DIR"
# Persistent last-scan roster cache — written by the flasher, read (0755) by the
# bus-free sa02m-rs485-roster aggregator. install -d is idempotent (re-run safe).
install -d -m 0755 -o "$FLASHER_USER" -g "$FLASHER_USER" "$SCAN_CACHE_DIR"
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
    if grep -q '^SOCKET_PATH=/run/sa02m-flasher\.sock$' /etc/sa02m_flasher.conf; then
        log INFO "Миграция SOCKET_PATH → /run/sa02m-flasher/flasher.sock"
        sed -i 's|^SOCKET_PATH=/run/sa02m-flasher\.sock$|SOCKET_PATH=/run/sa02m-flasher/flasher.sock|' /etc/sa02m_flasher.conf
    fi
fi

# sudoers для управления службами/fuser (единый дом хардненинга — lib.sh)
sa02m_install_sudoers "$ETC_DIR/sudoers.d/sa02m-flasher" /etc/sudoers.d/sa02m-flasher

# logrotate
install -m 0644 -o root -g root "$ETC_DIR/logrotate.d/sa02m-flasher" /etc/logrotate.d/sa02m-flasher
# CRLF валит logrotate на каждой загрузке — это уже происходило именно с этим
# файлом (BUGLOG [2026-06-03 09:40]). В репозитории закрыто .gitattributes,
# здесь — страховка на случай checkout'а без него.
sed -i 's/\r$//' /etc/logrotate.d/sa02m-flasher

# ── systemd unit ──────────────────────────────────────────────────────────
# Capture prior state BEFORE (re)installing the unit: an absent unit (empty
# is-enabled) marks a FIRST install → apply the default-on; an existing device
# has its enabled/active state PRESERVED — never force-start a flasher the
# operator deliberately stopped, and never re-enable a disabled one.
read -r _FL_PREV_EN _FL_PREV_ACT < <(sa02m_capture_svc_state sa02m-flasher.service)
log INFO "Устанавливаю systemd unit sa02m-flasher.service"
install -m 0644 -o root -g root "$ETC_DIR/sa02m-flasher.service" /etc/systemd/system/sa02m-flasher.service
systemctl daemon-reload
_FL_EXPECT_UP=0
if [ -z "$_FL_PREV_EN" ] || [ "$_FL_PREV_EN" = unknown ]; then
    # first install → default on (enable + start on fresh code)
    systemctl enable sa02m-flasher.service >> "$LOG_FILE" 2>&1 || log WARN "enable sa02m-flasher не удался"
    systemctl restart sa02m-flasher.service >> "$LOG_FILE" 2>&1 && log OK "sa02m-flasher запущен" \
        || log WARN "sa02m-flasher не стартовал (journalctl -u sa02m-flasher -n 100)"
    _FL_EXPECT_UP=1
else
    # upgrade → restore EXACTLY the prior state, refreshing code if it was active
    sa02m_restore_svc_state sa02m-flasher.service "$_FL_PREV_EN" "$_FL_PREV_ACT" refresh
    if [ "$_FL_PREV_ACT" = active ]; then
        log OK "sa02m-flasher перезапущен на свежем коде (был активен)"
        _FL_EXPECT_UP=1
    else
        log INFO "sa02m-flasher: прежнее состояние сохранено (en=$_FL_PREV_EN act=$_FL_PREV_ACT) — не форсируем запуск"
    fi
fi

# ── Проверки ──────────────────────────────────────────────────────────────
# Только когда флэшер должен быть запущен (первый install или был активен) —
# иначе отсутствие сокета штатно (оператор остановил службу), не WARN.
if [ "$_FL_EXPECT_UP" = 1 ]; then
    for _ in $(seq 1 10); do
        [ -S /run/sa02m-flasher/flasher.sock ] && break
        sleep 1
    done
    if [ -S /run/sa02m-flasher/flasher.sock ]; then
        log OK "Unix-сокет /run/sa02m-flasher/flasher.sock создан"
    else
        log WARN "Сокет /run/sa02m-flasher/flasher.sock не создан — смотрите journalctl"
    fi
fi

log OK "=== [04] sa02m-flasher установлен ==="
