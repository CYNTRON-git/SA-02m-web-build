#!/bin/bash
# Ждёт ping + ssh после reboot (до 5 мин).
# Учётки: tools/sa02m-device.env (пароль) или SSH-ключ.
set -e
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

KEY="${SSH_KEY:-$HOME/.ssh/sa02m_sa02}"
IP="${IP:-${SA02M_HOST:-192.168.1.136}}"
USER="${SA02M_USER:-root}"
PASS="${SA02M_PASS:-cyntron}"
MAX="${MAX:-40}"

mkdir -p ~/.ssh && chmod 700 ~/.ssh
if [ ! -r "$KEY" ] && [ -r "$REPO_ROOT/private/.ssh/sa02m_sa02" ]; then
    cp "$REPO_ROOT/private/.ssh/sa02m_sa02" "$KEY"
    chmod 600 "$KEY"
fi

ssh_try() {
    if [ -r "$KEY" ]; then
        ssh -i "$KEY" -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
            "${USER}@${IP}" 'echo SSH_OK; uname -nrm; df -h /' 2>/dev/null
        return $?
    fi
    if command -v sshpass >/dev/null 2>&1; then
        SSHPASS="$PASS" sshpass -e ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
            -o PreferredAuthentications=password -o PubkeyAuthentication=no \
            "${USER}@${IP}" 'echo SSH_OK; uname -nrm; df -h /' 2>/dev/null
        return $?
    fi
    return 1
}

for i in $(seq 1 "$MAX"); do
    if ping -c 1 -W 2 "$IP" >/dev/null 2>&1; then
        echo "[$(date +%H:%M:%S)] ping OK (attempt $i)"
        if ssh_try; then
            exit 0
        fi
        echo "[$(date +%H:%M:%S)] ping ok, ssh not ready yet"
    else
        echo "[$(date +%H:%M:%S)] waiting ping ($i/$MAX)"
    fi
    sleep 8
done
echo "TIMEOUT waiting for donor" >&2
exit 1
