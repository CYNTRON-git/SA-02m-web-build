#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  Installer  v1.0.3
# Дата: 2026
# Использование: sudo ./install.sh [--ip X.X.X.X] [--port 9999] [--pass cyntron]
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LOG_FILE="/var/log/sa02m_install.log"

# ── Parse arguments ────────────────────────────────────────────────────────
export NETMASK="255.255.255.0"
export DNS_SERVERS="77.88.8.8 77.88.8.1"
export NET_IFACE="eth0"
export PORT="9999"
export WEB_ROOT="/var/www/network_config"
export ADMIN_PASS="cyntron"
export SA02M_SERIAL_PROFILE=""
export SA02M_HW_VARIANT=""
# IP_ADDRESS and GATEWAY are resolved after lib.sh is sourced (variant-aware defaults)
_ARG_IP=""
_ARG_GW=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --ip)      _ARG_IP="$2";             shift 2 ;;
        --mask)    NETMASK="$2";             shift 2 ;;
        --gw)      _ARG_GW="$2";             shift 2 ;;
        --port)    PORT="$2";                shift 2 ;;
        --pass)    ADMIN_PASS="$2";          shift 2 ;;
        --serial-profile) SA02M_SERIAL_PROFILE="$2"; shift 2 ;;
        --variant) SA02M_HW_VARIANT="$2";   shift 2 ;;
        *)         shift ;;
    esac
done

# ── Init log ───────────────────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG_FILE")"
echo "──────────────────────────────────────────" >> "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') Установка СА-02м начата" >> "$LOG_FILE"

source "$SCRIPT_DIR/scripts/lib.sh"
check_root

# Persist variant if explicitly provided, then resolve IP/GW defaults
if [ -n "$SA02M_HW_VARIANT" ]; then
    printf 'SA02M_HW_VARIANT=%s\n' "$SA02M_HW_VARIANT" > /etc/sa02m_hw_variant.conf
    chmod 644 /etc/sa02m_hw_variant.conf
fi
export IP_ADDRESS="${_ARG_IP:-$(sa02m_default_ip)}"
export GATEWAY="${_ARG_GW:-$(sa02m_default_gw)}"
# Whether the operator passed --ip explicitly. 02-network.sh only rewrites an
# EXISTING eth0.conf when this is set — otherwise a re-run (the upgrade path)
# would reset a device the operator gave a static IP via the web UI back to the
# factory default and make it unreachable.
export IP_EXPLICIT=$([ -n "$_ARG_IP" ] && echo 1 || echo 0)
HW_VARIANT=$(sa02m_hw_variant)

echo ""
# Derive the banner version from the shipped web VERSION file (single source),
# so it never drifts stale like the former hardcoded v1.0.3.
_WEB_VER=$(grep -vE '^\s*#' "$SCRIPT_DIR/www/network_config/VERSION" 2>/dev/null | grep -m1 -oE '[0-9]+(\.[0-9]+){2,3}')
echo "  ╔══════════════════════════════════════╗"
printf "  ║   СА-02м  Installer  v%-16s ║\n" "${_WEB_VER:-?}"
echo "  ╚══════════════════════════════════════╝"
echo ""
echo "  Вариант: $HW_VARIANT"
echo "  IP    : $IP_ADDRESS"
echo "  Шлюз  : $GATEWAY"
echo "  PORT  : $PORT"
echo "  LOG   : $LOG_FILE"
echo ""

# ── Run modules ────────────────────────────────────────────────────────────
bash "$SCRIPT_DIR/scripts/01-system.sh"
bash "$SCRIPT_DIR/scripts/02-network.sh"
bash "$SCRIPT_DIR/scripts/03-webserver.sh"
bash "$SCRIPT_DIR/scripts/04-flasher.sh"
bash "$SCRIPT_DIR/scripts/05-cloud-agent.sh"

# ── Optional stacks (MQTT / Gateway / Node-RED / CODESYS / MPLC / Docker) ─
# Можно отключить по-отдельности:
#   SA02M_SKIP_MQTT=1 SA02M_SKIP_GATEWAY=1 SA02M_SKIP_NODERED=1 \
#   SA02M_SKIP_CODESYS=1 SA02M_SKIP_MPLC=1 SA02M_SKIP_DOCKER=1 ./install.sh
# По умолчанию — устанавливаем всё, чтобы после первой прошивки был полный стек.
# CODESYS/MPLC ставятся только если найден vendor-payload (см. docs/vendor-integrations.md);
# отсутствие payload'а не считается ошибкой — шаг просто пропускается.

if [ "${SA02M_SKIP_MQTT:-0}" != "1" ] && [ -f "$SCRIPT_DIR/scripts/05-mqtt.sh" ]; then
    log INFO "──── Опциональный стек: MQTT (mosquitto + мосты) ────"
    bash "$SCRIPT_DIR/scripts/05-mqtt.sh" || log WARN "05-mqtt.sh завершился с ошибкой"
fi

if [ "${SA02M_SKIP_GATEWAY:-0}" != "1" ] && [ -f "$SCRIPT_DIR/scripts/06-gateway.sh" ]; then
    log INFO "──── Опциональный стек: sa02m-serial-gateway ────"
    bash "$SCRIPT_DIR/scripts/06-gateway.sh" || log WARN "06-gateway.sh завершился с ошибкой"
fi

if [ "${SA02M_SKIP_NODERED:-0}" != "1" ] && [ -f "$SCRIPT_DIR/scripts/07-nodered.sh" ]; then
    log INFO "──── Опциональный стек: Node-RED ────"
    bash "$SCRIPT_DIR/scripts/07-nodered.sh" || log WARN "07-nodered.sh завершился с ошибкой"
fi

if [ "${SA02M_SKIP_CODESYS:-0}" != "1" ] && [ -f "$SCRIPT_DIR/scripts/08-codesys.sh" ]; then
    log INFO "──── Опциональный стек: CODESYS Control (SL, armhf) ────"
    bash "$SCRIPT_DIR/scripts/08-codesys.sh" || log WARN "08-codesys.sh завершился с ошибкой"
fi

if [ "${SA02M_SKIP_MPLC:-0}" != "1" ] && [ -f "$SCRIPT_DIR/scripts/09-mplc.sh" ]; then
    log INFO "──── Опциональный стек: MasterSCADA MPLC 4D Runtime ────"
    bash "$SCRIPT_DIR/scripts/09-mplc.sh" || log WARN "09-mplc.sh завершился с ошибкой"
fi

if [ "${SA02M_SKIP_DOCKER:-0}" != "1" ]; then
    log INFO "──── Опциональный стек: Docker CE (docker.io) ────"
    if ! command -v docker >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose >> "$LOG_FILE" 2>&1 \
            && log OK "docker.io + docker-compose установлены" \
            || log WARN "docker.io не установлен — проверьте apt sources"
    else
        log INFO "docker уже установлен: $(docker --version 2>/dev/null | head -1)"
    fi

    # Настройка Docker зависит от ядра. Начиная с SA-02м kernel 5.10.35 (без
    # суффикса) собирается с CONFIG_OVERLAY_FS=y + CONFIG_BRIDGE=y + CONFIG_NF_TABLES=y
    # → доступен полноценный режим: overlay2 storage + iptables-nft + bridge networking.
    # На старом ядре (5.10.35-sa02m+ / любое ядро без OVERLAY_FS/BRIDGE/NF_TABLES)
    # автоматически откатываемся на minimal-mode (vfs + iptables=false + bridge=none).
    if command -v docker >/dev/null 2>&1; then
        DOCKER_MODE=full
        KERNEL_CFG="/boot/config-$(uname -r)"
        for req in CONFIG_OVERLAY_FS CONFIG_BRIDGE CONFIG_NF_TABLES; do
            if [ -f "$KERNEL_CFG" ]; then
                if ! grep -qE "^${req}=[ym]" "$KERNEL_CFG"; then
                    DOCKER_MODE=minimal
                    log WARN "kernel $(uname -r): $req отсутствует → Docker minimal-mode"
                    break
                fi
            fi
        done

        mkdir -p /etc/docker
        if [ "$DOCKER_MODE" = "full" ]; then
            update-alternatives --set iptables  /usr/sbin/iptables-nft  >> "$LOG_FILE" 2>&1 || true
            update-alternatives --set ip6tables /usr/sbin/ip6tables-nft >> "$LOG_FILE" 2>&1 || true

            if [ ! -f /etc/docker/daemon.json ] || grep -q '"storage-driver": "vfs"' /etc/docker/daemon.json 2>/dev/null; then
                cat > /etc/docker/daemon.json <<'DOCKER_JSON'
{
  "storage-driver": "overlay2",
  "iptables": true,
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
DOCKER_JSON
                log INFO "docker daemon.json: full-mode (overlay2 + iptables-nft + bridge)"
            fi
        else
            update-alternatives --set iptables  /usr/sbin/iptables-legacy  >> "$LOG_FILE" 2>&1 || true
            update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy >> "$LOG_FILE" 2>&1 || true

            if [ ! -f /etc/docker/daemon.json ] || ! grep -q '"storage-driver"' /etc/docker/daemon.json; then
                cat > /etc/docker/daemon.json <<'DOCKER_JSON'
{
  "storage-driver": "vfs",
  "iptables": false,
  "bridge": "none",
  "log-driver": "journald"
}
DOCKER_JSON
                log INFO "docker daemon.json: minimal-mode (vfs, без iptables/bridge) — старое ядро"
            fi
        fi

        systemctl reset-failed docker 2>/dev/null || true
        systemctl enable --now docker >> "$LOG_FILE" 2>&1 && log OK "docker.service активен ($DOCKER_MODE-mode)" \
            || log WARN "docker.service не стартует — journalctl -u docker -n 30"
    fi
fi

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
log OK "════════════════════════════════════════"
log OK " Установка завершена!"
log OK " URL  : http://${IP_ADDRESS}:${PORT}"
log OK " Логин: admin / ${ADMIN_PASS}"
log OK "════════════════════════════════════════"
echo ""

# ── Check services ─────────────────────────────────────────────────────────
if [ -z "${SA02M_ROOTFS_BUILD:-}" ]; then
    # Базовые сервисы всегда обязательные
    for svc in nginx fcgiwrap sa02m-flasher sa02m-cloud-agent sa02m-pre-start; do
        if systemctl is-active "$svc" &>/dev/null || systemctl is-enabled "$svc" &>/dev/null; then
            log OK " ✓ $svc установлен"
        else
            log WARN " ✗ $svc не установлен!"
        fi
    done
    # Опциональные — только если не пропущены
    for svc in mosquitto sa02m-serial-gateway nodered codesyscontrol mplc4 docker; do
        if systemctl list-unit-files "${svc}.service" 2>/dev/null | grep -q "^${svc}.service"; then
            state=$(systemctl is-active "$svc" 2>/dev/null || echo "inactive")
            log INFO "   $svc: $state"
        fi
    done
    echo ""
fi
