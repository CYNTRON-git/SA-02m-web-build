#!/bin/bash
# Install only the software adapter; preserve configuration, state and service policy.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib.sh"
check_root
source_root="$SCRIPT_DIR/.."
for source in opt/sa02m-phase-energy/energy_state.py opt/sa02m-phase-energy/phase_energy.py opt/sa02m-phase-energy/mqtt_service.py etc/sa02m-phase-energy.json etc/systemd/system/sa02m-phase-energy.service; do
    [ -s "$source_root/$source" ] || { log ERR "Phase energy payload is incomplete"; exit 1; }
done
python3 -c 'import paho.mqtt.client; from paho.mqtt.subscribeoptions import SubscribeOptions' || exit 1
sa02m_svc_capture sa02m-phase-energy.service
install -d -m 0755 /usr/local/libexec/sa02m-phase-energy /etc/systemd/system
for module in energy_state.py phase_energy.py mqtt_service.py; do
    sa02m_atomic_install -m 0644 -o root -g root "$source_root/opt/sa02m-phase-energy/$module" "/usr/local/libexec/sa02m-phase-energy/$module"
done
if [ ! -e /etc/sa02m-phase-energy.json ]; then
    sa02m_atomic_install -m 0644 -o root -g root "$source_root/etc/sa02m-phase-energy.json" /etc/sa02m-phase-energy.json
fi
unit=/etc/systemd/system/sa02m-phase-energy.service
runtime_unit=/run/systemd/system/sa02m-phase-energy.service
phase_unit_masked=0
for phase_unit_link in "$unit" "$runtime_unit"; do
    if [[ -L "$phase_unit_link" ]]; then
        if ! phase_unit_target=$(readlink "$phase_unit_link"); then
            log ERR "Cannot inspect sa02m-phase-energy.service unit mask"
            exit 1
        fi
        if [[ "$phase_unit_target" == /dev/null ]]; then phase_unit_masked=1; fi
    fi
done
if [[ "$phase_unit_masked" == 1 ]]; then
    log INFO "Phase energy service mask preserved; after unmask, repeat refresh to install the unit"
else
    sa02m_atomic_install -m 0644 -o root -g root "$source_root/etc/systemd/system/sa02m-phase-energy.service" "$unit"
fi
sa02m_systemctl daemon-reload
sa02m_svc_apply sa02m-phase-energy.service app off
