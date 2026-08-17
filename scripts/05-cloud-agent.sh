#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  05-cloud-agent.sh  —  Cloud Agent install (frpc edition)
# Устанавливает sa02m-cloud-agent (pairing + telemetry) и юнит туннеля
# sa02m-cloud-frpc. Активация: веб-UI устройства (вкладка «Облако», код
# сопряжения) или sa02m-cloud-activate --token <TOKEN> (fallback).
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
source "$(dirname "$0")/lib.sh"

AGENT_SRC="$(cd "$(dirname "$0")/.." && pwd)/opt/sa02m-cloud-agent"
AGENT_DST="/opt/sa02m-cloud-agent"
SYSTEMD_DIR="/etc/systemd/system"
FRPC_BIN="/usr/local/bin/frpc"

log INFO "── Cloud Agent: установка ──────────────────────────────────────────"

# ── 1. Копируем агент ────────────────────────────────────────────────────────
log INFO "Копирую агент в $AGENT_DST..."
mkdir -p "$AGENT_DST"
cp "$AGENT_SRC/sa02m-cloud-agent.py"    "$AGENT_DST/"
cp "$AGENT_SRC/sa02m-cloud-activate.py" "$AGENT_DST/"
chmod +x "$AGENT_DST/sa02m-cloud-agent.py"
chmod +x "$AGENT_DST/sa02m-cloud-activate.py"

# Symlink activate script в /usr/local/bin
ln -sf "$AGENT_DST/sa02m-cloud-activate.py" /usr/local/bin/sa02m-cloud-activate
log OK "Агент скопирован"

# ── 2. Systemd units ─────────────────────────────────────────────────────────
log INFO "Устанавливаю systemd units..."
cp "$AGENT_SRC/sa02m-cloud-agent.service" "$SYSTEMD_DIR/"
cp "$AGENT_SRC/sa02m-cloud-frpc.service"  "$SYSTEMD_DIR/"
systemctl daemon-reload
# Агент включаем сразу — до активации он в standby и ждёт код/токен.
# Туннель включает сам агент после enrollment (юнит гейтится frpc.toml).
systemctl enable sa02m-cloud-agent
log OK "Units установлены (агент ждёт активации)"

# ── 3. Замена ручных стендовых юнитов (прототип Фазы B) ─────────────────────
# На стенде туннель и heartbeat были подняты вручную (sa02m-frpc +
# sa02m-cloud-heartbeat); переработанный агент их полностью заменяет.
for unit in sa02m-frpc sa02m-cloud-heartbeat; do
    if systemctl list-unit-files "${unit}.service" &>/dev/null \
       && systemctl list-unit-files "${unit}.service" | grep -q "${unit}.service"; then
        log INFO "Отключаю ручной юнит ${unit} (заменён агентом)..."
        systemctl disable --now "${unit}" 2>/dev/null || true
    fi
done

# ── 4. Очистка WireGuard-эры (агент больше не использует WG) ────────────────
if [ -f /etc/wireguard/wg-cloud.conf ]; then
    log INFO "Убираю WireGuard-конфиг облака (wg-cloud)..."
    wg-quick down wg-cloud 2>/dev/null || true
    rm -f /etc/wireguard/wg-cloud.conf
    log OK "wg-cloud удалён"
fi

# ── 5. Конфиг-директория + web trigger (CGI пишет через sudo) ───────────────
mkdir -p /etc/sa02m-cloud
chmod 750 /etc/sa02m-cloud

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$REPO_ROOT/usr/local/sbin/sa02m-cloud-web-trigger.sh" ]; then
    install -m 755 "$REPO_ROOT/usr/local/sbin/sa02m-cloud-web-trigger.sh" \
        /usr/local/sbin/sa02m-cloud-web-trigger.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-cloud-web-trigger.sh
    log OK "sa02m-cloud-web-trigger.sh установлен"
fi
if [ -f "$REPO_ROOT/etc/sudoers.d/sa02m-cloud" ]; then
    sa02m_install_sudoers "$REPO_ROOT/etc/sudoers.d/sa02m-cloud" /etc/sudoers.d/sa02m-cloud
fi

# ── 6. frpc binary ───────────────────────────────────────────────────────────
if [ -x "$FRPC_BIN" ]; then
    log OK "frpc найден: $("$FRPC_BIN" --version 2>/dev/null || echo "$FRPC_BIN")"
else
    log WARN "frpc не найден ($FRPC_BIN) — облачный туннель не поднимется."
    log WARN "Установите frpc (static binary, linux_arm) и перезапустите агент."
fi

log OK "── Cloud Agent: готов ──────────────────────────────────────────────"
log INFO "Активация: веб-интерфейс → вкладка «Облако» → «Подключить к облаку»"
log INFO "  (или sa02m-cloud-activate --token <TOKEN> для наладчиков)"
