#!/bin/bash
# SA-02m — снять образ eMMC с донора → .img (+ .img.xz) в out/
#
# Требует: WSL2/Linux, pishrink.sh, ssh (ключ или sshpass + пароль из tools/sa02m-device.env)
#
# Примеры:
#   ./capture-image.sh
#   ./capture-image.sh --ip 192.168.1.136 --name SA-02m-20260730
#   DEVICE_IP=192.168.1.50 ./capture-image.sh --no-cleanup
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${SA02M_DEVICE_ENV:-$REPO_ROOT/tools/sa02m-device.env}"

if [ -r "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a
    # shellcheck source=/dev/null
    . "$ENV_FILE"
    set +a
fi

DEVICE_IP="${DEVICE_IP:-${SA02M_HOST:-192.168.1.136}}"
OUT_DIR="${OUT_DIR:-$SCRIPT_DIR/out}"
STAMP="$(date +%Y%m%d)"
OUTPUT_NAME="${OUTPUT_NAME:-SA-02m-${STAMP}}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/sa02m_sa02}"
EXTRA_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --ip)           DEVICE_IP="$2"; shift 2 ;;
        --name)         OUTPUT_NAME="$2"; shift 2 ;;
        --out-dir)      OUT_DIR="$2"; shift 2 ;;
        --key)          SSH_KEY="$2"; shift 2 ;;
        --no-cleanup|--no-zerofill|--no-id-reset|--no-manifest)
            EXTRA_ARGS+=("$1"); shift ;;
        -h|--help)
            sed -n '2,12p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) EXTRA_ARGS+=("$1"); shift ;;
    esac
done

mkdir -p "$OUT_DIR"

# Prefer repo private key if present (gitignored), else ~/.ssh, else password via make-image.
if [ ! -r "$SSH_KEY" ] && [ -r "$REPO_ROOT/private/.ssh/sa02m_sa02" ]; then
    mkdir -p "$HOME/.ssh"
    cp -f "$REPO_ROOT/private/.ssh/sa02m_sa02" "$SSH_KEY"
    chmod 600 "$SSH_KEY"
fi

echo "Donor : root@${DEVICE_IP} (pass from tools/sa02m-device.env / SA02M_PASS)"
echo "Output: ${OUT_DIR}/${OUTPUT_NAME}.img  (+ .img.xz)"
echo

exec bash "$SCRIPT_DIR/make-image.sh" \
    --ip "$DEVICE_IP" \
    --key "$SSH_KEY" \
    --out-dir "$OUT_DIR" \
    --name "$OUTPUT_NAME" \
    "${EXTRA_ARGS[@]}"
