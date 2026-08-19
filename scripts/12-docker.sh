#!/bin/bash
set -o pipefail  # catch masked failures in pipes (Y7); set -u deferred pending on-device install test
# ═══════════════════════════════════════════════════════════════════════════
# 12-docker.sh  •  Docker CE (docker.io) на СА-02м — third-party stack module
#
# Moved verbatim from install.sh (the docker block) and gated by the stack
# policy like the other third-party modules (07/08/09): the verdict is read
# FIRST, before any apt or systemctl. Refresh semantics:
#   skip-disabled — the operator removed/refused docker: never reinstalled;
#   skip-absent   — not installed and this is a refresh: never installed;
#   overlay       — installed: refresh only the sa02m-owned overlay
#                   (daemon.json + iptables alternatives), preserve the
#                   operator's unit state (capture/apply, norestart — a docker
#                   restart kills containers and no sa02m code lives in it);
#   install       — full mode / --with-optional: packages via the thirdparty
#                   tier, then the same overlay + unit apply.
#
# Отключить: SA02M_SKIP_DOCKER=1 ./install.sh
# Контракт: docs/contracts/installer-refresh-policy.md;
# kernel-политика (ExecCondition guard): docs/contracts/kernel-conditional-services.md.
# ═══════════════════════════════════════════════════════════════════════════
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [12] Установка Docker CE (docker.io) ==="

_VERDICT=$(sa02m_stack_verdict DOCKER)
case "$_VERDICT" in
    skip-disabled)
        log INFO "Docker: отключён оператором (/etc/sa02m_stacks.conf) — пропуск; вернуть: --with-optional"
        exit 0
        ;;
    skip-absent)
        log INFO "refresh: Docker не установлен — не ставлю (--with-optional — поставить)"
        exit 0
        ;;
esac

if [ "$_VERDICT" = install ] && ! command -v docker >/dev/null 2>&1; then
    sa02m_apt_update
    sa02m_pkg_install_tier thirdparty docker.io docker-compose
    if command -v docker >/dev/null 2>&1; then
        log OK "docker.io + docker-compose установлены"
    else
        log WARN "docker.io не установлен — проверьте apt sources"
    fi
elif command -v docker >/dev/null 2>&1; then
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
    # The unit's run-state belongs to the operator: capture BEFORE the overlay
    # lands, apply after (first install ⇒ on; docker gets `norestart` — see the
    # header).
    sa02m_svc_capture docker.service

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
    sa02m_svc_apply docker.service app on norestart --stack=DOCKER
    case "$SA02M_SVC_LAST_RESULT" in
        started) log OK "docker.service активен ($DOCKER_MODE-mode)" ;;
    esac
    sa02m_stack_policy_set DOCKER present || true
else
    log WARN "docker недоступен — daemon.json/юнит не настраиваю"
fi

log OK "=== [12] Docker: модуль завершён ==="
