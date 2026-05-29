#!/bin/bash
# SSE-поток: подписывается на /devices/# и транслирует входящие сообщения.
# nginx требует: fastcgi_buffering off; fastcgi_read_timeout 300s;

check_auth() {
    [[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]] && return 0
    return 1
}
if ! check_auth; then
    echo "Content-type: application/json"
    echo ""
    echo '{"error":"unauthorized"}'
    exit 0
fi

if ! command -v mosquitto_sub >/dev/null 2>&1; then
    echo "Content-type: text/event-stream"
    echo "Cache-Control: no-store"
    echo "X-Accel-Buffering: no"
    echo ""
    echo "event: error"
    echo "data: mosquitto_sub not installed"
    echo ""
    exit 0
fi

echo "Content-type: text/event-stream"
echo "Cache-Control: no-store"
echo "X-Accel-Buffering: no"
echo ""

# Фильтр устройства из QUERY_STRING
DEVICE_FILTER=""
case "${QUERY_STRING:-}" in
    *device=*)
        DEVICE_FILTER=$(printf '%s' "${QUERY_STRING}" | grep -oP '(?<=device=)[^&]+' | head -1)
        ;;
esac

if [ -n "$DEVICE_FILTER" ]; then
    TOPIC="/devices/${DEVICE_FILTER}/#"
else
    TOPIC="/devices/#"
fi

# Чистое завершение при разрыве соединения
MOSQ_PID=""
cleanup() {
    [ -n "$MOSQ_PID" ] && kill "$MOSQ_PID" 2>/dev/null
    exit 0
}
trap cleanup TERM INT HUP PIPE

# Один mosquitto_sub для retained + live сообщений, -q 2 для надёжной доставки
mosquitto_sub -h 127.0.0.1 -v -t "$TOPIC" -q 2 2>/dev/null | while IFS= read -r line; do
    topic="${line%% *}"
    payload="${line#* }"
    # Пропустить служебные meta-топики (title, order, type, units, readonly, driver, name)
    case "$topic" in
        */meta/title|*/meta/order|*/meta/type|*/meta/units|\
        */meta/readonly|*/meta/driver|*/meta/name)
            continue ;;
        */meta/error)
            # Пустой payload = сброс ошибки (WB); в мониторе даёт шум «(null)».
            [ -z "$payload" ] && continue
            ;;
    esac
    ts=$(date '+%H:%M:%S')
    payload_esc=$(printf '%s' "$payload" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().rstrip()))" 2>/dev/null || printf '"%s"' "$payload")
    topic_esc=$(printf '%s' "$topic" | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf 'data: {"ts":"%s","topic":"%s","value":%s}\n\n' \
        "$ts" "$topic_esc" "$payload_esc"
done &
MOSQ_PID=$!

# Heartbeat каждые 20 с, пока mosquitto_sub жив
while kill -0 "$MOSQ_PID" 2>/dev/null; do
    sleep 20
    printf ': heartbeat\n\n'
done
