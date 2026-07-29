# SSH и удалённая диагностика SA-02m (для агентов Cursor)

Краткий алгоритм, чтобы **не тратить время** на зависания, host key и интерактивный ввод пароля при работе из Windows (PowerShell, агент Cursor).

---

## Доступы по умолчанию

| | Значение |
|--|----------|
| IP | `192.168.1.136` (LAN, `eth0`) |
| SSH | `root` / `cyntron`, порт `22` |
| Веб | `http://192.168.1.136:9999`, `admin` / `cyntron` |
| Ключ (опционально) | `%USERPROFILE%\.ssh\sa02m_sa02` или `private/.ssh/sa02m_sa02` (не в git) |

После прошивки нового образа eMMC **host key SSH меняется** — см. раздел «Host key» ниже.

---

## Что НЕ делать (типичные потери времени)

| Ошибка | Симптом | Почему |
|--------|---------|--------|
| `ssh root@192.168.1.136 "cmd"` без ключа в batch | Команда висит 20+ с, нет вывода | OpenSSH ждёт пароль интерактивно |
| `plink -batch` без `-hostkey` | `FATAL ERROR: Cannot confirm a host key in batch mode` | Кэш PuTTY не совпадает (после перепрошивки образа) |
| PowerShell + `ssh "grep a\|b"` | `COM`/`modbus` не распознаны | `\|` и `$p` интерпретируются PowerShell |
| `curl --unix-socket .../flasher.sock` без cookie | `{"error":"unauthorized"}` | API flasher требует сессию веб-UI |
| Долгие потоки по SSH | `journalctl -f`, `dmesg -w` | Могут подвешивать устройство — см. `docs/SA02M_SSH_ACCESS_PROBLEM_AND_FIX.md` |

---

## Рекомендуемый алгоритм (Windows, агент)

### Вариант A — Python (предпочтительно для агентов)

```powershell
cd C:\Users\admin\Downloads\SA-02m-web-build
py -3 tools/ssh/sa02m_remote.py exec "systemctl is-active sa02m-flasher nginx"
py -3 tools/ssh/sa02m_remote.py tail-flasher
py -3 tools/ssh/sa02m_remote.py watch-flasher    # во время прошивки модулей через веб
```

Загрузить `.fw` в кеш на устройстве (если нет DNS / нет интернета):

```powershell
py -3 tools/ssh/sa02m_remote.py upload-fw D:\path\MR-02m_1.0.9.0.fw
```

Требуется: `pip install paramiko` (обычно уже есть в окружении агента).

### Вариант B — PuTTY plink (одна команда)

```powershell
$HostKey = "SHA256:TMkrSFsuRUe0F1caCEcTNUli9gb7KaQYsPC7FELohKc"
& "C:\Program Files\PuTTY\plink.exe" -batch -ssh root@192.168.1.136 -pw cyntron -hostkey $HostKey "hostname"
```

Обёртка в репозитории:

```powershell
.\tools\ssh\sa02m-remote.ps1 "systemctl is-active sa02m-flasher"
```

### Вариант C — OpenSSH с ключом (интерактивная сессия человека)

```powershell
ssh -i "$env:USERPROFILE\.ssh\sa02m_sa02" -o StrictHostKeyChecking=accept-new root@192.168.1.136
```

---

## Host key (обязательно для plink -batch)

**Текущий fingerprint** (обновляйте после перепрошивки образа SA-02m):

```
SHA256:TMkrSFsuRUe0F1caCEcTNUli9gb7KaQYsPC7FELohKc
```

Получить заново с ПК:

```powershell
ssh-keyscan -t ed25519 192.168.1.136
```

Или из сообщения об ошибке plink (`The new ssh-ed25519 key fingerprint is:`).

Переменная окружения для скриптов: `SA02M_HOSTKEY=SHA256:...`

---

## Диагностика прошивки модулей MR-02m (веб → RS-485)

Цепочка: браузер → nginx `/api/flasher/*` → unix-socket `/run/sa02m-flasher/flasher.sock` → `sa02m-flasher.service`.

### Логи на устройстве

| Файл | Содержимое |
|------|------------|
| `/var/log/sa02m-flasher/events.log` | JSON Lines: шаги job scan/flash, ошибки (основной для агента) |
| `/var/log/sa02m-flasher/flasher.log` | HTTP-запросы к демону, systemd |
| `journalctl -u sa02m-flasher -n 50 --no-pager` | падения сервиса |

Мониторинг во время прошивки пользователем через веб:

```powershell
py -3 tools/ssh/sa02m_remote.py watch-flasher
```

### Кеш прошивок на устройстве

```
/var/lib/sa02m-flasher/firmware/
```

Манифест: `.index.json` (скачивается с `https://cyntron.ru/.../index.json`).

---

## Типичная ошибка прошивки: DNS / файл не в кеше

**Симптом в веб-логе:**

```
Ошибка: <urlopen error [Errno -3] Temporary failure in name resolution>
```

**Причина:** выбран файл из манифеста (например `MR-02m_1.0.9.0.fw`), но **файла нет локально** в `/var/lib/sa02m-flasher/firmware/`. При старте `flash_batch` демон вызывает `repo.download()` → HTTP к `cyntron.ru` → на устройстве **не работает DNS** (`ping cyntron.ru` → `unknown host`), хотя в `/etc/resolv.conf` могут быть прописаны 8.8.8.8 / 77.88.8.8.

**Проверка на устройстве:**

```bash
ls -la /var/lib/sa02m-flasher/firmware/
ping -c1 -W2 cyntron.ru
```

**Решения (любое одно):**

1. **Прошить версию из кеша** — например `MR-02m_1.0.8.26.fw`, если файл уже на диске.
2. **Загрузить .fw через веб** — вкладка MR-02m → загрузка файла (POST `/api/flasher/firmware/upload`), без интернета на шлюзе.
3. **SCP/pscp/paramiko** — положить файл в `/var/lib/sa02m-flasher/firmware/`, владелец `sa02m-flasher:sa02m-flasher`, mode `644`:
   ```powershell
   py -3 tools/ssh/sa02m_remote.py upload-fw path\to\MR-02m_1.0.9.0.fw
   ```
4. **Починить DNS/шлюз** на SA-02m (маршрут, `systemd-resolved`, firewall) — для автозагрузки из манифеста.

SSE «Потеряно соединение» после ошибки job — следствие завершения задачи с `error`, не отдельная сетевая проблема браузера.

---

## Другие частые ошибки flash_batch

| Сообщение | Причина | Действие |
|-----------|---------|----------|
| Сигнатура «» не распознана | В UI выбрана строка скана без сигнатуры | Выбрать устройство с `4DO6DI` / `6AI6AO` и т.д. |
| Загрузчик не отвечает по серийному | Не в bootloader / шина занята / 2 модуля на линии | «Остановить опрос», прошивать по одному, fast Modbus |
| Таймаут reg 129 | MPLC4/MQTT держат RS-485 | Остановить опрос; `mplc4` может не гаситься через pkill (`mplc_daemon`) |
| HTTP 409 | Порт занят другим job или процессом | Дождаться job / «Остановить опрос» |
| HTTP 502 | `sa02m-flasher` down или EPERM на socket | `systemctl status sa02m-flasher` |

---

## Связанные документы

- `README.md` — доступы, установка
- `docs/SA02M_SSH_ACCESS_PROBLEM_AND_FIX.md` — зависания SSH, MOTD, PAM
- `README.md` § «Устройства MR-02m (flasher)» — API и архитектура
- `.cursor/rules/sa02m_flash_workflow.mdc` — прошивка **образа** eMMC (не путать с модулями RS-485)
