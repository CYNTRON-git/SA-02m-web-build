#!/bin/bash
read -r -n "${CONTENT_LENGTH:-0}" POST_DATA

decode() { printf '%b' "$(echo "$1" | sed 's/+/ /g; s/%/\\x/g')"; }

get_field() {
    local val
    val=$(echo "$POST_DATA" | tr '&' '\n' | grep "^${1}=" | cut -d= -f2-)
    decode "$val"
}

USERNAME=$(get_field "username")
PASSWORD=$(get_field "password")

SCRIPT_DIR="$(dirname "$0")"
AUTH_ENV="/etc/sa02m_web.env"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib_web_auth.sh"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib_web_session.sh"

fail_redirect() {  # $1 = login.html error code
    echo "Status: 302 Found"
    echo "Content-type: text/html; charset=UTF-8"
    echo "Location: /login.html?error=$1"
    echo ""
    exit 0
}

# ── Rate-limit по IP (защита от перебора) ───────────────────────────────────
# Состояние в tmpfs; недоступность каталога не открывает доступ — лишь снимает
# лимит (аутентификация всё равно обязательна). Плюс фиксированная задержка на отказ.
RL_DIR=/run/sa02m-web-login
RL_WINDOW=300
RL_MAXFAIL=8
NOW=$(date +%s)
IPKEY=$(printf '%s' "${REMOTE_ADDR:-unknown}" | tr -c 'A-Za-z0-9' '_')
RLFILE="$RL_DIR/$IPKEY"

rl_count() {
    local n=0 ts
    [ -f "$RLFILE" ] || { echo 0; return; }
    while read -r ts; do
        case "$ts" in ''|*[!0-9]*) continue ;; esac
        [ "$((NOW - ts))" -le "$RL_WINDOW" ] && n=$((n + 1))
    done < "$RLFILE"
    echo "$n"
}

rl_add_failure() {
    [ -d "$RL_DIR" ] || return 0
    local tmp="$RLFILE.$$" ts
    {
        [ -f "$RLFILE" ] && while read -r ts; do
            case "$ts" in ''|*[!0-9]*) continue ;; esac
            [ "$((NOW - ts))" -le "$RL_WINDOW" ] && printf '%s\n' "$ts"
        done < "$RLFILE"
        printf '%s\n' "$NOW"
    } > "$tmp" 2>/dev/null && mv "$tmp" "$RLFILE" 2>/dev/null || rm -f "$tmp" 2>/dev/null
}

if [ "$(rl_count)" -ge "$RL_MAXFAIL" ]; then
    sleep 2
    fail_redirect locked
fi

# ── Проверка учётных данных ─────────────────────────────────────────────────
SA02M_WEB_USER=admin
SA02M_WEB_PASS=cyntron
SA02M_WEB_PASS_HASH=""
if [ -f "$AUTH_ENV" ]; then
    web_auth_read "$AUTH_ENV" || true
fi
: "${SA02M_WEB_USER:=admin}"

if [ "$USERNAME" = "$SA02M_WEB_USER" ] && web_auth_verify_password "$PASSWORD"; then
    rm -f "$RLFILE" 2>/dev/null || true
    TOKEN=$(web_session_create "$SA02M_WEB_USER")
    if [ -z "$TOKEN" ]; then
        # Не удалось создать сессию (нет каталога/urandom) — не пускаем молча.
        fail_redirect session
    fi
    SECURE=""
    [ "${HTTPS:-}" = "on" ] && SECURE="; Secure"
    # Нудж на смену дефолтного пароля (admin/cyntron) — баннер в UI.
    LOCATION="/"
    if [ "$USERNAME" = "admin" ] && [ "$PASSWORD" = "cyntron" ]; then
        LOCATION="/?warn=default_creds"
    fi
    echo "Status: 302 Found"
    echo "Content-type: text/html; charset=UTF-8"
    # Без HttpOnly: guard в app.js/login.html читает document.cookie (HttpOnly в JS не виден → вечный редирект на логин)
    echo "Set-Cookie: session_token=${TOKEN}; Path=/; SameSite=Lax; Max-Age=864000${SECURE}"
    echo "Location: $LOCATION"
    echo ""
else
    rl_add_failure
    sleep 1
    fail_redirect 1
fi
