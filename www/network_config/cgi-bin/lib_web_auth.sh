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
    # setgid + group-accessible: the sa02m-flasher daemon runs in the www-data
    # group and reads the session files by group, so the dir must be traversable
    # by the group (2750) and files group-readable (640, see web_session_create).
    chmod 2750 "$SA02M_SESSION_DIR" 2>/dev/null || true
    return 0
}

# sha256 of the token. The session file is named by this hash — never the raw
# token — so listing the store never reveals a live token, and it MUST match the
# flasher daemon (opt/sa02m-flasher/sa02m_flasher/auth.py) and the login layer.
web_session__hash() {
    printf '%s' "$1" | sha256sum 2>/dev/null | cut -d' ' -f1
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
# File name = sha256(token); content = "<expiry_epoch> <user>" — the daemon reads
# the expiry from the content, and the file is group-readable (640) so it can.
web_session_create() {
    web_session__ensure_dir || return 1
    local tok hash f exp user
    tok=$(web_session__gen_token)
    [ -n "$tok" ] || return 1
    hash=$(web_session__hash "$tok")
    [ -n "$hash" ] || return 1
    exp=$(( $(date +%s 2>/dev/null) + SA02M_SESSION_TTL ))
    user="${1:-${SA02M_WEB_USER:-admin}}"
    f="$SA02M_SESSION_DIR/$hash"
    ( umask 027; printf '%s %s\n' "$exp" "$user" > "$f" ) 2>/dev/null || return 1
    printf '%s' "$tok"
}

# Validate the cookie's token against the store, pruning it if expired.
# Returns 0 (valid) / 1 (missing, unknown, or expired) — fail closed. Expiry
# lives in the file content (first field), matching the daemon's read-only check.
web_session_check_cookie() {
    local tok hash f line exp now user
    tok=$(web_session__cookie_token) || return 1
    hash=$(web_session__hash "$tok") || return 1
    f="$SA02M_SESSION_DIR/$hash"
    [ -f "$f" ] || return 1
    IFS= read -r line < "$f" 2>/dev/null || return 1
    exp="${line%% *}"
    case "$exp" in ''|*[!0-9]*) return 1 ;; esac
    now=$(date +%s 2>/dev/null) || return 1
    if [ "$now" -ge "$exp" ]; then
        rm -f "$f" 2>/dev/null
        return 1
    fi
    # Sliding renewal — the CGI layer owns the files; the daemon only reads.
    user="${line#* }"
    [ "$user" = "$line" ] && user="${SA02M_WEB_USER:-admin}"
    ( umask 027; printf '%s %s\n' "$(( now + SA02M_SESSION_TTL ))" "$user" > "$f" ) 2>/dev/null || true
    return 0
}

# Revoke the session named by the current cookie (logout).
web_session_destroy_cookie() {
    local tok hash
    tok=$(web_session__cookie_token) || return 0
    hash=$(web_session__hash "$tok") || return 0
    rm -f "$SA02M_SESSION_DIR/$hash" 2>/dev/null
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

# ── Password hashing (S5) ───────────────────────────────────────────────────
# Store a SHA-512 crypt hash ($6$…) instead of the plaintext password. Legacy
# plaintext files stay readable and comparable (migration: a device keeps
# working; the password is hashed on its next change). Hashing is BEST-EFFORT —
# if no hasher is available, callers fall back to plaintext so a login can never
# be locked out by a missing tool.

# Print a $6$ crypt hash of $1, or nothing on failure.
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

# Is $1 a stored SHA-512 crypt hash?
web_auth_is_hash() {
    case "$1" in
        '$6$'*) return 0 ;;
        *) return 1 ;;
    esac
}

# Verify plaintext $1 against stored credential $2 (hash or legacy plaintext).
web_auth_verify() {
    local plain="$1" stored="$2" salt rest recomputed
    if web_auth_is_hash "$stored"; then
        # $6$<salt>$<hash> — recompute with the same salt and compare.
        rest="${stored#\$6\$}"
        salt="${rest%%\$*}"
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
    # Legacy plaintext — constant-time-ish compare via salted hashes.
    local s h1 h2
    s=$(head -c 16 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n')
    h1=$(printf '%s' "${s}:$plain"  | sha256sum | cut -d' ' -f1)
    h2=$(printf '%s' "${s}:$stored" | sha256sum | cut -d' ' -f1)
    [ "$h1" = "$h2" ]
}

# Write SA02M_WEB_USER + credential. The credential ($2) is stored as
# SA02M_WEB_PASS_HASH when it is already a $6$ hash, else as legacy plaintext
# SA02M_WEB_PASS. Callers pass a hash (from web_auth_hash) where they can and
# fall back to plaintext where hashing is unavailable.
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

# The stored credential to compare against: hash if present, else legacy plaintext.
web_auth_stored_secret() {
    if [ -n "${SA02M_WEB_PASS_HASH:-}" ]; then printf '%s' "$SA02M_WEB_PASS_HASH";
    else printf '%s' "${SA02M_WEB_PASS:-}"; fi
}

# Read credentials WITHOUT sourcing the file (S9). The former legacy path
# `. "$f"` evaluated a config file containing $(...)/`/;/| — a code-exec-on-
# config surface. The safe parser reads every value literally, which is also
# the CORRECT interpretation for the repair flow: a password written by
# web_auth_write is always single-quoted, so a metachar-bearing value is a
# literal string to re-quote, never a command to run.
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
    # A hashed credential: accept a well-formed $6$ crypt string, nothing else.
    if [ -n "${SA02M_WEB_PASS_HASH:-}" ]; then
        [[ "$SA02M_WEB_PASS_HASH" =~ ^\$6\$[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}$ ]] || return 1
        return 0
    fi
    # Legacy plaintext: bounded length, no newline or shell metacharacters.
    p="$SA02M_WEB_PASS"
    [ "${#p}" -ge 4 ] && [ "${#p}" -le 128 ] || return 1
    [[ "$p" != *$'\n'* ]] || return 1
    case "$p" in
        *'$'*|*'`'*|*';'*|*'|'*|*'&'*|*'<'*|*'>'*|*'('*|*')'*) return 1 ;;
    esac
    return 0
}
