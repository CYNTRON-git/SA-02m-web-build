#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "${SA02M_INSTALL_GUARDED:-0}" != 1 ]; then
    exec python3 "$SCRIPT_DIR/install-maintenance.py" --require-coordinator "$SCRIPT_DIR/03a-port-coordinator.sh" "$@"
fi
source "$SCRIPT_DIR/lib.sh"
check_root
BASE_DIR="$SCRIPT_DIR/.."

# Dependencies precede the broker and every updated UART consumer.
getent group sa02m-flasher >/dev/null || groupadd --system sa02m-flasher
if ! id sa02m-flasher >/dev/null 2>&1; then
    useradd --system --gid sa02m-flasher --home-dir /opt/sa02m-flasher \
        --shell /usr/sbin/nologin sa02m-flasher
fi
install -d -m 0755 -o root -g root /opt/sa02m-port-coordinator /etc/sa02m
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
    "$BASE_DIR/opt/sa02m-port-coordinator/" /opt/sa02m-port-coordinator/
chown -R root:root /opt/sa02m-port-coordinator
if [ ! -e /etc/sa02m/port-coordinator.json ] && [ ! -L /etc/sa02m/port-coordinator.json ]; then
    install -m 0644 -o root -g root "$BASE_DIR/etc/sa02m/port-coordinator.json" \
        /etc/sa02m/port-coordinator.json
fi
sa02m_atomic_install -m 0644 -o root -g root \
    "$BASE_DIR/etc/systemd/sa02m-port-coordinator.service" \
    /etc/systemd/system/sa02m-port-coordinator.service
systemctl daemon-reload
sa02m_svc_apply sa02m-port-coordinator.service infra start restart
if [ -z "${SA02M_ROOTFS_BUILD:-}" ]; then
    python3 -I -c "import sys; sys.path.insert(0,'/opt/sa02m-port-coordinator'); from sa02m_ports import assert_idle_for_maintenance; assert_idle_for_maintenance()"
fi
