#!/bin/bash
# Безопасное чтение/запись /etc/sa02m_web.env (без command substitution при source).
# Используется CGI и sa02m-commit-web-env / sa02m-repair-web-env.

# ── Server-side sessions ────────────────────────────────────────────────────
# Replaces the former fixed token `cyntron_session` (identical on every device,
# committed → any LAN client could forge the cookie). A login now mints a random
# token stored server-side with an expiry; every endpoint validates the cookie
# against that store; logout / password change revoke it. Fail CLOSED: if the
# store is unreadable or the token is absent/expired, access is denied.
SA02M_SESSION_DIR="${SA02M_SESSION_DIR:-/run/sa02m-web-sessions}"
SA02M_SESSION_TTL="${SA02M_SESSION_TTL:-864000}"   # 10 days, matches the cookie Max-Age

web_session__ensure_dir() {
    [ -d "$SA02M_SESSION_DIR" ] && return 0
    mkdir -p "$SA02M_SESSION_DIR" 2>/dev/null || return 1
    chmod 700 "$SA02M_SESSION_DIR" 2>/dev/null || true
    return 0
}

# Print a fresh 32-byte hex token to stdout (nothing else).
web_session__gen_token() {
    if [ -r /dev/urandom ]; then
        head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
    else
        # Fallback — still unpredictable enough for a LAN device, but urandom is
        # expected to exist on the target.
        printf '%s%s%s' "$$" "$(date +%s%N 2>/dev/null)" "$RANDOM" | sha256sum | cut -d' ' -f1
    fi
}

# Extract the session_token value from the Cookie header (hex only).
web_session__cookie_token() {
    local c="${HTTP_COOKIE:-}"
    [[ "$c" =~ session_token=([a-f0-9]{32,128}) ]] || return 1
    printf '%s' "${BASH_REMATCH[1]}"
}

# Create a session, print its token. Returns 1 if the store cannot be written.
web_session_create() {
    web_session__ensure_dir || return 1
    local tok f
    tok=$(web_session__gen_token)
    [ -n "$tok" ] || return 1
    f="$SA02M_SESSION_DIR/$tok"
    : > "$f" 2>/dev/null || return 1
    chmod 600 "$f" 2>/dev/null || true
    printf '%s' "$tok"
}

# Validate the cookie's token against the store, pruning it if expired.
# Returns 0 (valid) / 1 (missing, unknown, or expired) — fail closed.
web_session_check_cookie() {
    local tok f age now mtime
    tok=$(web_session__cookie_token) || return 1
    f="$SA02M_SESSION_DIR/$tok"
    [ -f "$f" ] || return 1
    now=$(date +%s 2>/dev/null) || return 1
    mtime=$(stat -c %Y "$f" 2>/dev/null) || return 1
    age=$(( now - mtime ))
    if [ "$age" -lt 0 ] || [ "$age" -gt "$SA02M_SESSION_TTL" ]; then
        rm -f "$f" 2>/dev/null
        return 1
    fi
    return 0
}

# Revoke the session named by the current cookie (logout).
web_session_destroy_cookie() {
    local tok
    tok=$(web_session__cookie_token) || return 0
    rm -f "$SA02M_SESSION_DIR/$tok" 2>/dev/null
    return 0
}

# Revoke ALL sessions (called on password/username change).
web_session_destroy_all() {
    [ -d "$SA02M_SESSION_DIR" ] || return 0
    rm -f "$SA02M_SESSION_DIR"/* 2>/dev/null
    return 0
}


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
