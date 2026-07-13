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

# ── Password hashing (S5) — mirror of www/network_config/cgi-bin/lib_web_auth.sh.
# Store a $6$ SHA-512 crypt hash; legacy plaintext stays readable/comparable.
# Best-effort: callers fall back to plaintext where no hasher exists.
web_auth_hash() {
    local plain="$1" salt
    salt=$(head -c 12 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n' | cut -c1-16)
    [ -n "$salt" ] || salt="sa02m$$"
    if command -v openssl >/dev/null 2>&1; then
        openssl passwd -6 -salt "$salt" "$plain" 2>/dev/null && return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import crypt,sys; print(crypt.crypt(sys.argv[1], "$6$"+sys.argv[2]))' "$plain" "$salt" 2>/dev/null && return 0
    fi
    return 1
}

web_auth_is_hash() {
    case "$1" in '$6$'*) return 0 ;; *) return 1 ;; esac
}

web_auth_verify() {
    local plain="$1" stored="$2" salt rest recomputed s h1 h2
    if web_auth_is_hash "$stored"; then
        rest="${stored#\$6\$}"; salt="${rest%%\$*}"
        if command -v openssl >/dev/null 2>&1; then
            recomputed=$(openssl passwd -6 -salt "$salt" "$plain" 2>/dev/null)
        elif command -v python3 >/dev/null 2>&1; then
            recomputed=$(python3 -c 'import crypt,sys; print(crypt.crypt(sys.argv[1], sys.argv[2]))' "$plain" "$stored" 2>/dev/null)
        else
            return 1
        fi
        [ -n "$recomputed" ] && [ "$recomputed" = "$stored" ]
        return
    fi
    s=$(head -c 16 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n')
    h1=$(printf '%s' "${s}:$plain"  | sha256sum | cut -d' ' -f1)
    h2=$(printf '%s' "${s}:$stored" | sha256sum | cut -d' ' -f1)
    [ "$h1" = "$h2" ]
}

# Write USER + credential; a $6$ hash goes to PASS_HASH, else legacy PASS.
web_auth_write() {
    local user="$1" secret="$2" qu qp
    qu=$(web_auth__escape_sq "$user")
    qp=$(web_auth__escape_sq "$secret")
    if web_auth_is_hash "$secret"; then
        printf "SA02M_WEB_USER='%s'\nSA02M_WEB_PASS_HASH='%s'\n" "$qu" "$qp"
    else
        printf "SA02M_WEB_USER='%s'\nSA02M_WEB_PASS='%s'\n" "$qu" "$qp"
    fi
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
            SA02M_WEB_USER=*) val="${line#SA02M_WEB_USER=}"; SA02M_WEB_USER=$(web_auth__strip_quotes "$val") ;;
            SA02M_WEB_PASS_HASH=*) val="${line#SA02M_WEB_PASS_HASH=}"; SA02M_WEB_PASS_HASH=$(web_auth__strip_quotes "$val") ;;
            SA02M_WEB_PASS=*) val="${line#SA02M_WEB_PASS=}"; SA02M_WEB_PASS=$(web_auth__strip_quotes "$val") ;;
        esac
    done < "$f"
    [ -n "$SA02M_WEB_USER" ] && { [ -n "$SA02M_WEB_PASS" ] || [ -n "$SA02M_WEB_PASS_HASH" ]; }
}

web_auth_stored_secret() {
    if [ -n "${SA02M_WEB_PASS_HASH:-}" ]; then printf '%s' "$SA02M_WEB_PASS_HASH";
    else printf '%s' "${SA02M_WEB_PASS:-}"; fi
}

# Read credentials WITHOUT sourcing the file (S9) — no eval of config content.
# The safe parser reads values literally; a password written by web_auth_write
# is always single-quoted, so a metachar-bearing value is a literal to re-quote,
# never a command to run.
web_auth_read() {
    web_auth_read_safe "$1"
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
    web_auth_write "$SA02M_WEB_USER" "$(web_auth_stored_secret)" > "$tmp"
    install -m 640 -o root -g www-data "$tmp" "$f"
    rm -f "$tmp"
}

web_auth_validate_staging() {
    local f="$1" u p
    [ -f "$f" ] || return 1
    web_auth_read_safe "$f" || return 1
    u="$SA02M_WEB_USER"
    [[ "$u" =~ ^[a-zA-Z0-9_.-]{1,32}$ ]] || return 1
    if [ -n "${SA02M_WEB_PASS_HASH:-}" ]; then
        [[ "$SA02M_WEB_PASS_HASH" =~ ^\$6\$[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}$ ]] || return 1
        return 0
    fi
    p="$SA02M_WEB_PASS"
    [ "${#p}" -ge 4 ] && [ "${#p}" -le 128 ] || return 1
    [[ "$p" != *$'\n'* ]] || return 1
    case "$p" in
        *'$'*|*'`'*|*';'*|*'|'*|*'&'*|*'<'*|*'>'*|*'('*|*')'*) return 1 ;;
    esac
    return 0
}
