#!/bin/bash
# GET  — status JSON
# POST — {"profile":"performance"|"high"|"medium"|"low"|"adaptive"}

check_auth() {
    [[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]] && return 0
    return 1
}

CTL=/usr/local/sbin/sa02m-cpu-profile.sh
SET=/usr/local/sbin/sa02m-set-cpu-profile

if [[ ! -x "$CTL" ]]; then
    echo "Content-type: application/json; charset=UTF-8"
    echo "Cache-Control: no-store"
    echo ""
    echo '{"ok":false,"error":"ctl_missing"}'
    exit 0
fi

if ! check_auth; then
    echo "Content-type: application/json; charset=UTF-8"
    echo "Cache-Control: no-store"
    echo ""
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

METHOD="${REQUEST_METHOD:-GET}"

if [[ "$METHOD" = "GET" ]]; then
    echo "Content-type: application/json; charset=UTF-8"
    echo "Cache-Control: no-store"
    echo ""
    sudo -n "$CTL" status --json
    exit 0
fi

if [[ "$METHOD" != "POST" ]]; then
    echo "Content-type: application/json; charset=UTF-8"
    echo ""
    echo '{"ok":false,"error":"method_not_allowed"}'
    exit 0
fi

TMP=$(mktemp /tmp/sa02m-cpuprof.XXXXXX)
trap 'rm -f "$TMP"' EXIT
CL=$(printf '%s' "${CONTENT_LENGTH:-}" | tr -cd '0-9')
if [[ -n "$CL" ]] && [[ "$CL" -gt 0 ]] 2>/dev/null; then
    dd bs=1 count="$CL" 2>/dev/null >"$TMP" || true
fi

PROFILE=""
if [[ -s "$TMP" ]] && command -v python3 >/dev/null 2>&1; then
    PROFILE=$(python3 -c "import json
try:
    with open('$TMP') as f:
        d = json.load(f)
except Exception:
    d = {}
print(d.get('profile', ''))" 2>/dev/null | tr -d '\r\n')
fi

PROFILE=$(printf '%s' "$PROFILE" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
case "$PROFILE" in
    performance|high|medium|low|adaptive) ;;
    *)
        echo "Content-type: application/json; charset=UTF-8"
        echo ""
        echo '{"ok":false,"error":"bad_profile"}'
        exit 0
        ;;
esac

echo "$(date '+%Y-%m-%d %H:%M:%S') cpu_profile.cgi: set ${PROFILE} (web)" >> /var/log/sa02m_install.log 2>&1

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""
if [[ -x "$SET" ]]; then
    OUT=$(sudo -n "$SET" "$PROFILE" 2>&1) || true
else
    OUT=$(sudo -n "$CTL" set "$PROFILE" 2>&1) || true
fi
if [[ -z "$OUT" ]]; then
    echo '{"ok":false,"error":"sudo_failed"}'
    exit 0
fi
printf '%s\n' "$OUT" | head -n1
