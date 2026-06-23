#!/bin/bash
# Загрузка и деплой веб-интерфейса из GitHub. Запускается от root через sudo.
# Вывод пишется в $LOGFILE; статус — в $STATEDIR/update_status (running/done/error).
set -uo pipefail

LIB=/usr/local/lib/sa02m-web-build-lib.sh
[ -f "$LIB" ] && . "$LIB"

STATEDIR=/var/lib/sa02m-web-build
LOGFILE="$STATEDIR/update.log"
LOCKFILE="$STATEDIR/update.lock"
STATUS_FILE="$STATEDIR/update_status"
REPO_URL="${SA02M_WEB_BUILD_REPO_URL:-https://github.com/CYNTRON-git/SA-02m-web-build.git}"
BRANCH="${SA02M_WEB_BUILD_BRANCH:-main}"
if type resolve_web_build_branch >/dev/null 2>&1; then
    BRANCH="$(resolve_web_build_branch)"
fi
WEB_ROOT=/var/www/network_config

mkdir -p "$STATEDIR"
chmod 755 "$STATEDIR"

log() { local ts; ts=$(date '+%Y-%m-%d %H:%M:%S'); printf '%s %s\n' "$ts" "$*" | tee -a "$LOGFILE"; }

# Лок
if [ -f "$LOCKFILE" ]; then
    pid=$(cat "$LOCKFILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        log "ERROR: обновление уже выполняется (pid $pid)"
        exit 1
    fi
    rm -f "$LOCKFILE"
fi
printf '%s' "$$" > "$LOCKFILE"
printf 'running' > "$STATUS_FILE"
chmod 644 "$LOCKFILE" "$STATUS_FILE" 2>/dev/null || true

cleanup() {
    rm -f "$LOCKFILE"
    rm -rf "${TMPDIR:-}" 2>/dev/null || true
}
trap cleanup EXIT

> "$LOGFILE"
log "START: $REPO_URL ветка $BRANCH"
printf 'running' > "$STATUS_FILE"

# Временная директория для клонирования
TMPDIR=$(mktemp -d /tmp/sa02m-web-update-XXXXXX)
chmod 755 "$TMPDIR"

log "Клонирование репозитория..."
if ! git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 \
    clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$TMPDIR/repo" 2>&1 | tee -a "$LOGFILE"; then
    log "ERROR: git clone не удался"
    printf 'error' > "$STATUS_FILE"
    exit 1
fi

log "Деплой веб-файлов в $WEB_ROOT..."
if ! cp -a "$TMPDIR/repo/www/network_config/." "$WEB_ROOT/" 2>&1 | tee -a "$LOGFILE"; then
    log "ERROR: копирование не удалось"
    printf 'error' > "$STATUS_FILE"
    exit 1
fi

# Права доступа
find "$WEB_ROOT/cgi-bin" -name '*.cgi' -exec chmod 755 {} \; 2>/dev/null || true
find "$WEB_ROOT/static" \( -name '*.css' -o -name '*.js' \) -exec chmod 644 {} \; 2>/dev/null || true
chmod 644 "$WEB_ROOT/index.html" "$WEB_ROOT/login.html" 2>/dev/null || true
chown -R www-data:www-data "$WEB_ROOT" 2>/dev/null || true

# Обновляем вспомогательные скрипты из репозитория
for src in etc/sa02m-web-build-lib.sh etc/sa02m-web-update-check.sh etc/sa02m-web-update-apply.sh etc/sa02m-web-auth-lib.sh etc/sa02m-repair-web-env.sh; do
    if [ -f "$TMPDIR/repo/$src" ]; then
        tgt="/usr/local/sbin/$(basename "${src%.sh}")"
        if [ "$src" = "etc/sa02m-web-auth-lib.sh" ]; then
            tgt="/usr/local/lib/sa02m-web-auth-lib.sh"
            install -m 644 "$TMPDIR/repo/$src" "$tgt" && sed -i 's/\r$//' "$tgt"
        elif [ "$src" = "etc/sa02m-web-build-lib.sh" ]; then
            tgt="/usr/local/lib/sa02m-web-build-lib.sh"
            install -m 644 "$TMPDIR/repo/$src" "$tgt" && sed -i 's/\r$//' "$tgt"
        else
            install -m 755 "$TMPDIR/repo/$src" "$tgt" && sed -i 's/\r$//' "$tgt"
        fi
        log "Обновлён $tgt"
    fi
done
if [ -x /usr/local/sbin/sa02m-repair-web-env ]; then
    /usr/local/sbin/sa02m-repair-web-env >>"$LOGFILE" 2>&1 || true
fi

# Записываем задеплоенный коммит
NEW_COMMIT=$(git -C "$TMPDIR/repo" rev-parse HEAD 2>/dev/null || echo "")
if [ -n "$NEW_COMMIT" ]; then
    printf '%s\n' "$NEW_COMMIT" > "$STATEDIR/deployed_commit"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$STATEDIR/deployed_at"
    chmod 644 "$STATEDIR/deployed_commit" "$STATEDIR/deployed_at" 2>/dev/null || true
    log "Задеплоен коммит $NEW_COMMIT"
fi

if type write_web_build_conf_branch >/dev/null 2>&1; then
    conf_branch="$BRANCH"
    if type read_local_web_version >/dev/null 2>&1; then
        v=$(read_local_web_version 2>/dev/null || true)
        [ -n "$v" ] && conf_branch="$v"
    fi
    write_web_build_conf_branch "$conf_branch" && log "Записан /etc/sa02m_web_build.conf branch=$conf_branch"
fi

# Сбрасываем кэш check.json
rm -f "$STATEDIR/check.json" 2>/dev/null || true
# Сбрасываем кэш status.cgi (JS/CSS обновились — новая версия в браузере сразу)
rm -f /tmp/sa02m_status_cache/*.json /tmp/sa02m_status_cache/*.lock 2>/dev/null || true

log "DONE: обновление завершено успешно"
printf 'done' > "$STATUS_FILE"
