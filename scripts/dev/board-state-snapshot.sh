#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# board-state-snapshot.sh — normalised board-state snapshot for the refresh-mode
# acceptance («повторный прогон ничего не меняет»). Runs ON THE BOARD (root);
# ships in the full tarball. Procedure (docs/deployment.md «Режим обновления»):
#
#   bash scripts/dev/board-state-snapshot.sh > /root/before.txt
#   bash /tmp/sa02m-upd/scripts/offline-full-update.sh
#   bash scripts/dev/board-state-snapshot.sh > /root/after.txt
#   diff /root/before.txt /root/after.txt
#     # must be empty except the new /etc/sa02m_stacks.conf lines (first run)
#
# Oneshot units are excluded from the active-state list (they flap by design).
# ═══════════════════════════════════════════════════════════════════════════
set -u

section() { printf '### %s\n' "$1"; }

section "unit files (name enable-state)"
systemctl list-unit-files --type=service,timer,socket --no-legend --plain 2>/dev/null \
    | awk '{print $1, $2}' | sort

section "active state (non-oneshot units)"
systemctl list-units --all --type=service,timer,socket --no-legend --plain 2>/dev/null \
    | awk '{print $1}' \
    | while read -r u; do
        t=$(systemctl show -p Type --value "$u" 2>/dev/null)
        [ "$t" = oneshot ] && continue
        printf '%s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null)"
    done | sort

section "dpkg packages"
dpkg-query -W -f='${Package} ${Version} ${db:Status-Abbrev}\n' 2>/dev/null | sort

section "apt holds"
apt-mark showhold 2>/dev/null | sort

section "apt sources.list.d"
ls /etc/apt/sources.list.d/ 2>/dev/null | sort

section "users"
getent passwd | cut -d: -f1 | sort

section "systemd drop-in dirs"
# shellcheck disable=SC2012  # names only, no metadata needed
ls -d /etc/systemd/system/*.d/ 2>/dev/null | sort

section "python modules (bridge deps)"
python3 -c 'import paho.mqtt, yaml, serial' 2>&1

section "node / node-red"
node --version 2>/dev/null
sed -n 's/.*"version": *"\([0-9.]*\)".*/node-red \1/p' /usr/lib/node_modules/node-red/package.json 2>/dev/null

section "stacks policy"
cat /etc/sa02m_stacks.conf 2>/dev/null
