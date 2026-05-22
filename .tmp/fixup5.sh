#!/bin/bash
set -u
ts() { date '+%H:%M:%S'; }
say() { echo "[$(ts)] $*"; }

say "=== A. systemd: уменьшаем DefaultTimeoutStopSec (90s → 15s) ==="
mkdir -p /etc/systemd/system.conf.d
cat >/etc/systemd/system.conf.d/sa02m-timeouts.conf <<'CFG'
[Manager]
# По умолчанию systemd ждёт 90 с прежде чем SIGKILL зависший сервис при
# shutdown. На СА-02м обычно завершение должно быть мгновенным; даём 15 с —
# этого достаточно чтобы nginx/sshd/chrony нормально остановились, но не
# превращает reboot в трёхминутный «зависон».
DefaultTimeoutStopSec=15s
DefaultTimeoutStartSec=30s
CFG

# То же для user services (если будут добавлены).
mkdir -p /etc/systemd/user.conf.d
cat >/etc/systemd/user.conf.d/sa02m-timeouts.conf <<'CFG'
[Manager]
DefaultTimeoutStopSec=15s
DefaultTimeoutStartSec=30s
CFG

# Перечитать (нужен daemon-reexec, не reload, т.к. это конфиг pid1).
systemctl daemon-reexec
say "  -> DefaultTimeoutStopSec=$(systemctl show -p DefaultTimeoutStopUSec --value)"

say "=== B. sa02m-pre-start.sh обновляется отдельно через scp ==="
# Скрипт уменьшит время ожидания DS3231 и PCA9536.

say "=== C. Готовы к reboot. Текущий boot-метрики (сохраняем): ==="
systemd-analyze 2>&1 | head -3
echo
systemctl is-active ssh sa02m-userspace-watchdog chrony net-watchdog 2>&1 | paste -d= <(printf '%s\n' ssh sa02m-userspace-watchdog chrony net-watchdog) -
