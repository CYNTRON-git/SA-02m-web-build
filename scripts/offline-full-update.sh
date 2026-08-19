#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Офлайн ПОЛНОЕ обновление СА-02м из распакованного архива репозитория.
# Запуск на устройстве (root), из любого каталога:
#   mkdir -p /tmp/sa02m-upd && tar xzf /tmp/SA-02m-full-<ver>.tar.gz -C /tmp/sa02m-upd
#   bash /tmp/sa02m-upd/scripts/offline-full-update.sh [опции]
# Процедура и WHY — docs/deployment.md «Полный деплой» → «Офлайн-вариант»;
# режим --unattended — там же, «Мост самообновления для плат < 1.0.5.75».
# ═══════════════════════════════════════════════════════════════════════════
# On-device wrapper for the device-side steps of the full-deploy runbook
# (docs/deployment.md `## Полный деплой (install.sh)`, steps 1,4,5,6,7): /etc +
# web-root backup → `bash -n install.sh` → detached `nohup install.sh` with a
# log → poll → PASS/FAIL post-checks incl. the update-check proof (branch=main).
# Two callers, one home: an operator over SSH (interactive poll loop) and the
# self-upgrade bridge unit (`--unattended`: no TTY, synchronous wait, legacy
# status file + update.log for the old UI).
# Deliberately NOT `set -e`: the poll loop and the post-check table must keep
# reporting after a failing probe (a FAIL row, never a silent abort); every
# refusal below exits explicitly instead.
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
: "${WEB_ROOT:=/var/www/network_config}"   # same override idiom as update-www-only.sh
STATE_JSON=/var/lib/sa02m-web-build/check.json
UPDATE_CHECK=/usr/local/sbin/sa02m-web-update-check
PIDFILE=/run/sa02m-offline-update.pid
[ -w /run ] || PIDFILE=/root/sa02m-offline-update.pid
POLL_SEC=15
CORE_SERVICES="nginx fcgiwrap mosquitto sa02m-modbus-mqtt sa02m-devices-api sa02m-flasher"
# Optional vendor/heavy stacks skipped by default: Node-RED/CODESYS/Docker pull
# from the internet or need an EULA payload, MPLC needs a vendor payload absent
# from git. Skipping a module leaves an already-installed stack untouched
# (install.sh semantics). Every sa02m stack (web, flasher, cloud, mqtt bridge,
# devices, gateway, alice, update runner) always runs.
DEFAULT_SKIPS="NODERED CODESYS DOCKER MPLC"
ALLOWED_SKIPS="NODERED CODESYS DOCKER MPLC MQTT GATEWAY ALICE DEVICES"

EXTRA_LOG=""      # --log PATH: every line below is also appended there (plain, no colour)
STATUS_FILE=""    # --status-file PATH: running | done | error (legacy update_status contract)

say() {   # plain line to stdout (+ the extra log when set)
    printf '%s\n' "$1"
    [ -n "$EXTRA_LOG" ] && printf '%s\n' "$1" >> "$EXTRA_LOG" 2>/dev/null
    return 0
}
log() {
    local level=${1:-INFO} msg=${2:-} color reset='\033[0m' line
    case "$level" in
        OK)   color='\033[0;32m' ;;
        WARN) color='\033[0;33m' ;;
        ERR)  color='\033[0;31m' ;;
        *)    color='\033[0;36m' ;;
    esac
    line="[$(date '+%H:%M:%S')] [${level}] ${msg}"
    echo -e "${color}${line}${reset}"
    [ -n "$EXTRA_LOG" ] && printf '%s\n' "$line" >> "$EXTRA_LOG" 2>/dev/null
    return 0
}
set_status() { [ -n "$STATUS_FILE" ] && printf '%s' "$1" > "$STATUS_FILE" 2>/dev/null; return 0; }

usage() {
    cat <<EOF
Использование: bash scripts/offline-full-update.sh [опции]

Полное обновление СА-02м из распакованного архива репозитория (install.sh
в фоне с логом, бэкапы, пост-проверки). Запуск от root на устройстве.

Опции:
  --status          не запускать установку: показать хвост лога, идёт ли ещё
                    install.sh (дождаться его), выполнить пост-проверки
  --force           разрешить откат на версию НИЖЕ установленной
  --no-backup       не снимать бэкапы /etc и веб-корня
  --with-optional   ставить и опциональные стеки (по умолчанию пропущены:
                    ${DEFAULT_SKIPS})
  --skip LIST       дополнительно пропустить модули, через запятую; допустимы:
                    ${ALLOWED_SKIPS}
  --unattended      без терминала (мост самообновления / systemd-юнит): без
                    опроса лога, дождаться install.sh синхронно
  --dry-run         проверки + bash -n, напечатать что БЫЛО БЫ сделано; без
                    бэкапов и без запуска install.sh (код выхода 0)
  --status-file P   писать running|done|error в файл P (контракт update_status)
  --log P           дублировать ход и таблицу проверок в файл P (append)
  --help            эта справка

Лог установки: /root/install-offline-<версия>.log; бэкапы:
/root/etc-backup-<ts>.tgz, /root/www-backup-<ts>.tgz. Ctrl-C во время
ожидания НЕ прерывает установку — вернуться: ... --status.
Код выхода: 0 — все пост-проверки PASS, 1 — есть FAIL/отказ, 2 — ошибка аргументов.
EOF
}

# ── Argument parsing (allow-listed; anything unknown → usage + exit 2) ──────
MODE_STATUS=0 FORCE=0 NO_BACKUP=0 WITH_OPTIONAL=0 UNATTENDED=0 DRY_RUN=0 EXTRA_SKIPS=""
need_abs_path() {   # option values that name a file: absolute, no whitespace
    case "${2:-}" in /?*) ;; *) echo "$1 требует абсолютный путь" >&2; usage >&2; exit 2 ;; esac
    case "$2" in *[[:space:]]*) echo "$1: путь без пробелов" >&2; exit 2 ;; esac
}
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --status) MODE_STATUS=1 ;;
        --force) FORCE=1 ;;
        --no-backup) NO_BACKUP=1 ;;
        --with-optional) WITH_OPTIONAL=1 ;;
        --unattended) UNATTENDED=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --skip=*) EXTRA_SKIPS="$EXTRA_SKIPS ${1#--skip=}" ;;
        --skip)
            [ $# -ge 2 ] || { echo "--skip требует список" >&2; usage >&2; exit 2; }
            EXTRA_SKIPS="$EXTRA_SKIPS $2"; shift ;;
        --status-file) need_abs_path "$1" "${2:-}"; STATUS_FILE=$2; shift ;;
        --log) need_abs_path "$1" "${2:-}"; EXTRA_LOG=$2; shift ;;
        *) echo "Неизвестный аргумент: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
SKIPS=""
[ "$WITH_OPTIONAL" = 1 ] || SKIPS="$DEFAULT_SKIPS"
for tok in $(printf '%s' "$EXTRA_SKIPS" | tr ',' ' ' | tr '[:lower:]' '[:upper:]'); do
    case " $ALLOWED_SKIPS " in
        *" $tok "*) case " $SKIPS " in *" $tok "*) ;; *) SKIPS="$SKIPS $tok" ;; esac ;;
        *) echo "Недопустимое имя модуля в --skip: $tok (допустимы: $ALLOWED_SKIPS)" >&2; exit 2 ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    log ERR "Запустите от root: sudo bash $0"; exit 1
fi
for f in "$EXTRA_LOG" "$STATUS_FILE"; do [ -n "$f" ] && mkdir -p "$(dirname "$f")" 2>/dev/null; done

# Status contract: `running` from here on, `done`/`error` by the final exit
# code (an early refusal is an `error` too — the bridge UI must never hang on
# `running`). The observer modes (--status, --help) never touch the file.
KEEP_STATUS=0
if [ -n "$STATUS_FILE" ] && [ "$MODE_STATUS" = 0 ]; then
    set_status running
    trap 'rc=$?; [ "$KEEP_STATUS" = 1 ] || { [ "$rc" -eq 0 ] && set_status "done" || set_status "error"; }' EXIT
fi

# ── Tree check: a full tree is required (a www-only archive is refused) ─────
missing=""
for p in install.sh scripts etc opt www/network_config/VERSION; do
    [ -e "$REPO_ROOT/$p" ] || missing="$missing $p"
done
if [ -n "$missing" ]; then
    log ERR "В $REPO_ROOT нет:$missing"
    log ERR "Нужен ПОЛНЫЙ архив репозитория (git archive origin/main), а не www-only: install.sh + scripts/ + etc/ + opt/ + www/."
    exit 1
fi

read_version() { [ -f "$1" ] || return 0; tr -d '\r' < "$1" | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1; }
compare_versions() {   # prints lt | equal | gt for $1 vs $2 (both validated M.M.P[.S])
    [ "$1" = "$2" ] && { echo equal; return; }
    [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ] && echo lt || echo gt
}
TARGET_VER="$(read_version "$REPO_ROOT/www/network_config/VERSION")"
if [ -z "$TARGET_VER" ]; then
    log ERR "Не удалось прочитать версию из $REPO_ROOT/www/network_config/VERSION"; exit 1
fi
DEPLOYED_VER="$(read_version "$WEB_ROOT/VERSION")"
LOG="/root/install-offline-${TARGET_VER}.log"
log INFO "Установлено: ${DEPLOYED_VER:-не установлено}  →  цель: ${TARGET_VER}  (дерево: $REPO_ROOT)"

pid_alive() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }

# ── Wait for install.sh ─────────────────────────────────────────────────────
# Interactive: print new log lines every POLL_SEC. Ctrl-C only stops the
# watcher — install.sh was started as an async job of a non-interactive shell
# (SIGINT ignored) under nohup (SIGHUP ignored), so it keeps running; the trap
# tells the operator how to re-attach. Unattended: no TTY — a heartbeat with the
# installer's last line goes to the extra log (the old UI tails it) every 60 s.
poll_install() {
    local pid=$1 shown=0 total started
    started=$(date +%s)
    if [ "$UNATTENDED" = 1 ]; then
        log INFO "install.sh запущен (PID $pid), лог: $LOG — ожидание без терминала"
        while kill -0 "$pid" 2>/dev/null; do
            sleep 60
            kill -0 "$pid" 2>/dev/null || break
            log INFO "… install.sh работает ($(( ($(date +%s) - started) / 60 )) мин): $(tail -n 1 "$LOG" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g')"
        done
        rm -f "$PIDFILE"; return 0
    fi
    trap 'echo; KEEP_STATUS=1; log WARN "Ожидание прервано, install.sh (PID '"$pid"') продолжает работать. Вернуться: bash $0 --status"; exit 0' INT
    log INFO "install.sh запущен (PID $pid), лог: $LOG — ожидание, Ctrl-C не прерывает установку"
    while kill -0 "$pid" 2>/dev/null; do
        sleep "$POLL_SEC"
        total=$(wc -l < "$LOG" 2>/dev/null || echo 0)
        if [ "$total" -gt "$shown" ]; then
            sed -n "$((shown + 1)),${total}p" "$LOG"; shown=$total
        else
            log INFO "… install.sh работает ($(( ($(date +%s) - started) / 60 )) мин)"
        fi
    done
    trap - INT
    total=$(wc -l < "$LOG" 2>/dev/null || echo 0)
    [ "$total" -gt "$shown" ] && sed -n "$((shown + 1)),${total}p" "$LOG"
    rm -f "$PIDFILE"
}

# ── Post-checks: PASS/FAIL table, runbook step 7; never silent ──────────────
N_PASS=0 N_FAIL=0
row() {   # row PASS|FAIL <name> <detail>
    case "$1" in PASS) N_PASS=$((N_PASS + 1)) ;; *) N_FAIL=$((N_FAIL + 1)) ;; esac
    # Pad by characters, not printf's bytes (Cyrillic names); width floor 1.
    local w=$((34 - ${#2})); [ "$w" -ge 1 ] || w=1
    say "$(printf '  %-4s  %s%*s%s' "$1" "$2" "$w" '' "${3:-}")"
}
post_checks() {   # $1 = install.sh exit code or "" when unknown (--status re-attach)
    local rc=${1:-} out code ver svc st branch rv dv ua
    say ""; log INFO "Пост-проверки (docs/deployment.md, шаг 7):"
    if grep -q 'Установка завершена' "$LOG" 2>/dev/null; then
        row PASS "install.sh завершён" "финальный баннер в логе${rc:+, rc=$rc}"
    else
        row FAIL "install.sh завершён" "нет баннера «Установка завершена» в $LOG${rc:+, rc=$rc}"
    fi
    if command -v systemctl >/dev/null 2>&1; then
        # sed strips the leading "●" bullet some systemd versions print on failed rows.
        out="$(systemctl list-units --state=failed --no-legend --plain 2>/dev/null | sed 's/^[^[:alnum:]]*//' | awk '{print $1}' | tr '\n' ' ')"
        [ -z "${out// /}" ] && row PASS "systemctl --failed" "пусто" || row FAIL "systemctl --failed" "$out"
    else
        row FAIL "systemctl --failed" "systemctl не найден"
    fi
    code="$(timeout 10 curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9999/ 2>/dev/null)"; [ -n "$code" ] || code=000
    [ "$code" = 200 ] && row PASS "nginx :9999" "HTTP $code" || row FAIL "nginx :9999" "HTTP $code (ожидалось 200)"
    ver="$(read_version "$WEB_ROOT/VERSION")"
    [ "$ver" = "$TARGET_VER" ] && row PASS "VERSION развёрнут" "$ver" || row FAIL "VERSION развёрнут" "${ver:-нет} ≠ $TARGET_VER"
    if visudo -c >/dev/null 2>&1; then row PASS "visudo -c" "rc=0"; else row FAIL "visudo -c" "rc≠0 — проверить /etc/sudoers.d"; fi
    out="$(grep -E '\[(ERR|WARN)\]' "$LOG" 2>/dev/null)"
    if printf '%s' "$out" | grep -q '\[ERR\]'; then
        row FAIL "лог: [ERR]/[WARN]" "есть [ERR] — строки ниже"
    else
        row PASS "лог: [ERR]/[WARN]" "$( [ -n "$out" ] && echo 'только [WARN] — строки ниже (пропущенный/упавший опциональный стек виден ТОЛЬКО по WARN)' || echo 'нет')"
    fi
    [ -n "$out" ] && while IFS= read -r line; do say "        $line"; done <<< "$out"
    for svc in $CORE_SERVICES; do
        st="$(systemctl is-active "$svc" 2>/dev/null || true)"
        if [ "$st" = active ]; then row PASS "служба $svc" "active"
        else
            systemctl cat "$svc" >/dev/null 2>&1 && row FAIL "служба $svc" "${st:-unknown}" \
                || row FAIL "служба $svc" "юнит не установлен"
        fi
    done
    # Update-check proof: the symptom this path exists for — the old check
    # compared against the board's own version branch; the new one targets main.
    if [ -x "$UPDATE_CHECK" ]; then
        timeout 60 "$UPDATE_CHECK" --manual >/dev/null 2>&1 || true
        branch=""; rv=""; dv=""; ua=""
        if [ -f "$STATE_JSON" ]; then
            if command -v python3 >/dev/null 2>&1; then
                read -r branch rv dv ua < <(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print(*(str(d.get(k)) for k in ("branch","remote_version","deployed_version","update_available")))' "$STATE_JSON" 2>/dev/null || true)
            else
                branch="$(grep -oE '"branch":"[^"]*"' "$STATE_JSON" | cut -d'"' -f4)"
                rv="$(grep -oE '"remote_version":("[^"]*"|null)' "$STATE_JSON" | cut -d: -f2 | tr -d '"')"
                dv="$(grep -oE '"deployed_version":("[^"]*"|null)' "$STATE_JSON" | cut -d: -f2 | tr -d '"')"
                ua="$(grep -oE '"update_available":[a-z]+' "$STATE_JSON" | cut -d: -f2)"
            fi
        fi
        if [ "$branch" = main ]; then
            row PASS "проверка обновлений → main" "remote=${rv:-?} deployed=${dv:-?} update_available=${ua:-?}"
        else
            row FAIL "проверка обновлений → main" "branch=${branch:-нет check.json} (старый check целится в свою ветку)"
        fi
    else
        row FAIL "проверка обновлений → main" "$UPDATE_CHECK отсутствует"
    fi
    say ""
    if [ "$N_FAIL" -eq 0 ]; then
        log OK "ИТОГ: PASS ($N_PASS/$((N_PASS + N_FAIL))) — обновление до $TARGET_VER выполнено"; return 0
    fi
    log ERR "ИТОГ: FAIL — $N_FAIL из $((N_PASS + N_FAIL)) проверок не прошли (лог: $LOG)"; return 1
}

# ── --status: re-attach (wait if still running) + post-checks ──────────────
if [ "$MODE_STATUS" = 1 ]; then
    if pid_alive; then
        poll_install "$(cat "$PIDFILE")"
    else
        [ -f "$PIDFILE" ] && rm -f "$PIDFILE"
        if [ -f "$LOG" ]; then log INFO "install.sh не работает; хвост $LOG:"; tail -n 20 "$LOG"
        else log WARN "Лога $LOG нет — установка этой версии ещё не запускалась"; fi
    fi
    post_checks ""; exit $?
fi

# ── Guards before touching the board ───────────────────────────────────────
if pid_alive; then
    log ERR "install.sh уже работает (PID $(cat "$PIDFILE")). Наблюдать: bash $0 --status"; exit 1
fi
rm -f "$PIDFILE"
if [ -n "$DEPLOYED_VER" ] && [ "$(compare_versions "$TARGET_VER" "$DEPLOYED_VER")" = lt ]; then
    if [ "$FORCE" = 1 ]; then
        log WARN "ОТКАТ $DEPLOYED_VER → $TARGET_VER разрешён флагом --force"
    else
        log ERR "Отказ: цель $TARGET_VER НИЖЕ установленной $DEPLOYED_VER (откат только с --force)"; exit 1
    fi
fi

# ── Step 1: backups (runbook: /etc whole, web root as in the www-only path) ─
if [ "$DRY_RUN" = 1 ]; then
    if [ "$NO_BACKUP" = 1 ]; then log INFO "DRY-RUN: бэкапы были бы пропущены (--no-backup)"
    else log INFO "DRY-RUN: снял бы /root/etc-backup-<ts>.tgz (из /etc) и /root/www-backup-<ts>.tgz (из $WEB_ROOT)"; fi
elif [ "$NO_BACKUP" = 1 ]; then
    log WARN "Бэкапы пропущены (--no-backup)"
else
    ts="$(date +%Y%m%d-%H%M%S)"
    if tar czf "/root/etc-backup-${ts}.tgz" -C / etc 2>/dev/null; then log OK "Бэкап /etc → /root/etc-backup-${ts}.tgz"
    else log ERR "Не удалось снять бэкап /etc"; exit 1; fi
    if [ -d "$WEB_ROOT" ]; then
        if tar czf "/root/www-backup-${ts}.tgz" -C "$(dirname "$WEB_ROOT")" "$(basename "$WEB_ROOT")" 2>/dev/null; then
            log OK "Бэкап веб-корня → /root/www-backup-${ts}.tgz"
        else log ERR "Не удалось снять бэкап $WEB_ROOT"; exit 1; fi
    fi
fi

# ── Step 5: pre-flight ─────────────────────────────────────────────────────
if bash -n "$REPO_ROOT/install.sh"; then log OK "bash -n install.sh: синтаксис чист"
else log ERR "install.sh: синтаксическая ошибка — установка не запущена"; exit 1; fi

# ── Step 6: detached run with a log (never foreground — install touches the
# network stack, SSH may drop). The env assignments go through `env` BEFORE
# the command word; after nohup they would become its program name. The same
# launch serves --unattended: the PID file lets an SSH `--status` observe the
# bridge's install, and `wait` gives the exit code in both modes.
SKIP_ENV=()
for tok in $SKIPS; do SKIP_ENV+=("SA02M_SKIP_${tok}=1"); done
log INFO "Пропускаемые модули: ${SKIPS:-нет (ставится всё)}"
cd "$REPO_ROOT" || exit 1
if [ "$DRY_RUN" = 1 ]; then
    # Test hook for the bridge chain (no board): same path up to the launch,
    # then report instead of act; the status contract still ends in `done`.
    log WARN "DRY-RUN: install.sh не запускался"
    log INFO "DRY-RUN: выполнил бы: (cd $REPO_ROOT && nohup env ${SKIP_ENV[*]:-} bash install.sh > $LOG 2>&1 &), PID-файл $PIDFILE, unattended=$UNATTENDED"
    log INFO "DRY-RUN: затем пост-проверки (таблица PASS/FAIL), статус-файл: ${STATUS_FILE:-нет}, доп. лог: ${EXTRA_LOG:-нет}"
    exit 0
fi
nohup env ${SKIP_ENV[@]+"${SKIP_ENV[@]}"} bash install.sh > "$LOG" 2>&1 &
INSTALL_PID=$!
echo "$INSTALL_PID" > "$PIDFILE"
poll_install "$INSTALL_PID"
wait "$INSTALL_PID"; INSTALL_RC=$?
[ "$INSTALL_RC" -eq 0 ] && log OK "install.sh завершился, rc=0" || log ERR "install.sh завершился с rc=$INSTALL_RC"

# ── Step 7: post-checks ────────────────────────────────────────────────────
post_checks "$INSTALL_RC"
