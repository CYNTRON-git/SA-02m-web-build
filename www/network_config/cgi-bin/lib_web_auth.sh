#!/bin/bash
# Безопасное чтение/запись /etc/sa02m_web.env (без command substitution при source).
# Используется CGI и sa02m-commit-web-env / sa02m-repair-web-env.
#
# Пароль хранится хэшированным в SA02M_WEB_PASS_HASH ($6$ = sha512crypt).
# SA02M_WEB_PASS (открытый) поддерживается только для чтения legacy-файлов —
# новые записи всегда хэшированы. web_auth_verify_password даёт единую проверку.
#
# ВНИМАНИЕ: файл дублируется в двух местах и должен совпадать байт-в-байт:
#   etc/sa02m-web-auth-lib.sh            → /usr/local/lib/sa02m-web-auth-lib.sh (root: commit/repair)
#   www/network_config/cgi-bin/lib_web_auth.sh (www-data: login.cgi / web_creds.cgi)

web_auth__strip_quotes() {
    local v="$1"
    case "$v" in
        \"*\") v="${v#\"}"; v="${v%\"}" ;;
        \'*\') v="${v#\'}"; v="${v%\'}" ;;
    esac
    printf '%s' "$v"
}

web_auth__escape_sq() {
    printf '%s' "$1" | sed "s/'/'\\\\''/g"
}

# ── Хэширование пароля (sha512crypt) ────────────────────────────────────────
web_auth_hash_password() {
    # <plaintext> → строка вида $6$<salt>$<hash>; пусто при ошибке
    local plain="$1" salt
    salt=$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-16)
    [ -n "$salt" ] || return 1
    openssl passwd -6 -salt "$salt" "$plain" 2>/dev/null
}

# Проверить plaintext против SA02M_WEB_PASS_HASH (приоритет) или legacy SA02M_WEB_PASS.
# Требует предварительного web_auth_read/read_safe. 0 — совпало.
web_auth_verify_password() {
    local plain="$1" salt calc
    if [ -n "${SA02M_WEB_PASS_HASH:-}" ]; then
        case "$SA02M_WEB_PASS_HASH" in
            \$6\$*)
                salt=${SA02M_WEB_PASS_HASH#\$6\$}
                salt=${salt%%\$*}
                calc=$(openssl passwd -6 -salt "$salt" "$plain" 2>/dev/null)
                [ -n "$calc" ] && [ "$calc" = "$SA02M_WEB_PASS_HASH" ]
                return $?
                ;;
            *) return 1 ;;
        esac
    fi
    [ -n "${SA02M_WEB_PASS:-}" ] && [ "$plain" = "$SA02M_WEB_PASS" ]
}

# Запись хэшированного формата: SA02M_WEB_USER='...' / SA02M_WEB_PASS_HASH='...'
web_auth_write_hashed() {
    local user="$1" hash="$2" qu qh
    qu=$(web_auth__escape_sq "$user")
    qh=$(web_auth__escape_sq "$hash")
    printf "SA02M_WEB_USER='%s'\nSA02M_WEB_PASS_HASH='%s'\n" "$qu" "$qh"
}

# Legacy-запись открытого пароля (оставлено для совместимости; не для новых данных).
web_auth_write() {
    local user="$1" pass="$2"
    local qu qp
    qu=$(web_auth__escape_sq "$user")
    qp=$(web_auth__escape_sq "$pass")
    printf "SA02M_WEB_USER='%s'\nSA02M_WEB_PASS='%s'\n" "$qu" "$qp"
}

web_auth_read_safe() {
    local f="$1" line val
    SA02M_WEB_USER=""
    SA02M_WEB_PASS=""
    SA02M_WEB_PASS_HASH=""
    [ -f "$f" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%$'\r'}"
        case "$line" in
            SA02M_WEB_USER=*)      val="${line#SA02M_WEB_USER=}";      SA02M_WEB_USER=$(web_auth__strip_quotes "$val") ;;
            SA02M_WEB_PASS_HASH=*) val="${line#SA02M_WEB_PASS_HASH=}"; SA02M_WEB_PASS_HASH=$(web_auth__strip_quotes "$val") ;;
            SA02M_WEB_PASS=*)      val="${line#SA02M_WEB_PASS=}";      SA02M_WEB_PASS=$(web_auth__strip_quotes "$val") ;;
        esac
    done < "$f"
    [ -n "$SA02M_WEB_USER" ] && { [ -n "$SA02M_WEB_PASS" ] || [ -n "$SA02M_WEB_PASS_HASH" ]; }
}

# Файл с $(...) / ` / ; — legacy: один раз eval в subshell, затем переписать quoted.
web_auth_read() {
    local f="$1"
    [ -f "$f" ] || return 1
    if grep -qE '\$\(|`|;[[:space:]]*[^[:space:]]|\|' "$f" 2>/dev/null; then
        SA02M_WEB_USER=$( ( unset SA02M_WEB_USER SA02M_WEB_PASS; # shellcheck disable=SC1090
            . "$f"; printf '%s' "${SA02M_WEB_USER:-admin}" ) )
        SA02M_WEB_PASS=$( ( unset SA02M_WEB_USER SA02M_WEB_PASS; # shellcheck disable=SC1090
            . "$f"; printf '%s' "${SA02M_WEB_PASS:-cyntron}" ) )
        SA02M_WEB_PASS_HASH=$( ( unset SA02M_WEB_USER SA02M_WEB_PASS SA02M_WEB_PASS_HASH; # shellcheck disable=SC1090
            . "$f"; printf '%s' "${SA02M_WEB_PASS_HASH:-}" ) )
        return 0
    fi
    web_auth_read_safe "$f"
}

web_auth_needs_repair() {
    local f="$1"
    [ -f "$f" ] || return 1
    grep -qE '\$\(|`|;|\|' "$f" 2>/dev/null
}

web_auth_repair_file() {
    local f="$1" tmp
    [ -f "$f" ] || return 0
    web_auth_needs_repair "$f" || return 0
    web_auth_read "$f" || return 1
    tmp="${f}.repair.$$"
    if [ -n "${SA02M_WEB_PASS_HASH:-}" ]; then
        web_auth_write_hashed "$SA02M_WEB_USER" "$SA02M_WEB_PASS_HASH" > "$tmp"
    else
        web_auth_write "$SA02M_WEB_USER" "$SA02M_WEB_PASS" > "$tmp"
    fi
    install -m 640 -o root -g www-data "$tmp" "$f"
    rm -f "$tmp"
}

web_auth_validate_staging() {
    local f="$1" u p h
    [ -f "$f" ] || return 1
    web_auth_read_safe "$f" || return 1
    u="$SA02M_WEB_USER"; p="$SA02M_WEB_PASS"; h="${SA02M_WEB_PASS_HASH:-}"
    [[ "$u" =~ ^[a-zA-Z0-9_.-]{1,32}$ ]] || return 1
    # Предпочтительный путь — хэш sha512crypt.
    if [ -n "$h" ]; then
        [[ "$h" =~ ^\$6\$[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]+$ ]] || return 1
        return 0
    fi
    # Legacy: валидация открытого пароля (запрет shell-метасимволов).
    [ "${#p}" -ge 4 ] && [ "${#p}" -le 128 ] || return 1
    [[ "$p" != *$'\n'* ]] || return 1
    case "$p" in
        *'$'*|*'`'*|*';'*|*'|'*|*'&'*|*'<'*|*'>'*|*'('*|*')'*) return 1 ;;
    esac
    return 0
}
