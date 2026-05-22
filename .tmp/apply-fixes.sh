#!/bin/bash
# Применяет фиксы на устройстве СА-02м.
# Вызывается через ssh sa02m bash -s
set -u

ts() { date '+%H:%M:%S'; }
say() { echo "[$(ts)] $*"; }

say "=== 1/8 Watchdog-feed: устанавливаем корректную реализацию ==="
cat >/usr/local/sbin/sa02m-watchdog-feed.sh <<'WD'
#!/bin/bash
set -u
DEV="${WATCHDOG_DEV:-/dev/watchdog}"
INTERVAL="${WATCHDOG_FEED_INTERVAL:-10}"
if [ ! -c "$DEV" ]; then
    echo "watchdog-feed: ${DEV} отсутствует, выход" >&2
    exit 0
fi
if ! exec 3>"$DEV"; then
    echo "watchdog-feed: не удалось открыть ${DEV}" >&2
    exit 1
fi
graceful_stop() {
    printf 'V' >&3 2>/dev/null || true
    exec 3>&- 2>/dev/null || true
    exit 0
}
trap graceful_stop TERM INT QUIT HUP
if [ -w /sys/class/watchdog/watchdog0/timeout ]; then
    echo 30 > /sys/class/watchdog/watchdog0/timeout 2>/dev/null || true
fi
while :; do
    printf '.' >&3 2>/dev/null || break
    sleep "$INTERVAL"
done
graceful_stop
WD
chmod 755 /usr/local/sbin/sa02m-watchdog-feed.sh

cat >/etc/systemd/system/sa02m-watchdog-feed.service <<'UNIT'
[Unit]
Description=SA-02m hardware watchdog feeder (kernel /dev/watchdog)
Documentation=file:/usr/local/sbin/sa02m-watchdog-feed.sh
DefaultDependencies=no
After=systemd-modules-load.service systemd-udevd.service local-fs.target
Conflicts=shutdown.target reboot.target halt.target
Before=shutdown.target reboot.target halt.target

[Service]
Type=simple
ExecStart=/usr/local/sbin/sa02m-watchdog-feed.sh
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=5
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=sa02m-watchdog-feed

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl restart sa02m-watchdog-feed.service
say "  -> sa02m-watchdog-feed: $(systemctl is-active sa02m-watchdog-feed)"

say "=== 2/8 SSH: ClientAliveInterval + reverse-DNS off ==="
mkdir -p /etc/ssh/sshd_config.d
cat >/etc/ssh/sshd_config.d/10-sa02m.conf <<'SSHCFG'
ClientAliveInterval 15
ClientAliveCountMax 4
TCPKeepAlive yes
UseDNS no
GSSAPIAuthentication no
LoginGraceTime 30
MaxStartups 30:30:100
MaxSessions 10
SSHCFG
chmod 644 /etc/ssh/sshd_config.d/10-sa02m.conf
if sshd -t 2>/tmp/sshd-test.err; then
    systemctl reload ssh 2>/dev/null || systemctl restart ssh
    say "  -> ssh reloaded ok"
else
    say "  !! sshd config error:"; cat /tmp/sshd-test.err
fi

say "=== 3/8 Time/RTC: unmask fake-hwclock, install/enable timesyncd ==="
# fake-hwclock сохраняет последнее время в файл и восстанавливает при загрузке —
# страхуем случай, когда DS3231/PCF8563 нет или садится батарейка.
if systemctl status fake-hwclock 2>&1 | grep -q masked; then
    systemctl unmask fake-hwclock.service
fi
if ! dpkg -l fake-hwclock 2>/dev/null | grep -q '^ii'; then
    DEBIAN_FRONTEND=noninteractive apt-get -y install fake-hwclock 2>&1 | tail -3 || true
fi
# Сохраняем текущее системное время как стартовое (если оно адекватное).
# Если время явно в прошлом (< 2024), не пишем — пусть остаётся файл от installer.
CUR_YEAR=$(date +%Y)
if [ "$CUR_YEAR" -ge 2024 ]; then
    fake-hwclock save 2>/dev/null || true
fi
systemctl enable fake-hwclock.service 2>/dev/null || true
fake-hwclock load force 2>/dev/null || hwclock -s 2>/dev/null || true

# systemd-timesyncd — лёгкий NTP-клиент. Когда устройство в локальной сети
# без интернета — не повредит, просто не синхронизируется. Когда есть инет —
# время выправится автоматически.
if ! command -v systemd-timesyncd >/dev/null 2>&1 && ! systemctl list-unit-files systemd-timesyncd.service 2>/dev/null | grep -q timesyncd; then
    DEBIAN_FRONTEND=noninteractive apt-get -y install systemd-timesyncd 2>&1 | tail -3 || true
fi
mkdir -p /etc/systemd/timesyncd.conf.d
cat >/etc/systemd/timesyncd.conf.d/sa02m.conf <<'TS'
[Time]
NTP=192.168.1.1 pool.ntp.org
FallbackNTP=time.cloudflare.com 0.ru.pool.ntp.org 1.ru.pool.ntp.org
TS
systemctl unmask systemd-timesyncd.service 2>/dev/null || true
systemctl enable --now systemd-timesyncd.service 2>/dev/null || true
say "  -> timesyncd: $(systemctl is-active systemd-timesyncd 2>/dev/null || echo n/a)"
say "  -> fake-hwclock saved: $(cat /etc/fake-hwclock.data 2>/dev/null || echo n/a)"

# Чиним PAM-предупреждение "password changed in future": если shadow-дата
# в будущем (потому что время было выставлено вперёд раньше), сбрасываем её на 0
# (=never). Это убирает спам "account root has password changed in future".
CUR_DAYS=$(( $(date +%s) / 86400 ))
ROOT_LAST=$(awk -F: '/^root:/{print $3}' /etc/shadow)
if [ -n "$ROOT_LAST" ] && [ "$ROOT_LAST" -gt "$CUR_DAYS" ]; then
    say "  -> shadow root lastchange=$ROOT_LAST > today=$CUR_DAYS, выравниваем"
    chage -d "$(date +%Y-%m-%d)" root 2>/dev/null || true
fi

say "=== 4/8 fix-eth.sh: устанавливаем версию с дедупликацией логов ==="
# Скрипт обновится с диска (см. ниже, передаём содержимым).
# Здесь только перезапускаем watchdog чтобы новый скрипт подхватился.

say "=== 5/8 NetworkManager-wait-online: отключаем (блокирует boot на 6 с) ==="
# eth0 управляется ifupdown (managed=false в NM). NM-wait-online всё равно
# ждёт network-online и удлиняет загрузку. Маскируем — это безопасно.
systemctl disable NetworkManager-wait-online.service 2>/dev/null || true
systemctl mask    NetworkManager-wait-online.service 2>/dev/null || true

say "=== 6/8 NodeRed: останавливаем и маскируем (стабильно падает по env) ==="
for u in nodered.service node-red.service; do
    if systemctl list-unit-files "$u" 2>/dev/null | grep -q "^$u"; then
        systemctl stop "$u" 2>/dev/null || true
        systemctl disable "$u" 2>/dev/null || true
        systemctl mask "$u" 2>/dev/null || true
    fi
done

say "=== 7/8 storage-mount@sda: сбрасываем failed-состояние ==="
# Условие старта прописывает udev — если sda нет, юнит не должен висеть failed.
systemctl reset-failed 'storage-mount@*.service' 2>/dev/null || true
systemctl reset-failed 2>/dev/null || true

say "=== 8/8 fake-hwclock сохранение при штатной остановке ==="
# Гарантируем, что при reboot/poweroff fake-hwclock.service делает save.
# Это стандартное поведение пакета, но при перезагрузке через мастер-сервис
# иногда не успевает — добавляем ExecStop в drop-in.
mkdir -p /etc/systemd/system/fake-hwclock.service.d
cat >/etc/systemd/system/fake-hwclock.service.d/sa02m-save-onstop.conf <<'DROP'
[Service]
ExecStop=/usr/sbin/fake-hwclock save
RemainAfterExit=yes
DROP
systemctl daemon-reload

say "=== Установка завершена. Статусы: ==="
echo "--- timesyncd ---";       timedatectl 2>&1 | sed 's/^/    /'
echo "--- watchdog-feed ---";   systemctl is-active sa02m-watchdog-feed
echo "--- ssh ---";             systemctl is-active ssh
echo "--- net-watchdog ---";    systemctl is-active net-watchdog
echo "--- failed units ---";    systemctl --failed --no-pager 2>&1 | sed 's/^/    /'
