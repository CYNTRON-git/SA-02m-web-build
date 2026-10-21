#!/bin/bash
# Authenticated callers only; return the controller JSON and accepted status.
# stderr is left to fcgiwrap (-> nginx error log): a sudo refusal or a controller
# traceback is the only trace a failed call leaves on the board.
web_update_control() {
    local out
    out=$(sudo -n /usr/local/libexec/sa02m-update-control "$@") || true
    if [ -z "$out" ]; then
        out='{"ok":false,"status":"error","error_code":"E_CMD","error_message":"update controller unavailable; run install.sh --refresh"}'
    fi
    if printf '%s' "$out" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("http_status")==202 else 1)' 2>/dev/null; then
        printf 'Status: 202 Accepted\r\n'
    fi
    printf 'Content-type: application/json; charset=UTF-8\r\nCache-Control: no-store\r\n\r\n%s\n' "$out"
}
