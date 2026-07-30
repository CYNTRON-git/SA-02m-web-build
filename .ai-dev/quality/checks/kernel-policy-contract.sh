#!/bin/bash
# Static gate for docs/contracts/kernel-conditional-services.md.
#
# Pins (non-vacuous: a missing file or an empty extraction FAILS):
#   1. opcua gateway port default agrees between the conf template and BOTH
#      py defaults, and is not 4840 (CODESYS owns 4840).
#   2. sa02m-grat-arp@.service is Type=simple (a oneshot regression re-blocks
#      multi-user.target for ~29 s).
#   3. guard script + unit + docker ExecCondition drop-in exist and
#      01-system.sh installs + enables them.
#   4. 08-codesys.sh carries no unconditional enable/start of codesyscontrol
#      (install-only policy).
#   5. service-ctl start/install paths carry no codesys_rc_enable (start =
#      runtime only; allowed only in an explicit autostart-toggle path).
#   6. sa02m-modem-dhcp@.service has no [Install]/WantedBy (a statically
#      enabled device-bound instance holds multi-user for the 90 s device
#      JobTimeout when the modem is absent); 99-modem.rules carries the
#      start line; 01-system.sh carries the stale-symlink cleanup.
#   7. sa02m-mqtt-opcua.py sends exactly one sd_notify("READY=1") and it
#      appears BEFORE self._server.start() (early READY decouples the ~17 s
#      OPC UA address-space build from multi-user.target; moving it back
#      reintroduces the ~20 s boot hold — contract §5.1).
#
# Run: bash .ai-dev/quality/checks/kernel-policy-contract.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1

fails=0
fail() { printf 'kernel-policy-contract: FAIL  %s\n' "$*"; fails=$((fails + 1)); }
pass() { printf 'kernel-policy-contract: ok    %s\n' "$*"; }

# ── 1. opcua port lock-step (conf + both py defaults), never 4840 ──────────
CONF=etc/sa02m-mqtt-opcua.conf
PY=opt/sa02m-mqtt-opcua/sa02m-mqtt-opcua.py
# The conf has two "port" keys (opcua, mqtt) — take the one inside "opcua".
conf_port=$(awk '/"opcua"/{f=1} f && /"port"/{gsub(/[^0-9]/,""); print; exit}' "$CONF" 2>/dev/null)
py_default1=$(grep -oE '"opcua": \{"host": "[^"]*", "port": [0-9]+' "$PY" 2>/dev/null | grep -oE '[0-9]+$')
py_default2=$(grep -oE 'ocfg\.get\("port", [0-9]+\)' "$PY" 2>/dev/null | grep -oE '[0-9]+')

if [ -z "${conf_port:-}" ] || [ -z "${py_default1:-}" ] || [ -z "${py_default2:-}" ]; then
    fail "opcua port extraction came up empty (conf='${conf_port:-}' py1='${py_default1:-}' py2='${py_default2:-}') — file moved or pattern drifted; update this gate"
elif [ "$conf_port" = "4840" ] || [ "$py_default1" = "4840" ] || [ "$py_default2" = "4840" ]; then
    fail "opcua gateway port default is 4840 — CODESYS owns 4840; the gateway lives on 4841 (contract §1)"
elif [ "$conf_port" != "$py_default1" ] || [ "$conf_port" != "$py_default2" ]; then
    fail "opcua port defaults disagree: conf=$conf_port py_load_config=$py_default1 py_ocfg_get=$py_default2 — keep all three in lock-step (contract §1)"
else
    pass "opcua port default $conf_port in lock-step (conf + both py defaults), not 4840"
fi

# ── 2. grat-arp unit stays Type=simple ─────────────────────────────────────
ARP_UNIT=etc/systemd/system/sa02m-grat-arp@.service
if [ ! -f "$ARP_UNIT" ]; then
    fail "$ARP_UNIT missing"
elif grep -qE '^Type=oneshot' "$ARP_UNIT"; then
    fail "$ARP_UNIT regressed to Type=oneshot — multi-user.target waits ~29 s for the ARP burst again (contract §4)"
elif grep -qE '^Type=simple' "$ARP_UNIT"; then
    pass "sa02m-grat-arp@.service is Type=simple"
else
    fail "$ARP_UNIT has no explicit Type=simple line"
fi

# ── 3. guard script + unit + docker drop-in exist; 01-system.sh wires them ─
GUARD_SH=etc/sa02m-kernel-service-guard.sh
GUARD_UNIT=etc/systemd/system/sa02m-kernel-service-guard.service
DOCKER_DROPIN=etc/systemd/system/docker.service.d/sa02m-kernel-guard.conf
for f in "$GUARD_SH" "$GUARD_UNIT" "$DOCKER_DROPIN"; do
    if [ -f "$f" ]; then
        pass "$f present"
    else
        fail "$f missing (contract §2)"
    fi
done
if [ -f "$DOCKER_DROPIN" ] && grep -q '^ExecCondition=/usr/local/sbin/sa02m-kernel-service-guard.sh docker-capable' "$DOCKER_DROPIN"; then
    pass "docker drop-in carries the docker-capable ExecCondition"
else
    fail "docker drop-in lost the docker-capable ExecCondition line (contract §2)"
fi
if grep -q 'install -m 755 "\$ETC_REPO/sa02m-kernel-service-guard.sh"' scripts/01-system.sh \
   && grep -q 'install -m 644 "\$ETC_REPO/systemd/system/sa02m-kernel-service-guard.service"' scripts/01-system.sh \
   && grep -q 'install -m 644 "\$ETC_REPO/systemd/system/docker.service.d/sa02m-kernel-guard.conf"' scripts/01-system.sh; then
    pass "01-system.sh installs guard script + unit + docker drop-in"
else
    fail "01-system.sh no longer installs the guard script/unit/docker drop-in (contract §2)"
fi
if grep -q 'enable sa02m-kernel-service-guard.service' scripts/01-system.sh \
   && grep -q 'sa02m-kernel-service-guard.sh apply-policy' scripts/01-system.sh; then
    pass "01-system.sh enables the guard unit and runs apply-policy"
else
    fail "01-system.sh no longer enables the guard unit / runs apply-policy (contract §2)"
fi

# ── 4. 08-codesys.sh is install-only ───────────────────────────────────────
# Comment lines excluded; the rootfs branch's `disable` must not trip this.
codesys08=$(grep -vE '^[[:space:]]*#' scripts/08-codesys.sh)
if [ -z "$codesys08" ]; then
    fail "scripts/08-codesys.sh unreadable/empty — gate cannot check it"
else
    if printf '%s\n' "$codesys08" | grep -qE 'systemctl[^#]*[^-]enable[^#]*codesyscontrol'; then
        fail "08-codesys.sh re-grew 'systemctl enable codesyscontrol' — install-only policy (contract §3)"
    else
        pass "08-codesys.sh has no systemctl enable codesyscontrol"
    fi
    if printf '%s\n' "$codesys08" | grep -q 'init\.d/codesyscontrol start'; then
        fail "08-codesys.sh re-grew '/etc/init.d/codesyscontrol start' — install-only policy (contract §3)"
    else
        pass "08-codesys.sh has no init.d codesyscontrol start"
    fi
fi

# ── 5. service-ctl start/install paths carry no codesys_rc_enable ──────────
SVCCTL=etc/sa02m-web-service-ctl.sh
# Comment lines excluded (as in pin 4): the policy comments name the banned
# call in prose; only a real call site may trip the pin.
cmd_start_body=$(sed -n '/^cmd_start() {/,/^}/p' "$SVCCTL" 2>/dev/null | grep -vE '^[[:space:]]*#')
codesys_install_body=$(sed -n '/^codesys_install() {/,/^}/p' "$SVCCTL" 2>/dev/null | grep -vE '^[[:space:]]*#')
if [ -z "$cmd_start_body" ] || [ -z "$codesys_install_body" ]; then
    fail "cmd_start()/codesys_install() not found in $SVCCTL — function renamed? update this gate"
else
    if printf '%s\n' "$cmd_start_body" | grep -q 'codesys_rc_enable'; then
        fail "cmd_start() calls codesys_rc_enable — start must be runtime-only (contract §3)"
    else
        pass "cmd_start() carries no codesys_rc_enable"
    fi
    if printf '%s\n' "$codesys_install_body" | grep -q 'codesys_rc_enable'; then
        fail "codesys_install() calls codesys_rc_enable — install must not re-arm autostart (contract §3)"
    else
        pass "codesys_install() carries no codesys_rc_enable"
    fi
fi

# ── 6. Boot-time holds: modem-dhcp is event-driven only (contract §5) ──────
# A statically enabled instance pins its BindsTo modem device job into the
# boot transaction: absent modem ⇒ multi-user waits the 90 s device
# JobTimeout (bench 2026-07-30, live list-jobs captures). Same failure family
# as the grat-arp oneshot pin above.
MODEM_UNIT=etc/systemd/sa02m-modem-dhcp@.service
if [ ! -f "$MODEM_UNIT" ]; then
    fail "$MODEM_UNIT missing"
elif grep -qE '^\[Install\]|^WantedBy=' "$MODEM_UNIT"; then
    fail "sa02m-modem-dhcp@.service grew an [Install]/WantedBy — a statically enabled instance holds multi-user ~90 s when the modem is absent (contract §5)"
else
    pass "sa02m-modem-dhcp@.service has no [Install]/WantedBy (event-driven only)"
fi
if grep -q 'systemctl start sa02m-modem-dhcp@%k.service' etc/udev/99-modem.rules; then
    pass "99-modem.rules still starts sa02m-modem-dhcp@%k on interface add"
else
    fail "etc/udev/99-modem.rules lost the sa02m-modem-dhcp@%k start line — with no [Install] the unit would never run at all (contract §5)"
fi
if grep -q 'rm -f /etc/systemd/system/multi-user.target.wants/sa02m-modem-dhcp@\*.service' scripts/01-system.sh; then
    pass "01-system.sh removes stale modem-dhcp enable symlinks (migration)"
else
    fail "scripts/01-system.sh lost the stale sa02m-modem-dhcp@ wants-symlink cleanup — a previously-enabled device keeps the 90 s boot hold (contract §5)"
fi

# ── 7. mqtt-opcua sends READY=1 before server.start() (contract §5.1) ──────
# Early READY decouples the ~17 s OPC UA address-space build from multi-user;
# moving READY back after server.start() reintroduces the ~20 s boot hold.
if [ ! -f "$PY" ]; then
    fail "$PY missing — cannot check early-READY (contract §5.1)"
else
    ready_count=$(grep -c 'sd_notify("READY=1")' "$PY")
    ready_line=$(grep -n 'sd_notify("READY=1")' "$PY" | head -1 | cut -d: -f1)
    start_line=$(grep -n 'self\._server\.start()' "$PY" | head -1 | cut -d: -f1)
    if [ -z "${ready_line:-}" ] || [ -z "${start_line:-}" ]; then
        fail "opcua daemon READY=1 / server.start() extraction empty (ready='${ready_line:-}' start='${start_line:-}') — file moved or pattern drifted; update this gate"
    elif [ "$ready_count" != "1" ]; then
        fail "opcua daemon has $ready_count sd_notify(\"READY=1\") calls — expect exactly ONE, sent early before server.start() (contract §5.1)"
    elif [ "$ready_line" -ge "$start_line" ]; then
        fail "opcua daemon sends READY=1 (line $ready_line) at/after server.start() (line $start_line) — READY must precede the ~17 s OPC UA address-space build or multi-user.target waits on it again (contract §5.1)"
    else
        pass "opcua daemon sends the single READY=1 (line $ready_line) before server.start() (line $start_line)"
    fi
fi

[ "$fails" = 0 ] || printf 'kernel-policy-contract: %s check(s) failed — see docs/contracts/kernel-conditional-services.md\n' "$fails"
exit "$fails"
