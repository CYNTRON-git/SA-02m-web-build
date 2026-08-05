# SA-02m system hardening

Стек защиты от зависаний/багов на работающем устройстве SA-02m.
Заменяет старый `sa02m-watchdog-feed` (bash-петля по `/dev/watchdog`),
который засорял journald сообщениями `watchdog: watchdog0: watchdog did not stop!`.

## Что делает

| Слой | Файл | Назначение |
|---|---|---|
| **HW WDT (PID1)** | `etc/systemd/sa02m-watchdog.conf` | drop-in для `system.conf` — `RuntimeWatchdogSec=15s`, `RebootWatchdogSec=0` (watchdog на время выключения отключён намеренно; обоснование — в шапке самого файла). PID1 systemd сам владеет `/dev/watchdog0`. |
| **Userspace WDT** | `etc/sa02m-userspace-watchdog.sh` + `etc/systemd/sa02m-userspace-watchdog.service` | health-checks (procs/ports/http/iface/load/mem); при стойком сбое делает forced reboot (graceful → reboot -f → kernel WDT). |
| **Failure monitor** | `etc/sa02m-failure-monitor.sh` + `etc/sa02m-failure-monitor.service` | логирование переходов состояний и snapshot при сбоях, без reboot. |
| **COM-fallback** | `serial-getty@ttyS0` (115200) | если сеть зависла — root login через USB-serial COM7. |

## Установка на устройство

С хоста (WSL/Windows):

```bash
# WSL: репо примонтирован в /mnt/c/...
ssh -i ~/.ssh/sa02m_sa02 root@192.168.1.136 "REPO_ROOT=/tmp/sa02m_repo bash -s" \
    < tools/system-hardening/install.sh
```

или через прямой stdin (без локальной копии скриптов на устройстве — режим
fallback с зашитыми внутри install.sh значениями):

```bash
ssh root@192.168.1.136 "bash -s" < tools/system-hardening/install.sh
```

На устройстве (через serial COM7):

```bash
# Сначала скопируй репо в /tmp/sa02m_repo (rsync/scp).
REPO_ROOT=/tmp/sa02m_repo bash /tmp/sa02m_repo/tools/system-hardening/install.sh
```

Скрипт **идемпотентный** — можно перезапускать.

## Проверка

```bash
ssh root@192.168.1.136 'systemctl show -p RuntimeWatchdogUSec -p RebootWatchdogUSec'
# RuntimeWatchdogUSec=15s
# RebootWatchdogUSec=0

ssh root@192.168.1.136 'cat /sys/class/watchdog/watchdog0/max_timeout'
# 16 — потолок драйвера sun4i-wdt; RuntimeWatchdogSec обязан быть ≤ него.
# Не путать с соседним `timeout` — там ТЕКУЩИЙ таймаут (с этой политикой 15).

ssh root@192.168.1.136 'tail -5 /var/log/sa02m_userspace_watchdog.log'
# heartbeat uptime=...s load=.../100 memavail=...KB

ssh root@192.168.1.136 'dmesg | grep -i watchdog | tail -5'
# systemd[1]: Using hardware watchdog 'sunxi-wdt'
# (НЕ должно быть "watchdog did not stop!" — это спам старого feeder'а)
```

## Конфиг (опционально)

`/etc/sa02m_userspace_watchdog.conf` — переопределение переменных. Пример:

```bash
# Если на этой плате mplc4 обязателен — переведи его в REQUIRED_PROCS
REQUIRED_PROCS="mplc4 nginx sshd"
WATCHED_PROCS="fcgiwrap"

# Минимум 32 MiB свободной памяти
MIN_MEM_AVAIL_KB=32768
```

По умолчанию `REQUIRED_PROCS="nginx sshd"`, `WATCHED_PROCS="mplc4 fcgiwrap"`.
Это значит: отсутствие mplc4 **не** перезагружает плату (на части плат
он по дизайну отключён); fcgiwrap мониторим, но не reboot'им.

## Восстановление через COM7

Если плата зависла по сети, но `serial-getty@ttyS0` поднят:

```
PuTTY/screen: COM7, 115200, 8N1
login: root
password: <обычный>
# затем:
systemctl status sa02m-userspace-watchdog
journalctl -u ssh -n 50
```

См. также `tools/imaging/serial-restore-ssh.py` — для восстановления sshd
после прерванного снятия образа (ssh host keys удалены).
