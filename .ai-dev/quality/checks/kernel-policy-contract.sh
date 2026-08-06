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
#      appears at the top of main() BEFORE `gw = OpcuaGateway(cfg)` (early
#      READY decouples the OPC UA address-space build — ~9 s in OpcuaServer()
#      construction inside __init__, ~17 s total — from multi-user.target;
#      moving it into/after construction reintroduces the boot hold — §5.1).
#   8. sa02m-kernel-select.sh's SMP/RT version defaults name the fleet's
#      kernel pair (§6) AND detect_installed_module_ver()'s whitelist matches
#      BOTH of them — a default the detector cannot find is not inert:
#      write_conf() persists it and the panel then refuses a profile the
#      board actually has.
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

# ── 7. mqtt-opcua sends READY=1 before OpcuaGateway() construction (§5.1) ───
# Early READY decouples the OPC UA address-space build (~9 s in OpcuaServer()
# construction inside __init__, ~17 s total) from multi-user.target. READY
# lives at the top of main(), BEFORE `gw = OpcuaGateway(cfg)`; moving it into
# or after construction reintroduces the boot hold.
if [ ! -f "$PY" ]; then
    fail "$PY missing — cannot check early-READY (contract §5.1)"
else
    ready_count=$(grep -c 'sd_notify("READY=1")' "$PY")
    ready_line=$(grep -n 'sd_notify("READY=1")' "$PY" | head -1 | cut -d: -f1)
    ctor_line=$(grep -n 'gw = OpcuaGateway(' "$PY" | head -1 | cut -d: -f1)
    if [ -z "${ready_line:-}" ] || [ -z "${ctor_line:-}" ]; then
        fail "opcua daemon READY=1 / OpcuaGateway() extraction empty (ready='${ready_line:-}' ctor='${ctor_line:-}') — file moved or pattern drifted; update this gate"
    elif [ "$ready_count" != "1" ]; then
        fail "opcua daemon has $ready_count sd_notify(\"READY=1\") calls — expect exactly ONE, sent early in main() before OpcuaGateway() construction (contract §5.1)"
    elif [ "$ready_line" -ge "$ctor_line" ]; then
        fail "opcua daemon sends READY=1 (line $ready_line) at/after OpcuaGateway() construction (line $ctor_line) — the ~9 s OpcuaServer() build runs in __init__, so READY must precede it or multi-user.target waits on it again (contract §5.1)"
    else
        pass "opcua daemon sends the single READY=1 (line $ready_line) before OpcuaGateway() construction (line $ctor_line)"
    fi
fi

# ── 8. kernel-select defaults == the fleet's kernel pair (contract §6) ─────
# The pair is declared ONCE here, mirroring the contract table. Non-vacuous:
# an empty extraction FAILS. The whitelist check uses `case` so the extracted
# pattern is matched as the shell itself would match it — not re-implemented.
KSEL=etc/sa02m-kernel-select.sh
FLEET_SMP=6.1.0-rc6
FLEET_RT=6.1.0-rc6-rt4
if [ ! -f "$KSEL" ]; then
    fail "$KSEL missing — cannot check the SMP/RT defaults (contract §6)"
else
    smp_def=$(sed -n 's/^SMP_VER_DEFAULT=\(.*\)$/\1/p' "$KSEL" | head -1)
    rt_def=$(sed -n 's/^RT_VER_DEFAULT=\(.*\)$/\1/p' "$KSEL" | head -1)
    # The detector's whitelist arm: the `case` label line inside
    # detect_installed_module_ver(), e.g. `*sa02m*|5.10.35*|6.1.0-rc6*)`.
    ksel_arm=$(sed -n '/^detect_installed_module_ver() {/,/^}/p' "$KSEL" \
        | sed -n 's/^[[:space:]]*\(\**[^ )]*|[^ )]*\))$/\1/p' | head -1)
    if [ -z "${smp_def:-}" ] || [ -z "${rt_def:-}" ] || [ -z "${ksel_arm:-}" ]; then
        fail "kernel-select extraction came up empty (smp='${smp_def:-}' rt='${rt_def:-}' arm='${ksel_arm:-}') — file moved or pattern drifted; update this gate"
    elif ! printf '%s' "$ksel_arm" | grep -qE '^[A-Za-z0-9._*?|+-]+$'; then
        # The arm is fed to `eval` below (a case label cannot be matched without
        # the shell's own globber). Refuse anything outside glob+version
        # characters so the eval can never see a substitution or a command
        # separator, whatever lands in the script.
        fail "detect_installed_module_ver case label '$ksel_arm' contains characters this gate refuses to eval — keep it to plain glob patterns (contract §6)"
    else
        if [ "$smp_def" = "$FLEET_SMP" ] && [ "$rt_def" = "$FLEET_RT" ]; then
            pass "kernel-select defaults name the fleet pair ($FLEET_SMP / $FLEET_RT)"
        else
            fail "kernel-select defaults are '$smp_def' / '$rt_def' but the fleet runs '$FLEET_SMP' / '$FLEET_RT' — write_conf() persists an unfindable default and the panel then refuses a profile the board has (contract §6)"
        fi
        # Match each default against the extracted arm exactly as the script does.
        arm_covers() {
            eval "case \"\$1\" in $ksel_arm) return 0 ;; esac"
            return 1
        }
        for v in "$smp_def" "$rt_def"; do
            if arm_covers "$v"; then
                pass "detect_installed_module_ver whitelist matches '$v'"
            else
                fail "detect_installed_module_ver whitelist ($ksel_arm) does NOT match the default '$v' — the self-heal path cannot rescue it (contract §6)"
            fi
        done
    fi
fi

[ "$fails" = 0 ] || printf 'kernel-policy-contract: %s check(s) failed — see docs/contracts/kernel-conditional-services.md\n' "$fails"
exit "$fails"
