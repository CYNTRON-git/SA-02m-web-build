#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  05-cloud-agent.sh  —  Cloud Agent install (frpc edition)
# Устанавливает sa02m-cloud-agent (pairing + telemetry) и юнит туннеля
# sa02m-cloud-frpc. Активация: веб-UI устройства (вкладка «Облако», код
# сопряжения) или sa02m-cloud-activate --token <TOKEN> (fallback).
#
# frpc — ОБЯЗАТЕЛЬНАЯ часть облачного агента (не опциональный сторонний стек,
# НЕ гейтится SA02M_SKIP_*): без него реверс-туннель не поднимается. §6 ставит
# его идемпотентно, 4 уровня (первый сработавший побеждает):
#   1. Уже стоит и работает           → пропуск (рабочий бинарь не перезаписываем)
#   2. Vendor-payload (оффлайн)        → /opt/vendor-installers/frpc/{frpc |
#                                        frp_*_linux_arm*.tar.gz}, проверка ARCH
#   3. Пиннутая загрузка (только онлайн)→ GitHub release frp ${FRP_VER}, sha256
#                                        извлечённого бинарника сверяется с пином
#                                        ДО установки; несовпадение → уровень 4
#   4. Ничего не сработало             → WARN + указатель на ручной комплект,
#                                        exit 0 (не ошибка установщика)
# Пин: FRP_VER=0.61.1, sha256(frpc)=55179be988a1987145f50ee36ef15ec37d06f1901d120bfb7b3ad091f7facd0a
# Источник истины по frpc (пин, payload-путь, URL, подготовка образа):
# docs/vendor-integrations.md → frpc. Ручной комплект для offline старых плат:
# out/sa02m-frpc-bundle.tar.gz (install-frpc.sh).
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
# Capture BEFORE the unit files land (first-install signal). An ACTIVE agent is
# restarted on the fresh code just copied — the stale-code class the MQTT
# bridge already fixed (1.0.5.73); a stopped/disabled one is preserved.
sa02m_svc_capture sa02m-cloud-agent.service
cp "$AGENT_SRC/sa02m-cloud-agent.service" "$SYSTEMD_DIR/"
cp "$AGENT_SRC/sa02m-cloud-frpc.service"  "$SYSTEMD_DIR/"
systemctl daemon-reload
# Первый раз агент только включаем (enable, без запуска) — до активации он в
# standby и ждёт код/токен. Туннель включает сам агент после enrollment
# (юнит гейтится frpc.toml).
sa02m_svc_apply sa02m-cloud-agent.service app enabled
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

# ── 6. frpc binary (ОБЯЗАТЕЛЬНАЯ часть агента — 4-уровневая идемпотентная установка)
# Порядок и пин задокументированы в шапке файла; источник истины —
# docs/vendor-integrations.md → frpc.
FRP_VER="0.61.1"
FRPC_SHA256="55179be988a1987145f50ee36ef15ec37d06f1901d120bfb7b3ad091f7facd0a"
FRPC_URL="https://github.com/fatedier/frp/releases/download/v${FRP_VER}/frp_${FRP_VER}_linux_arm.tar.gz"
VENDOR_FRPC_DIR="/opt/vendor-installers/frpc"
FRPC_DL_TIMEOUT_SEC="${SA02M_APT_TIMEOUT_SEC:-90}"

# sha256 of a file → stdout (empty on failure).
_frpc_sha256() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }

# Install <src> as $FRPC_BIN, 0755 root:root. Returns install's exit code.
_frpc_install_bin() { install -m 0755 -o root -g root "$1" "$FRPC_BIN"; }

# Host is ARMv7/ARMv6/armhf? (never lay down a wrong-arch static binary).
_frpc_arch_ok() {
    case "$(uname -m)" in
        armv7l|armv6l|armhf) return 0 ;;
        *) return 1 ;;
    esac
}

# 4-tier install. Sets $FRPC_INSTALLED_VIA to payload|download on a real install
# (empty when tier 1 finds a working binary). Returns 0 if frpc is present
# afterwards, 1 if it must WARN (tier 4). Called via `if install_frpc; then`, so
# `set -e` is disabled inside — tier fall-through is controlled by explicit rc.
install_frpc() {
    local tb tmp ex have

    # Tier 1: present + working → idempotent skip (never overwrite a good binary).
    if [ -x "$FRPC_BIN" ] && "$FRPC_BIN" --version >/dev/null 2>&1; then
        log OK "frpc уже установлен и работает: $("$FRPC_BIN" --version 2>/dev/null)"
        return 0
    fi

    # Tier 2: vendor payload (offline; golden image / prepared board).
    if [ -d "$VENDOR_FRPC_DIR" ]; then
        if _frpc_arch_ok; then
            if [ -f "$VENDOR_FRPC_DIR/frpc" ]; then
                if _frpc_install_bin "$VENDOR_FRPC_DIR/frpc"; then
                    log OK "frpc установлен из vendor-payload: $VENDOR_FRPC_DIR/frpc"
                    FRPC_INSTALLED_VIA=payload
                    return 0
                fi
                log WARN "frpc: не удалось установить $VENDOR_FRPC_DIR/frpc"
            fi
            for tb in "$VENDOR_FRPC_DIR"/frp_*_linux_arm*.tar.gz; do
                [ -f "$tb" ] || continue
                tmp=$(mktemp -d /tmp/frpc-payload.XXXXXX) || continue
                if tar -xzf "$tb" -C "$tmp" 2>/dev/null; then
                    ex=$(find "$tmp" -type f -name frpc | head -1)
                    if [ -n "$ex" ] && _frpc_install_bin "$ex"; then
                        log OK "frpc установлен из vendor-payload: $(basename "$tb")"
                        FRPC_INSTALLED_VIA=payload
                        rm -rf "$tmp"
                        return 0
                    fi
                fi
                rm -rf "$tmp"
                log WARN "frpc: не удалось извлечь/установить frpc из $(basename "$tb")"
            done
        else
            log WARN "frpc: vendor-payload есть, но архитектура $(uname -m) не ARM — пропускаю"
        fi
    fi

    # Tier 3: pinned download (online only; sha256 verified BEFORE install).
    # Arch-guarded like tier 2: the pin verifies content, not that an ARM binary
    # belongs on THIS host — a non-ARM invocation falls through to tier 4.
    if ! _frpc_arch_ok; then
        log WARN "frpc: архитектура $(uname -m) не ARM — загрузку пропускаю"
    elif sa02m_online; then
        if ! command -v curl >/dev/null 2>&1; then
            log WARN "frpc: curl отсутствует — загрузка невозможна"
        else
            tmp=$(mktemp -d /tmp/frpc-dl.XXXXXX) || tmp=""
            if [ -n "$tmp" ]; then
                if timeout "$FRPC_DL_TIMEOUT_SEC" curl -fsSL --max-time "$FRPC_DL_TIMEOUT_SEC" \
                       -o "$tmp/frp.tar.gz" "$FRPC_URL" 2>>"$LOG_FILE"; then
                    if tar -xzf "$tmp/frp.tar.gz" -C "$tmp" 2>/dev/null; then
                        ex=$(find "$tmp" -type f -name frpc | head -1)
                        if [ -n "$ex" ]; then
                            have=$(_frpc_sha256 "$ex")
                            if [ "$have" = "$FRPC_SHA256" ]; then
                                if _frpc_install_bin "$ex"; then
                                    log OK "frpc установлен загрузкой frp ${FRP_VER} (sha256 совпал с пином)"
                                    FRPC_INSTALLED_VIA=download
                                    rm -rf "$tmp"
                                    return 0
                                fi
                                log WARN "frpc: sha256 совпал, но install не удался"
                            else
                                log WARN "frpc: sha256 загруженного бинарника не совпал с пином (${have:-нет}) — НЕ устанавливаю"
                            fi
                        else
                            log WARN "frpc: в архиве frp ${FRP_VER} нет frpc — НЕ устанавливаю"
                        fi
                    else
                        log WARN "frpc: не удалось распаковать загруженный frp ${FRP_VER}"
                    fi
                else
                    log WARN "frpc: загрузка frp ${FRP_VER} не удалась (сеть/таймаут)"
                fi
                rm -rf "$tmp"
            fi
        fi
    else
        log INFO "frpc: сети нет — загрузку пропускаю (offline fast-path)"
    fi

    # Tier 4: nothing worked.
    return 1
}

FRPC_INSTALLED_VIA=""
if install_frpc; then
    # After a real install (payload/download) restart the agent so it picks up
    # the now-present binary — only if the device is already enrolled
    # (frpc.toml exists) and we are not in a chroot rootfs build. Not enrolled →
    # do nothing; the agent starts the tunnel itself after pairing.
    if [ -n "$FRPC_INSTALLED_VIA" ] && [ -z "${SA02M_ROOTFS_BUILD:-}" ] \
       && [ -f /etc/sa02m-cloud/frpc.toml ]; then
        log INFO "frpc установлен ($FRPC_INSTALLED_VIA), устройство привязано — перезапуск агента при необходимости"
        sa02m_svc_restart_if_active sa02m-cloud-agent.service
    fi
else
    log WARN "frpc не найден ($FRPC_BIN) — облачный туннель не поднимется."
    log WARN "Оффлайн старая плата: ручной комплект out/sa02m-frpc-bundle.tar.gz —"
    log WARN "  scp на плату, распаковать, 'bash install-frpc.sh' (docs/vendor-integrations.md → frpc)."
fi

log OK "── Cloud Agent: готов ──────────────────────────────────────────────"
log INFO "Активация: веб-интерфейс → вкладка «Облако» → «Подключить к облаку»"
log INFO "  (или sa02m-cloud-activate --token <TOKEN> для наладчиков)"
