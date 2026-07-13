#!/bin/bash
set -o pipefail  # catch masked failures in pipes (Y7); set -u deferred pending on-device install test
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  05-cloud-agent.sh  —  Cloud Agent & WireGuard install
# Устанавливает sa02m-cloud-agent и wireguard-tools.
# Активация выполняется отдельно: sa02m-cloud-activate --token <TOKEN>
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
source "$(dirname "$0")/lib.sh"

AGENT_SRC="$(cd "$(dirname "$0")/.." && pwd)/opt/sa02m-cloud-agent"
AGENT_DST="/opt/sa02m-cloud-agent"
SYSTEMD_DIR="/etc/systemd/system"

log INFO "── Cloud Agent: установка ──────────────────────────────────────────"

# ── 1. wireguard-tools ───────────────────────────────────────────────────────
if ! command -v wg &>/dev/null; then
    log INFO "Устанавливаю wireguard-tools..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard-tools
    log OK "wireguard-tools установлен"
else
    log OK "wireguard-tools уже установлен ($(wg --version | head -1))"
fi

# ── 2. Копируем агент ────────────────────────────────────────────────────────
log INFO "Копирую агент в $AGENT_DST..."
mkdir -p "$AGENT_DST"
cp "$AGENT_SRC/sa02m-cloud-agent.py"    "$AGENT_DST/"
cp "$AGENT_SRC/sa02m-cloud-activate.py" "$AGENT_DST/"
chmod +x "$AGENT_DST/sa02m-cloud-agent.py"
chmod +x "$AGENT_DST/sa02m-cloud-activate.py"

# Symlink activate script в /usr/local/bin
ln -sf "$AGENT_DST/sa02m-cloud-activate.py" /usr/local/bin/sa02m-cloud-activate
log OK "Агент скопирован"

# ── 3. Systemd unit ──────────────────────────────────────────────────────────
log INFO "Устанавливаю systemd unit..."
cp "$AGENT_SRC/sa02m-cloud-agent.service" "$SYSTEMD_DIR/"
systemctl daemon-reload
# Включаем сразу — агент сам уйдёт в standby и дождётся токена
systemctl enable sa02m-cloud-agent
log OK "Unit установлен и включён (ждёт токен активации)"

# ── 4. Конфиг-директория ─────────────────────────────────────────────────────
mkdir -p /etc/sa02m-cloud
chmod 750 /etc/sa02m-cloud

# ── 5. WireGuard директория ──────────────────────────────────────────────────
mkdir -p /etc/wireguard
chmod 700 /etc/wireguard

log OK "── Cloud Agent: готов ──────────────────────────────────────────────"
log INFO "Для активации устройства выполните:"
log INFO "  sa02m-cloud-activate --token <TOKEN> --server cloud.cyntron.ru"
