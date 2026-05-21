#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# tools/system-hardening/install.sh
# ───────────────────────────────────────────────────────────────────────────
# Деплой userspace-watchdog + failure-monitor + serial-getty fallback на
# работающее устройство SA-02m. Идемпотентен — можно запускать многократно.
#
# Использование:
#   # На устройстве (ssh / serial root):
#   bash install.sh
#
#   # С хоста (WSL/Windows) — поток скриптов на устройство:
#   ssh root@192.168.1.136 "bash -s" < install.sh
#
# Что делает:
#   1. Останавливает и маскирует старый sa02m-watchdog-feed (bash-петля).
#   2. Кладёт drop-in /etc/systemd/system.conf.d/sa02m-watchdog.conf
#      (RuntimeWatchdogSec=15s, RebootWatchdogSec=2min) → PID1 владеет HW WDT.
#   3. Ставит /usr/local/sbin/sa02m-userspace-watchdog (health-checks).
#   4. Активирует sa02m-userspace-watchdog.service (forces reboot при сбоях).
#   5. Активирует sa02m-failure-monitor.service (логирование, без reboot).
#   6. Размаскирует и активирует serial-getty@ttyS0 (115200) для COM-fallback.
#   7. Применяет настройки systemd (daemon-reexec).
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: install.sh должен запускаться от root" >&2
    exit 1
fi

REPO_ROOT="${REPO_ROOT:-}"
if [ -z "$REPO_ROOT" ]; then
    if [ -d /mnt/c/Users/admin/Downloads/SA-02m-web-build ]; then
        REPO_ROOT=/mnt/c/Users/admin/Downloads/SA-02m-web-build
    else
        REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
    fi
fi

log() { printf '\n[install %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

log "REPO_ROOT=$REPO_ROOT"

if [ -d "$REPO_ROOT/etc" ]; then
    SRC_WATCHDOG="$REPO_ROOT/etc/sa02m-userspace-watchdog.sh"
    SRC_WATCHDOG_CONF="$REPO_ROOT/etc/sa02m_userspace_watchdog.conf"
    SRC_WATCHDOG_UNIT="$REPO_ROOT/etc/systemd/sa02m-userspace-watchdog.service"
    SRC_FAILURE_MON="$REPO_ROOT/etc/sa02m-failure-monitor.sh"
    SRC_FAILURE_UNIT="$REPO_ROOT/etc/sa02m-failure-monitor.service"
    SRC_WDT_CONF="$REPO_ROOT/etc/systemd/sa02m-watchdog.conf"
    LOCAL_FILES=1
else
    LOCAL_FILES=0
fi

log "[1/7] Стоп и mask старого sa02m-watchdog-feed"
systemctl stop sa02m-watchdog-feed.service 2>/dev/null || true
systemctl disable sa02m-watchdog-feed.service 2>/dev/null || true
if [ -f /etc/systemd/system/sa02m-watchdog-feed.service ]; then
    rm -f /etc/systemd/system/sa02m-watchdog-feed.service
fi
systemctl mask sa02m-watchdog-feed.service 2>/dev/null || true

log "[2/7] Drop-in system.conf.d/sa02m-watchdog.conf (15s/2min)"
mkdir -p /etc/systemd/system.conf.d
if [ "$LOCAL_FILES" = 1 ] && [ -r "$SRC_WDT_CONF" ]; then
    install -m 644 "$SRC_WDT_CONF" /etc/systemd/system.conf.d/sa02m-watchdog.conf
else
    cat > /etc/systemd/system.conf.d/sa02m-watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=15s
RebootWatchdogSec=2min
EOF
fi

log "[3/7] /usr/local/sbin/sa02m-userspace-watchdog"
if [ "$LOCAL_FILES" = 1 ] && [ -r "$SRC_WATCHDOG" ]; then
    install -m 755 "$SRC_WATCHDOG" /usr/local/sbin/sa02m-userspace-watchdog
else
    echo "WARN: $SRC_WATCHDOG не найден — пропустил установку бинарника" >&2
fi
if [ "$LOCAL_FILES" = 1 ] && [ -r "$SRC_WATCHDOG_CONF" ]; then
    install -m 644 "$SRC_WATCHDOG_CONF" /etc/sa02m_userspace_watchdog.conf
fi

log "[4/7] sa02m-userspace-watchdog.service"
if [ "$LOCAL_FILES" = 1 ] && [ -r "$SRC_WATCHDOG_UNIT" ]; then
    install -m 644 "$SRC_WATCHDOG_UNIT" /etc/systemd/system/sa02m-userspace-watchdog.service
fi

log "[5/7] sa02m-failure-monitor (логирование без reboot)"
if [ "$LOCAL_FILES" = 1 ] && [ -r "$SRC_FAILURE_MON" ]; then
    install -m 755 "$SRC_FAILURE_MON" /usr/local/sbin/sa02m-failure-monitor
fi
if [ "$LOCAL_FILES" = 1 ] && [ -r "$SRC_FAILURE_UNIT" ]; then
    install -m 644 "$SRC_FAILURE_UNIT" /etc/systemd/system/sa02m-failure-monitor.service
fi

log "[6/7] serial-getty@ttyS0 (115200) для COM-fallback"
systemctl unmask serial-getty@ttyS0.service 2>/dev/null || true
systemctl enable serial-getty@ttyS0.service 2>/dev/null || true

log "[7/7] daemon-reload + активация unit'ов"
systemctl daemon-reload
systemctl enable sa02m-userspace-watchdog.service 2>/dev/null || true
systemctl enable sa02m-failure-monitor.service 2>/dev/null || true
systemctl restart sa02m-userspace-watchdog.service || true
systemctl restart sa02m-failure-monitor.service || true
systemctl restart serial-getty@ttyS0.service 2>/dev/null || true

# Применить новые RuntimeWatchdog/RebootWatchdog: daemon-reexec на ходу
# может не подхватить (PID1 уже владеет /dev/watchdog), но после reboot
# параметры применятся гарантированно.
systemctl daemon-reexec 2>/dev/null || true

log "Состояние"
for s in sa02m-userspace-watchdog sa02m-failure-monitor \
         serial-getty@ttyS0 sa02m-watchdog-feed; do
    a=$(systemctl is-active "$s" 2>&1 || true)
    e=$(systemctl is-enabled "$s" 2>&1 || true)
    printf '%-32s active=%-12s enabled=%s\n' "$s" "$a" "$e"
done

echo
echo "Watchdog config (running):"
systemctl show -p RuntimeWatchdogUSec -p RebootWatchdogUSec -p WatchdogDevice 2>&1 | sed 's/^/    /'
echo "Watchdog config (file):"
sed 's/^/    /' /etc/systemd/system.conf.d/sa02m-watchdog.conf

echo
echo "Готово. RuntimeWatchdogSec=15s/RebootWatchdogSec=2min применятся"
echo "полностью после следующего reboot (PID1 уже владеет /dev/watchdog)."
