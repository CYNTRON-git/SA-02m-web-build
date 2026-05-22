#!/bin/bash
set -u
ts() { date '+%H:%M:%S'; }
say() { echo "[$(ts)] $*"; }

say "=== A. Watchdog: переходим на встроенный systemd PID1 watchdog ==="
# Если systemd сам кормит /dev/watchdog0 (через RuntimeWatchdogSec=), отдельный
# скрипт не нужен. Это самое надёжное решение: pid1 переживает любые сбои
# юзерспейса, чего нельзя гарантировать для bash-демона.
mkdir -p /etc/systemd/system.conf.d
cat >/etc/systemd/system.conf.d/sa02m-watchdog.conf <<'CFG'
[Manager]
# Кормить /dev/watchdog раз в треть таймаута, реальный таймаут 16-30 c в драйвере sunxi-wdt.
RuntimeWatchdogSec=10s
# При выключении/перезагрузке держим watchdog ещё минуту, чтобы если ядро зависнет
# во время shutdown — sunxi-wdt всё-таки перезагрузил систему.
ShutdownWatchdogSec=4min
# Старый systemd: RebootWatchdogSec= (синоним). Оставляем оба для совместимости.
RebootWatchdogSec=4min
CFG

# Останавливаем и отключаем кастомный feeder — он больше не нужен и только
# дублирует то, что делает systemd. Маскируем, чтобы не запускался даже при
# misconfig из старого образа.
systemctl stop sa02m-watchdog-feed.service 2>/dev/null || true
systemctl disable sa02m-watchdog-feed.service 2>/dev/null || true
systemctl mask sa02m-watchdog-feed.service 2>/dev/null || true

# Перечитать system.conf — нужен `systemctl daemon-reexec` (не reload), потому
# что RuntimeWatchdogSec читается только при старте pid1.
systemctl daemon-reexec
sleep 1
say "  -> systemd watchdog status (после daemon-reexec):"
journalctl -k --since '15 seconds ago' --no-pager 2>/dev/null | grep -i 'watchdog' | tail -5
echo "    runtime=$(systemctl show -p RuntimeWatchdogUSec | cut -d= -f2)"

say "=== B. Chrony: настраиваем NTP-клиент (timesyncd недоступен) ==="
# В этом образе systemd-timesyncd не пакетируется; зато есть chrony.
if ! command -v chronyd >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get -y install chrony 2>&1 | tail -3 || true
fi
mkdir -p /etc/chrony/conf.d 2>/dev/null || true
cat >/etc/chrony/sources.d/sa02m.sources <<'NTP'
# SA-02m NTP sources
pool pool.ntp.org iburst maxsources 4
pool ru.pool.ntp.org iburst maxsources 4
server time.cloudflare.com iburst
NTP
# Если основной конфиг включает sourcedir conf.d / sources.d, наш файл подхватится.
# На всякий случай добавим включение, если его нет:
if ! grep -qE '^[[:space:]]*sourcedir[[:space:]]+/etc/chrony/sources.d' /etc/chrony/chrony.conf 2>/dev/null; then
    echo 'sourcedir /etc/chrony/sources.d' >> /etc/chrony/chrony.conf
fi
# Включаем синхронизацию RTC из chrony — раз в час пишем системное время в hwclock.
if ! grep -qE '^[[:space:]]*rtcsync' /etc/chrony/chrony.conf 2>/dev/null; then
    echo 'rtcsync' >> /etc/chrony/chrony.conf
fi
# makestep: если расхождение > 1 c, скорректировать одним шагом в течение первых
# 3 проверок после старта. Полезно если RTC батарейка села и время в 2000.
if ! grep -qE '^[[:space:]]*makestep' /etc/chrony/chrony.conf 2>/dev/null; then
    echo 'makestep 1.0 3' >> /etc/chrony/chrony.conf
fi
systemctl unmask chrony.service chronyd.service 2>/dev/null || true
systemctl enable --now chrony.service 2>/dev/null || systemctl enable --now chronyd.service 2>/dev/null || true
sleep 2
say "  -> chrony status: $(systemctl is-active chrony 2>/dev/null || systemctl is-active chronyd 2>/dev/null)"
chronyc -n sources 2>&1 | head -8

say "=== C. Установка обновлённого fix-eth.sh ==="
# Будет передан отдельно (см. ниже apply-fix-eth.sh).

say "=== D. Проверка статусов ==="
systemctl is-active ssh sa02m-watchdog-feed chrony net-watchdog 2>&1 | paste -d= <(echo -e "ssh\nsa02m-watchdog-feed\nchrony\nnet-watchdog") -
echo "---"
echo "RuntimeWatchdog: $(systemctl show -p RuntimeWatchdogUSec --value)"
echo "ShutdownWatchdog: $(systemctl show -p ShutdownWatchdogUSec --value)"
echo "Time: $(date)"
echo "RTC : $(hwclock --show 2>&1)"
echo "Synced: $(timedatectl show -p NTPSynchronized --value)"
