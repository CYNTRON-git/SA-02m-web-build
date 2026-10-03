#!/bin/bash
# SA-02m agent API. The unit is installed and left OFF until the panel card
# enables it (docs/contracts/agent-api.md). Skip with SA02M_SKIP_AGENT_API=1.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [13-agent-api] API для ИИ-агентов ==="

BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPT_SRC="$BASE_DIR/opt/sa02m-agent-api"
UNIT_SRC="$BASE_DIR/etc/systemd/system"

if ! id sa02m-user >/dev/null 2>&1; then
    useradd --system --home-dir /opt/sa02m-user --shell /usr/sbin/nologin sa02m-user
fi
if id www-data >/dev/null 2>&1; then
    usermod -aG sa02m-user www-data || true
fi

install -d -m 2775 -o sa02m-user -g www-data /opt/sa02m-user
install -d -m 0750 -o root -g www-data /etc/sa02m-agent-api
install -d -m 0700 -o root -g root /etc/sa02m-agent-api/root-cap
install -d -m 0750 -o www-data -g adm /var/log/sa02m-agent-api
if [ ! -f /etc/sa02m-agent-api/tokens.json ]; then
    printf '%s\n' '{"tokens":[]}' > /etc/sa02m-agent-api/tokens.json
    chown root:www-data /etc/sa02m-agent-api/tokens.json
    chmod 0640 /etc/sa02m-agent-api/tokens.json
fi

if [ -d "$OPT_SRC" ]; then
    install -d -m 0755 -o root -g root /opt/sa02m-agent-api
    rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' --exclude 'tests' \
        "$OPT_SRC/" /opt/sa02m-agent-api/
    # Root helpers import this package (storecli, rules apply): it must not be
    # writable by anyone but root, whatever modes the source tree carried.
    chown -R root:root /opt/sa02m-agent-api
    find /opt/sa02m-agent-api -type d -exec chmod 0755 {} +
    find /opt/sa02m-agent-api -type f -exec chmod 0644 {} +
fi

install_helper() {
    sa02m_atomic_install -m 0755 -o root -g root \
        "$BASE_DIR/usr/local/sbin/$1" "/usr/local/sbin/$1"
    sed -i 's/\r$//' "/usr/local/sbin/$1" || true
}

install_helper sa02m-agent-api-ctl.sh
install_helper sa02m-agent-token-store.sh
install_helper sa02m-agent-root-cap.sh
install_helper sa02m-agent-root-exec.sh
install_helper sa02m-rules-store-apply.sh
install_helper sa02m-user-unit.sh
install_helper sa02m-agent-journal.sh

sa02m_svc_capture sa02m-agent-api.service
sa02m_atomic_install -m 0644 "$UNIT_SRC/sa02m-agent-api.service" /etc/systemd/system/sa02m-agent-api.service
sa02m_atomic_install -m 0644 "$UNIT_SRC/sa02m-user@.service" /etc/systemd/system/sa02m-user@.service
sa02m_install_sudoers "$BASE_DIR/etc/sudoers.d/sa02m-agent-api" /etc/sudoers.d/sa02m-agent-api
install -m 0644 -o root -g root "$BASE_DIR/etc/tmpfiles.d/sa02m-agent-api.conf" /etc/tmpfiles.d/sa02m-agent-api.conf
install -m 0644 -o root -g root "$BASE_DIR/etc/logrotate.d/sa02m-agent-api" /etc/logrotate.d/sa02m-agent-api
if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /etc/tmpfiles.d/sa02m-agent-api.conf || true
fi
systemctl daemon-reload || true
sa02m_svc_apply sa02m-agent-api.service app off
log INFO "[13-agent-api] служба установлена и выключена, пока её не включат на карточке"
