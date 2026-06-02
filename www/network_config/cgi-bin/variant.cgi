#!/bin/bash
# SA-02m: hardware variant selector CGI
# GET  → {"variant":"sa02m-1eth","serial_map":"<base64>"}
# POST → variant=sa02m-1eth|sa02m-2eth → {"ok":true,"variant":"...","serial_count":N}

check_auth() {
    [[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] && return 0
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
        read -r -n "${CONTENT_LENGTH:-0}" POST_DATA
        VARIANT=$(printf '%s' "$POST_DATA" \
            | sed 's/.*variant=\([^&]*\).*/\1/' \
            | tr -cd 'a-z0-9-')

        case "$VARIANT" in
            sa02m-1eth|sa02m-2eth)
                RESULT=$(sudo /usr/local/sbin/sa02m-apply-variant.sh "$VARIANT" 2>&1)
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
