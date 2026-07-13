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

AUTH_ENV="/etc/sa02m_web.env"
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"

fail_login() {
    echo "Status: 302 Found"
    echo "Content-type: text/html; charset=UTF-8"
    echo "Location: /login.html?error=1"
    echo ""
    exit 0
}

# Fail CLOSED: if the credentials file is missing or unreadable, deny login —
# never default to a built-in admin/cyntron (that was a fail-open to a committed
# default). A device without a provisioned env cannot be logged into until the
# installer writes one.
SA02M_WEB_USER=""
SA02M_WEB_PASS=""
SA02M_WEB_PASS_HASH=""
if [ -f "$AUTH_ENV" ]; then
    web_auth_read_safe "$AUTH_ENV" || true
fi
STORED_SECRET=$(web_auth_stored_secret)
if [ -z "$SA02M_WEB_USER" ] || [ -z "$STORED_SECRET" ]; then
    fail_login
fi

# Username: constant-time-ish salted-hash compare. Password: verified against
# the stored credential — a $6$ hash (S5) or legacy plaintext (migration).
SALT=$(head -c 16 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n')
hash_of() { printf '%s' "${SALT}:$1" | sha256sum | cut -d' ' -f1; }
u_in=$(hash_of "$USERNAME"); u_ok=$(hash_of "$SA02M_WEB_USER")

if [ "$u_in" = "$u_ok" ] && web_auth_verify "$PASSWORD" "$STORED_SECRET"; then
    TOKEN=$(web_session_create) || fail_login
    echo "Status: 302 Found"
    echo "Content-type: text/html; charset=UTF-8"
    # Без HttpOnly: guard в app.js/login.html читает document.cookie (HttpOnly в JS не виден → вечный редирект на логин)
    echo "Set-Cookie: session_token=${TOKEN}; Path=/; SameSite=Lax; Max-Age=864000"
    echo "Location: /"
    echo ""
else
    fail_login
fi
