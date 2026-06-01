#!/bin/bash
# Безопасное чтение/запись /etc/sa02m_web.env (без command substitution при source).
# Используется CGI и sa02m-commit-web-env / sa02m-repair-web-env.

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

# Запись в формате SA02M_WEB_USER='...' / SA02M_WEB_PASS='...'
web_auth_write() {
    local user="$1" pass="$2"
    local qu qp
    qu=$(web_auth__escape_sq "$user")
    qp=$(web_auth__escape_sq "$pass")
    printf "SA02M_WEB_USER='%s'\nSA02M_WEB_PASS='%s'\n" "$qu" "$qp"
}

web_auth_read_safe() {
    local f="$1" line key val
    SA02M_WEB_USER=""
    SA02M_WEB_PASS=""
    [ -f "$f" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%$'\r'}"
        case "$line" in
            SA02M_WEB_USER=*) val="${line#SA02M_WEB_USER=}"; SA02M_WEB_USER=$(web_auth__strip_quotes "$val") ;;
            SA02M_WEB_PASS=*) val="${line#SA02M_WEB_PASS=}"; SA02M_WEB_PASS=$(web_auth__strip_quotes "$val") ;;
        esac
    done < "$f"
    [ -n "$SA02M_WEB_USER" ] && [ -n "$SA02M_WEB_PASS" ]
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
    web_auth_write "$SA02M_WEB_USER" "$SA02M_WEB_PASS" > "$tmp"
    install -m 640 -o root -g www-data "$tmp" "$f"
    rm -f "$tmp"
}

web_auth_validate_staging() {
    local f="$1" u p
    [ -f "$f" ] || return 1
    web_auth_read_safe "$f" || return 1
    u="$SA02M_WEB_USER" p="$SA02M_WEB_PASS"
    [[ "$u" =~ ^[a-zA-Z0-9_.-]{1,32}$ ]] || return 1
    [ "${#p}" -ge 4 ] && [ "${#p}" -le 128 ] || return 1
    [[ "$p" != *$'\n'* ]] || return 1
    case "$p" in
        *'$'*|*'`'*|*';'*|*'|'*|*'&'*|*'<'*|*'>'*|*'('*|*')'*) return 1 ;;
    esac
    return 0
}
