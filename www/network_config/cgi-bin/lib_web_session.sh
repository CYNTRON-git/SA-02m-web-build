#!/bin/bash
# Серверные веб-сессии SA-02м.
#
# Каждому успешному логину выдаётся случайный per-session токен (не общая
# константа). Хранилище — каталог в tmpfs; имя файла = sha256(token), так что
# в листинге видны только необратимые хэши, а не сами токены. Содержимое файла:
# "<expiry_epoch> <username>". TTL скользящий (продлевается при каждой валидной
# проверке).
#
# Ту же схему проверяет демон sa02m-flasher (opt/.../auth.py::check_session_store);
# хэш обязан совпадать байт-в-байт: bash `printf '%s' "$tok" | sha256sum`
# эквивалентен python `hashlib.sha256(tok.encode()).hexdigest()`.

# Каталог сессий (для тестов переопределяется через SA02M_WEB_SESSION_DIR).
web_session_dir() {
    printf '%s' "${SA02M_WEB_SESSION_DIR:-/run/sa02m-web-sessions}"
}

# TTL сессии, секунд (10 дней — как прежний Max-Age).
: "${SA02M_WEB_SESSION_TTL:=864000}"

web_session__sha256() {
    # stdin → hex sha256 (первое поле)
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    else
        openssl dgst -sha256 2>/dev/null | sed 's/^.*= *//'
    fi
}

# Извлечь и провалидировать значение session_token из заголовка Cookie.
# Печатает токен и возвращает 0 при успехе; иначе — непустой код возврата.
web_session_from_cookie() {
    local header="$1" tok
    [ -n "$header" ] || return 1
    tok=$(printf '%s' "$header" | tr ';' '\n' \
        | sed -n 's/^[[:space:]]*session_token=\(.*\)$/\1/p' | head -1 \
        | tr -d '[:space:]')
    [ -n "$tok" ] || return 1
    # Токен — только hex фиксированного диапазона длин (защита от подстановки пути).
    case "$tok" in
        *[!a-f0-9]*) return 1 ;;
    esac
    [ "${#tok}" -ge 32 ] && [ "${#tok}" -le 128 ] || return 1
    printf '%s' "$tok"
}

web_session_new_token() {
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

# Создать сессию для пользователя, напечатать токен. Код возврата !=0 при ошибке.
web_session_create() {
    local user="$1" dir tok hash exp
    dir=$(web_session_dir)
    [ -d "$dir" ] || return 1
    tok=$(web_session_new_token)
    [ -n "$tok" ] || return 1
    hash=$(printf '%s' "$tok" | web_session__sha256)
    [ -n "$hash" ] || return 1
    exp=$(( $(date +%s) + SA02M_WEB_SESSION_TTL ))
    # 027 → файл 640, группа наследуется от setgid-каталога (www-data): демон
    # sa02m-flasher читает по группе, посторонние — нет. Токен в файле не хранится.
    ( umask 027; printf '%s %s\n' "$exp" "${user:-admin}" > "$dir/$hash" ) || return 1
    printf '%s' "$tok"
}

# Внутренний токен для серверных вызовов (root-хелперы, health-пробы). Значение —
# per-device, генерируется установщиком в файл 640 root:www-data (не в репозитории).
web_session__internal_token() {
    local f="${SA02M_WEB_INTERNAL_TOKEN_FILE:-/etc/sa02m-web-internal-token}" t
    [ -r "$f" ] || return 1
    # Файл токена без завершающего перевода строки → read вернёт !=0 на EOF, но t
    # уже заполнен; не завершаемся по этому коду.
    IFS= read -r t < "$f" 2>/dev/null || :
    t=$(printf '%s' "$t" | tr -d '[:space:]')
    [ -n "$t" ] || return 1
    printf '%s' "$t"
}

# Проверить токен: 0 — валиден (и продлён), 1 — нет.
web_session_valid() {
    local tok="$1" dir hash f line exp now user it
    [ -n "$tok" ] || return 1
    # Внутренний токен серверных вызовов (не сессия, не продлевается).
    it=$(web_session__internal_token 2>/dev/null || true)
    if [ -n "$it" ] && [ "$tok" = "$it" ]; then
        return 0
    fi
    dir=$(web_session_dir)
    hash=$(printf '%s' "$tok" | web_session__sha256)
    [ -n "$hash" ] || return 1
    f="$dir/$hash"
    [ -f "$f" ] || return 1
    IFS= read -r line < "$f" || return 1
    exp="${line%% *}"
    case "$exp" in ''|*[!0-9]*) return 1 ;; esac
    now=$(date +%s)
    if [ "$now" -ge "$exp" ]; then
        rm -f "$f" 2>/dev/null
        return 1
    fi
    # Скользящее продление.
    user="${line#* }"
    ( umask 027; printf '%s %s\n' "$(( now + SA02M_WEB_SESSION_TTL ))" "$user" > "$f" ) 2>/dev/null || true
    return 0
}

web_session_destroy() {
    local tok="$1" dir hash
    [ -n "$tok" ] || return 0
    dir=$(web_session_dir)
    hash=$(printf '%s' "$tok" | web_session__sha256)
    [ -n "$hash" ] || return 0
    rm -f "$dir/$hash" 2>/dev/null || true
}

# Обёртка для CGI: проверить заголовок Cookie целиком (fail-closed).
web_session_check_cookie() {
    local tok
    tok=$(web_session_from_cookie "$1") || return 1
    web_session_valid "$tok"
}
