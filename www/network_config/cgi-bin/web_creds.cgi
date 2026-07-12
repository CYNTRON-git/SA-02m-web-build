#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

web_session_check_cookie || {
  echo '{"error":"unauthorized"}'
  exit 0
}

read -r -n "${CONTENT_LENGTH:-0}" POST_DATA

decode() { printf '%b' "$(echo "$1" | sed 's/+/ /g; s/%/\\x/g')"; }
get_f() { decode "$(echo "$POST_DATA" | tr '&' '\n' | grep "^${1}=" | cut -d= -f2-)"; }

CUR=$(get_f "current_password")
NEWU=$(get_f "new_username" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
NEWP=$(get_f "new_password")
NEWP2=$(get_f "new_password_confirm")

json_err() { echo "{\"error\":\"$1\"}"; exit 0; }

[ -n "$CUR" ] || json_err "no_current"
[ -n "$NEWU" ] || json_err "no_user"
[ -n "$NEWP" ] || json_err "no_password"
[ "$NEWP" = "$NEWP2" ] || json_err "mismatch"
[[ "$NEWU" =~ ^[a-zA-Z0-9_.-]{1,32}$ ]] || json_err "bad_username"
[[ ${#NEWP} -ge 4 && ${#NEWP} -le 128 ]] || json_err "bad_password_len"
[[ "$NEWP" != *$'\n'* ]] || json_err "bad_password"
case "$NEWP" in
  *"'"*|*'$'*|*'`'*|*';'*|*'|'*|*'&'*|*'<'*|*'>'*|*'('*|*')'*) json_err "bad_password_char" ;;
esac

AUTH=/etc/sa02m_web.env
if [ ! -f "$AUTH" ]; then
  json_err "no_auth_file"
fi
# Fail closed — never default to admin/cyntron. A missing/empty credential file
# means "no valid password to check against", so reject rather than accept a
# built-in default.
SA02M_WEB_USER=""
SA02M_WEB_PASS=""
web_auth_read "$AUTH" || json_err "no_auth_file"
[ -n "$SA02M_WEB_PASS" ] || json_err "no_auth_file"

[ "$CUR" = "$SA02M_WEB_PASS" ] || json_err "wrong_password"

web_auth_write "$NEWU" "$NEWP" > /tmp/sa02m_web.env.new
chmod 600 /tmp/sa02m_web.env.new

if ! sudo /usr/local/sbin/sa02m-commit-web-env 2>/dev/null; then
  rm -f /tmp/sa02m_web.env.new
  json_err "save_failed"
fi

# Credentials changed → revoke every existing session so an old cookie cannot
# outlive the password it was issued under.
web_session_destroy_all

echo '{"ok":true}'
