#!/bin/bash
# SA-02m — one-shot flash stand launcher (Linux / WSL2)
set -euo pipefail
LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGING_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${STAND_ENV:-$SCRIPT_DIR/stand.env}"
PID_DIR="$IMAGING_DIR/stand-data/run"
LOG_DIR="$IMAGING_DIR/stand-data/logs"

if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$SCRIPT_DIR/stand.env.example" ]; then
        cp "$SCRIPT_DIR/stand.env.example" "$ENV_FILE"
        echo "Created $ENV_FILE — edit STAND_IP before production use"
    fi
fi
# shellcheck disable=SC1090
set -a; [ -f "$ENV_FILE" ] && . "$ENV_FILE"; set +a

STATUS_PORT="${STATUS_PORT:-8765}"
mkdir -p "$PID_DIR" "$LOG_DIR" \
    "$IMAGING_DIR/stand-data/tftpboot" \
    "$IMAGING_DIR/stand-data/images"

echo "═══ SA-02m flash stand ═══"
echo "env: $ENV_FILE"
echo "status UI: http://127.0.0.1:${STATUS_PORT}/"

# Provisioner (dnsmasq + HTTP)
bash "$IMAGING_DIR/net-provisioner/start-provisioner.sh" --env "$ENV_FILE"

start_bg() {
    local name="$1"; shift
    local pidf="$PID_DIR/${name}.pid"
    if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
        echo "$name already running pid=$(cat "$pidf")"
        return 0
    fi
    nohup "$@" >"$LOG_DIR/${name}.log" 2>&1 &
    echo $! >"$pidf"
    echo "$name pid=$(cat "$pidf")"
}

start_bg status-server python3 "$SCRIPT_DIR/status-server.py" \
    --port "$STATUS_PORT" \
    --state-file "$IMAGING_DIR/stand-data/status.json"

start_bg fel-agent python3 "$SCRIPT_DIR/fel-agent.py" --env "$ENV_FILE"
start_bg postflash python3 "$SCRIPT_DIR/postflash-monitor.py" --env "$ENV_FILE"

# Reset UI to idle for the shift
python3 - <<PY
import urllib.request
try:
    urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:${STATUS_PORT}/api/status",
        data=b"state=IDLE&progress=0&message=stand+ready",
        method="POST"), timeout=2)
except Exception:
    pass
PY

cat <<EOF

Stand is up.
  UI:     http://127.0.0.1:${STATUS_PORT}/
  Image:  $IMAGING_DIR/stand-data/images/current.img.xz
  TFTP:   $IMAGING_DIR/stand-data/tftpboot/
  Logs:   $LOG_DIR/

Per board: Ethernet + USB-OTG + power → FEL → wait DONE.
Stop:     $SCRIPT_DIR/stop-stand.sh
EOF
