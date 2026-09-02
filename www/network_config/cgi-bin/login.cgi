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

# Brute-force throttle (M4): a locked-out client is refused BEFORE the password
# is checked, with a distinct error so login.html can explain the wait. Fails
# open on a broken /run — a legitimate operator is never permanently locked out.
if ! web_login_check; then
    echo "Status: 302 Found"
    echo "Content-type: text/html; charset=UTF-8"
    echo "Location: /login.html?error=locked"
    echo ""
    exit 0
fi

# Username: constant-time-ish salted-hash compare. Password: verified against
# the stored credential — a $6$ hash (S5) or legacy plaintext (migration).
SALT=$(head -c 16 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n')
hash_of() { printf '%s' "${SALT}:$1" | sha256sum | cut -d' ' -f1; }
u_in=$(hash_of "$USERNAME"); u_ok=$(hash_of "$SA02M_WEB_USER")

if [ "$u_in" = "$u_ok" ] && web_auth_verify "$PASSWORD" "$STORED_SECRET"; then
    web_login_record_success
    TOKEN=$(web_session_create) || fail_login
    CSRF=$(web_csrf_token_for_session "$TOKEN" 2>/dev/null || true)
    echo "Status: 302 Found"
    echo "Content-type: text/html; charset=UTF-8"
    # Без HttpOnly: guard в app.js/login.html читает document.cookie (HttpOnly в JS не виден → вечный редирект на логин)
    echo "Set-Cookie: session_token=${TOKEN}; Path=/; SameSite=Lax; Max-Age=864000"
    # CSRF mirror cookie (JS-readable) — value matches /run/sa02m-web-sessions/<hash>.csrf
    if [ -n "$CSRF" ]; then
        echo "Set-Cookie: sa02m_csrf=${CSRF}; Path=/; SameSite=Lax; Max-Age=864000"
    fi
    echo "Location: /"
    echo ""
else
    web_login_record_failure
    # A short delay slows scripted guessing before the window lockout trips.
    # HONEST LIMIT (security review 1.0.6.24): this does NOT protect the worker
    # pool — fcgiwrap has a fixed number of children and every CGI on the panel
    # shares them, so N concurrent bogus logins hold N workers for a second
    # each. It is bounded per request, not in aggregate. The lockout above is
    # what actually stops a sustained attempt; this only raises the cost of the
    # first MAXFAIL guesses.
    sleep 1
    fail_login
fi
