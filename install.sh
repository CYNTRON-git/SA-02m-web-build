#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  Installer  v1.0.3
# Дата: 2026
# Использование: sudo ./install.sh [--ip X.X.X.X] [--port 9999] [--pass cyntron]
#                                  [--canonical-iface-now] [--no-gw-repair]
#   --no-gw-repair (или SA02M_SKIP_GW_REPAIR=1) — не дописывать отсутствующие
#   gateway/dns-nameservers в существующий /etc/network/interfaces.d/ethN.conf.
#   Для сети, где шлюза нет намеренно (изолированный сегмент).
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
# Opt-in: rename end0/end1 -> eth0/eth1 during THIS run instead of at the next
# boot. Renaming a live interface drops the management link (SSH), so a board
# without console access must always use the deferred default.
export SA02M_CANONICAL_IFACE_NOW="${SA02M_CANONICAL_IFACE_NOW:-0}"
# Opt-out: 02-network.sh дописывает ОТСУТСТВУЮЩИЕ gateway/dns-nameservers в уже
# существующий конфиг интерфейса. Оператору изолированного сегмента, где шлюза
# нет намеренно, нужен документированный выключатель, а не спор с установщиком
# на каждом обновлении.
export SA02M_SKIP_GW_REPAIR="${SA02M_SKIP_GW_REPAIR:-0}"
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
        --canonical-iface-now) SA02M_CANONICAL_IFACE_NOW="1"; shift ;;
        --no-gw-repair) SA02M_SKIP_GW_REPAIR="1"; shift ;;
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
# Bus-free RS-485 module-roster aggregator (reads flasher scan cache + MQTT bridge).
bash "$SCRIPT_DIR/scripts/10-rs485-roster.sh"

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

# Devices tab (DTV / CE-02m-3 widgets + history) — after MQTT so cache path exists
if [ "${SA02M_SKIP_DEVICES:-0}" != "1" ] && [ -f "$SCRIPT_DIR/scripts/11-devices.sh" ]; then
    log INFO "──── Устройства ДТВ/СЭ: API + logger ────"
    bash "$SCRIPT_DIR/scripts/11-devices.sh" || log WARN "11-devices.sh завершился с ошибкой"
fi

if [ "${SA02M_SKIP_GATEWAY:-0}" != "1" ] && [ -f "$SCRIPT_DIR/scripts/06-gateway.sh" ]; then
    log INFO "──── Опциональный стек: sa02m-serial-gateway ────"
    bash "$SCRIPT_DIR/scripts/06-gateway.sh" || log WARN "06-gateway.sh завершился с ошибкой"
fi

if [ "${SA02M_SKIP_ALICE:-0}" != "1" ] && [ -f "$SCRIPT_DIR/scripts/06-alice.sh" ]; then
    log INFO "──── Опциональный стек: Яндекс Алиса (sa02m-alice) ────"
    bash "$SCRIPT_DIR/scripts/06-alice.sh" || log WARN "06-alice.sh завершился с ошибкой"
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

    # Настройка Docker зависит от возможностей ядра, а не от его версии: ядро
    # с CONFIG_OVERLAY_FS + CONFIG_BRIDGE + CONFIG_NF_TABLES получает полноценный
    # режим (overlay2 storage + iptables-nft + bridge networking), любое другое —
    # minimal-mode (vfs + iptables=false + bridge=none). Штатные ядра флота
    # (6.1.0-rc6 / 6.1.0-rc6-rt4) собраны с этим набором — 1.0.5.58.
    # NOTE: this config-grep predicate is the legacy half of the check; the
    # RUNTIME gate is the ExecCondition capability probe installed by
    # 01-system.sh (/etc/systemd/system/docker.service.d/sa02m-kernel-guard.conf
    # → sa02m-kernel-service-guard.sh docker-capable). The grep misses
    # NFT_COMPAT and mis-detected the 6.1.0-rc6 bench kernel as full-mode
    # (docs/contracts/kernel-conditional-services.md).
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

# ── Migration: sa02m-mqtt-opcua northbound gateway port 4840 → 4841 ────────
# CODESYS's own OPC UA server owns the IANA port 4840 (vendor-fixed); our
# gateway moves to 4841 (docs/contracts/kernel-conditional-services.md).
# Deliberately UNCONDITIONAL for port==4840: a conf still on 4840 is exactly
# the EADDRINUSE conflict class this release removes (CHANGELOG 1.0.5.57).
# Idempotent (port != 4840 → untouched); every other key (user `groups`
# config) is preserved via a json round-trip + atomic replace; a failed
# migration leaves the conf as-is and is logged — the gateway then keeps
# crash-looping on 4840, visible in the services UI, never silent.
OPCUA_CONF=/etc/sa02m-mqtt-opcua.conf
if [ -f "$OPCUA_CONF" ] && command -v python3 >/dev/null 2>&1; then
    _mig=$(python3 - "$OPCUA_CONF" 2>>"$LOG_FILE" <<'PYMIG'
import json, os, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
except Exception as e:
    sys.stderr.write("opcua conf parse failed: %s\n" % e)
    sys.exit(1)
opcua = cfg.get("opcua")
# Also match a hand-edited string "4840" — the driver would still int() it.
if isinstance(opcua, dict) and opcua.get("port") in (4840, "4840"):
    opcua["port"] = 4841
    tmp = path + ".sa02m-mig.tmp"
    st = os.stat(path)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    # The conf carries mqtt.password: preserve the original mode/owner across
    # the atomic replace (a tightened 0600 must not widen to umask 0644).
    os.chmod(tmp, st.st_mode & 0o7777)
    if hasattr(os, "chown"):
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)
    print("changed")
else:
    print("unchanged")
PYMIG
) || _mig="error"
    case "$_mig" in
        changed)
            log OK "sa02m-mqtt-opcua: порт 4840 → 4841 в $OPCUA_CONF (4840 занят OPC UA-сервером CODESYS)"
            systemctl try-restart sa02m-mqtt-opcua >> "$LOG_FILE" 2>&1 || true
            ;;
        unchanged)
            : ;;
        *)
            log WARN "sa02m-mqtt-opcua: не удалось мигрировать порт в $OPCUA_CONF — конфиг не изменён, см. $LOG_FILE"
            ;;
    esac
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
