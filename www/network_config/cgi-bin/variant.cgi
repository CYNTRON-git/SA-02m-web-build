#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# SA-02m: hardware variant selector CGI
# GET  → {"variant":"sa02m-1eth","serial_map":"<base64>"}
# POST → variant=sa02m-1eth|sa02m-2eth → {"ok":true,"variant":"...","serial_count":N}

check_auth() {
    web_session_check_cookie && return 0
    return 1
}

if ! check_auth; then
    printf 'Content-Type: application/json\r\n\r\n'
    printf '{"ok":false,"error":"unauthorized"}\n'
    exit 0
fi

HW_CONF=/etc/sa02m_hw_variant.conf

read_variant() {
    local v
    if [ -f "$HW_CONF" ]; then
        v=$(awk -F= '/^SA02M_HW_VARIANT=/{gsub(/^[ \t"]+|[ \t"]+$/,"",$2);print $2;exit}' "$HW_CONF" 2>/dev/null)
    fi
    case "$v" in
        sa02m-1eth|sa02m-2eth) printf '%s' "$v" ;;
        *) printf 'sa02m-1eth' ;;
    esac
}

case "${REQUEST_METHOD:-GET}" in
    POST)
        # CSRF BEFORE any mutation (policy: docs/decisions/selective-csrf-policy.md).
        # This branch emits its own headers only AFTER the mutation below, so
        # headers are NOT yet on the wire here — web_csrf_require prints its own.
        web_csrf_require
        read -r -n "${CONTENT_LENGTH:-0}" POST_DATA
        VARIANT=$(printf '%s' "$POST_DATA" \
            | sed 's/.*variant=\([^&]*\).*/\1/' \
            | tr -cd 'a-z0-9-')

        case "$VARIANT" in
            sa02m-1eth|sa02m-2eth)
                RESULT=$(sudo /usr/local/sbin/sa02m-apply-variant.sh "$VARIANT" 2>&1)
                # Инвалидируем кэш status.cgi, чтобы «Система» немедленно
                # показала обновлённое имя устройства (ЦИНТРОН СА-02м / -2).
                rm -f /tmp/sa02m_status_cache/system.json \
                      /tmp/sa02m_status_cache/system.json.lock \
                      /tmp/sa02m_status_cache/main.json \
                      /tmp/sa02m_status_cache/main.json.lock \
                      2>/dev/null || true
                printf 'Content-Type: application/json\r\n\r\n'
                printf '%s\n' "$RESULT"
                ;;
            *)
                printf 'Content-Type: application/json\r\n\r\n'
                printf '{"ok":false,"error":"invalid variant"}\n'
                ;;
        esac
        ;;

    GET|*)
        CURRENT=$(read_variant)
        MAP_B64=$(base64 -w0 /etc/sa02m_serial_map.conf 2>/dev/null || true)
        printf 'Content-Type: application/json\r\n\r\n'
        printf '{"variant":"%s","serial_map":"%s"}\n' "$CURRENT" "$MAP_B64"
        ;;
esac
