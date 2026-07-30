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

[ "$fails" = 0 ] || printf 'kernel-policy-contract: %s check(s) failed — see docs/contracts/kernel-conditional-services.md\n' "$fails"
exit "$fails"
