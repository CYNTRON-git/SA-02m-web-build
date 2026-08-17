#!/bin/bash
# SA-02m stand — DHCP + TFTP (dnsmasq) + HTTP image server
#
# Usage:
#   ./start-provisioner.sh [--env ../stand/stand.env]
#
# Expects stand-data layout:
#   stand-data/tftpboot/   zImage DTB uInitrd-netinstall boot.scr
#   stand-data/images/     current.img.xz current.img.xz.sha256
set -euo pipefail
LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGING_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STAND_DIR="$IMAGING_DIR/stand"
ENV_FILE="${STAND_ENV:-$STAND_DIR/stand.env}"
PID_DIR="${STAND_PID_DIR:-$IMAGING_DIR/stand-data/run}"
LOG_DIR="${STAND_LOG_DIR:-$IMAGING_DIR/stand-data/logs}"

usage() {
    cat <<'EOF'
Usage: start-provisioner.sh [--env PATH]

Starts:
  dnsmasq   DHCP + TFTP (config from dnsmasq.conf.tpl + stand.env)
  python3   HTTP on HTTP_PORT serving stand-data/images (+ /tftp alias)

PIDs under stand-data/run/. Stop with stop-stand.sh.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --env) ENV_FILE="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown: $1" >&2; usage; exit 2 ;;
    esac
done

if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; . "$ENV_FILE"; set +a
elif [ -f "$STAND_DIR/stand.env.example" ]; then
    echo "WARN: $ENV_FILE missing — using defaults from example values" >&2
fi

STAND_IP="${STAND_IP:-192.168.1.10}"
STAND_IFACE="${STAND_IFACE:-}"
DHCP_RANGE_START="${DHCP_RANGE_START:-192.168.1.200}"
DHCP_RANGE_END="${DHCP_RANGE_END:-192.168.1.220}"
DHCP_NETMASK="${DHCP_NETMASK:-255.255.255.0}"
DHCP_LEASE="${DHCP_LEASE:-2h}"
HTTP_PORT="${HTTP_PORT:-8080}"
TFTP_ROOT="${TFTP_ROOT:-$IMAGING_DIR/stand-data/tftpboot}"
IMAGES_DIR="${IMAGES_DIR:-$IMAGING_DIR/stand-data/images}"
ENABLE_DHCP="${ENABLE_DHCP:-1}"

mkdir -p "$PID_DIR" "$LOG_DIR" "$TFTP_ROOT" "$IMAGES_DIR"

if [ ! -f "$IMAGES_DIR/current.img.xz" ]; then
    echo "WARN: no $IMAGES_DIR/current.img.xz — publish with publish-image.sh" >&2
fi
for f in zImage sun8i-a40i-sk.dtb uInitrd-netinstall; do
    [ -f "$TFTP_ROOT/$f" ] || echo "WARN: missing TFTP file $TFTP_ROOT/$f (build-netinstall.sh)" >&2
done

# ── dnsmasq ──────────────────────────────────────────────────────────────
DNSMASQ_CONF="$PID_DIR/dnsmasq.conf"
TPL="$STAND_DIR/dnsmasq.conf.tpl"
[ -f "$TPL" ] || { echo "ERROR: missing $TPL" >&2; exit 1; }

IFACE_LINE=""
if [ -n "$STAND_IFACE" ]; then
    IFACE_LINE="interface=${STAND_IFACE}"
fi

DHCP_LINES="# DHCP disabled (ENABLE_DHCP=0)"
if [ "$ENABLE_DHCP" = "1" ]; then
    DHCP_LINES=$(printf '%s\n' \
        "dhcp-range=${DHCP_RANGE_START},${DHCP_RANGE_END},${DHCP_NETMASK},${DHCP_LEASE}" \
        "dhcp-option=option:router,${STAND_IP}" \
        "dhcp-option=option:tftp-server,${STAND_IP}" \
        "dhcp-boot=boot.scr")
fi

# Python template expand (safe for multiline DHCP block)
STAND_IP="$STAND_IP" TFTP_ROOT="$TFTP_ROOT" PID_DIR="$PID_DIR" LOG_DIR="$LOG_DIR" \
IFACE_LINE="$IFACE_LINE" DHCP_LINES="$DHCP_LINES" TPL="$TPL" DNSMASQ_CONF="$DNSMASQ_CONF" \
python3 - <<'PY'
import os
from pathlib import Path
tpl = Path(os.environ["TPL"]).read_text(encoding="utf-8")
for key in ("STAND_IP", "TFTP_ROOT", "PID_DIR", "LOG_DIR", "IFACE_LINE", "DHCP_LINES"):
    tpl = tpl.replace("__" + key + "__", os.environ.get(key, ""))
Path(os.environ["DNSMASQ_CONF"]).write_text(tpl, encoding="utf-8")
PY

if [ -f "$PID_DIR/dnsmasq.pid" ] && kill -0 "$(cat "$PID_DIR/dnsmasq.pid")" 2>/dev/null; then
    echo "dnsmasq already running pid=$(cat "$PID_DIR/dnsmasq.pid")"
else
    if ! command -v dnsmasq >/dev/null 2>&1; then
        echo "ERROR: dnsmasq not installed (sudo apt install dnsmasq)" >&2
        exit 1
    fi
    # May need root for DHCP bind — try without first
    if [ "$(id -u)" -eq 0 ]; then
        dnsmasq --conf-file="$DNSMASQ_CONF"
    else
        if ! dnsmasq --conf-file="$DNSMASQ_CONF" 2>"$LOG_DIR/dnsmasq.err"; then
            echo "dnsmasq needs privileges — retrying with sudo" >&2
            sudo dnsmasq --conf-file="$DNSMASQ_CONF"
        fi
    fi
    echo "dnsmasq started (conf $DNSMASQ_CONF)"
fi

# ── HTTP (images) ────────────────────────────────────────────────────────
if [ -f "$PID_DIR/http.pid" ] && kill -0 "$(cat "$PID_DIR/http.pid")" 2>/dev/null; then
    echo "HTTP already running pid=$(cat "$PID_DIR/http.pid")"
else
    # Tiny dual-root server: / → images, /tftp/ → tftpboot
    export _SA02M_HTTP_IMAGES="$IMAGES_DIR"
    export _SA02M_HTTP_TFTP="$TFTP_ROOT"
    export _SA02M_HTTP_PORT="$HTTP_PORT"
    nohup python3 - <<'PY' >"$LOG_DIR/http.log" 2>&1 &
import http.server, os, functools
from pathlib import Path

images = Path(os.environ["_SA02M_HTTP_IMAGES"]).resolve()
tftp = Path(os.environ["_SA02M_HTTP_TFTP"]).resolve()
port = int(os.environ["_SA02M_HTTP_PORT"])

class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        # strip query
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/tftp/"):
            rel = path[len("/tftp/"):]
            return str(tftp / rel)
        rel = path.lstrip("/")
        if not rel:
            rel = "current.img.xz"
        return str(images / rel)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
print(f"HTTP images={images} tftp={tftp} :{port}", flush=True)
httpd.serve_forever()
PY
    echo $! > "$PID_DIR/http.pid"
    echo "HTTP :$HTTP_PORT pid=$(cat "$PID_DIR/http.pid") images=$IMAGES_DIR"
fi

echo "PROVISIONER READY  stand_ip=$STAND_IP  http=:$HTTP_PORT  tftp=$TFTP_ROOT"
