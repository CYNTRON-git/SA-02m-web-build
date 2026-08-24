#!/bin/bash
# sudoers-pin-contract — the escalation-CLOSED gate for audit B1.
#
# Proves that an authenticated web session can no longer reach root without the
# device root password: the www-data sudoers grant lists ONLY pinned helpers
# and fixed argument vectors, apply.cgi/lib_hw hold no raw tee/ifup/ifdown/kill/
# i2cset/gpioset, the pinned helpers exist and validate their input, and the
# grant is single-home (installed wholesale so an installer re-run REMOVES a
# stale dangerous grant). RED against the pre-fix tree, GREEN after.
#
# Static + a sandboxed behavioural run of the iface-conf-write helper (allow /
# refuse / symlink). The real on-device `sudo -n tee … → refused` proof is
# device-only (needs the sudoers loaded + real sudo) and is modelled here by the
# granted-Cmnd assertions over the committed file. `visudo -cf` runs on every
# install (sa02m_harden_sudoers) — device/CI-only, no visudo on a dev Windows box.
#
# Run: bash .ai-dev/quality/checks/sudoers-pin-contract.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1

fails=0
fail() { printf 'sudoers-pin-contract: FAIL  %s\n' "$*"; fails=$((fails + 1)); }
pass() { printf 'sudoers-pin-contract: ok    %s\n' "$*"; }

SUD=etc/sudoers.d/sa02m-www
IFACE_HELPER=etc/sa02m-iface-conf-write.sh
USB_HELPER=etc/sa02m-usb-power.sh
APPLY=www/network_config/cgi-bin/apply.cgi
LIBHW=www/network_config/cgi-bin/lib_hw.sh
UDEV_RULE=etc/udev/rules.d/50-sa02m-i2c2-unbind.rules

# ── 1. The committed grant exists and lists no dangerous raw Cmnd ───────────
if [ -f "$SUD" ]; then
    pass "single-home sudoers drop-in present ($SUD)"
    # Strip comments so a rule named in prose does not trip a code gate.
    grant=$(grep -vE '^[[:space:]]*#' "$SUD")

    # Raw tee — the arbitrary-root-write pivot. Any tee Cmnd is a fail.
    if printf '%s\n' "$grant" | grep -qE '(^|[[:space:],])(/usr)?/bin/tee\b'; then
        fail "grant still lists raw tee — arbitrary root file-write (the B1 escalation)"
    else
        pass "no raw tee in the grant"
    fi

    # Bare ifup/ifdown (no argument, or a wildcard) = root exec via a pre-up
    # stanza / `-i <file>`. The pinned forms carry a fixed iface name.
    if printf '%s\n' "$grant" | grep -qE '/sbin/if(up|down)([[:space:]]*,|[[:space:]]*\\?$|[[:space:]]+\*)'; then
        fail "grant has a BARE or wildcard ifup/ifdown — must be argument-pinned to the 4 LAN names"
    else
        pass "no bare/wildcard ifup/ifdown in the grant"
    fi

    # Raw kill — kill any PID as root.
    if printf '%s\n' "$grant" | grep -qE '(^|[[:space:],])(/usr)?/bin/kill\b'; then
        fail "grant still lists raw kill"
    else
        pass "no raw kill in the grant"
    fi

    # i2cset/i2cget/gpioset/gpioget — dropped entirely (I2C via group, GPIO via helper).
    if printf '%s\n' "$grant" | grep -qE 'i2cset|i2cget|gpioset|gpioget'; then
        fail "grant still lists i2cset/i2cget/gpioset/gpioget — dropped in B1"
    else
        pass "no i2cset/i2cget/gpioset/gpioget in the grant"
    fi

    # Non-vacuity + the pinned replacements are actually present.
    for want in \
        '/usr/local/sbin/sa02m-iface-conf-write.sh' \
        '/usr/local/sbin/sa02m-usb-power.sh \*' \
        '/sbin/ifup eth0' '/sbin/ifup end1' \
        '/sbin/ifdown eth0' '/sbin/ifdown end1'; do
        if printf '%s\n' "$grant" | grep -qE "$want"; then
            pass "pinned grant present: ${want//\\/}"
        else
            fail "pinned grant MISSING: ${want//\\/} — the replacement for a dropped raw Cmnd"
        fi
    done

    # Modelled 'sudo -n tee refused / helper allowed' over the committed file:
    # tee is NOT a granted Cmnd (deny), the helper IS (allow). The real on-device
    # sudo check is stated device-only in the header.
    if printf '%s\n' "$grant" | grep -qE '(^|[[:space:],])(/usr)?/bin/tee\b'; then
        fail "modelled escalation OPEN: 'sudo -n tee …' would be permitted"
    elif printf '%s\n' "$grant" | grep -q '/usr/local/sbin/sa02m-iface-conf-write.sh'; then
        pass "modelled escalation CLOSED: tee denied, pinned iface-write allowed"
    else
        fail "modelled escalation: the pinned iface-write helper is not granted"
    fi
else
    fail "single-home sudoers drop-in missing ($SUD) — the whole B1 fix is absent"
fi

# ── 2. apply.cgi holds no raw tee; both writers route through the helper ────
if [ -f "$APPLY" ]; then
    acode=$(grep -vE '^[[:space:]]*#' "$APPLY")
    n_tee=$(printf '%s\n' "$acode" | grep -c 'sudo tee')
    n_help=$(printf '%s\n' "$acode" | grep -c 'sa02m-iface-conf-write\.sh')
    [ "$n_tee" = 0 ] && pass "apply.cgi: 0 raw 'sudo tee'" || fail "apply.cgi still has $n_tee raw 'sudo tee' (expected 0)"
    [ "$n_help" = 2 ] && pass "apply.cgi: both writers route through sa02m-iface-conf-write.sh" \
        || fail "apply.cgi calls the pinned iface-write helper $n_help times (expected 2: write_iface_conf + conf_backup)"
else
    fail "apply.cgi missing ($APPLY)"
fi

# ── 3. The pinned helpers exist and validate their input ────────────────────
if [ -f "$IFACE_HELPER" ]; then
    hb=$(cat "$IFACE_HELPER")
    grep -q 'interfaces.d/eth0.conf' <<<"$hb" && grep -q 'interfaces.d/end1.conf' <<<"$hb" \
        && pass "iface-conf-write helper carries the 4-name case allow-list" \
        || fail "iface-conf-write helper lost its 4-name allow-list"
    grep -qE '\[ -L "\$dst" \]|\[ -L "\$conf" \]' <<<"$hb" \
        && pass "iface-conf-write helper refuses symlinks" \
        || fail "iface-conf-write helper lost its symlink refusal"
    # Content validation (the B1 round-2 fix): a per-line allow-list with the
    # exec/hook directives enumerated (klogic adjust + installer route) and an
    # unknown-keyword fail-closed default.
    if grep -q 'iface_line_ok' <<<"$hb" \
       && grep -q '/home/klogic/adjust-' <<<"$hb" \
       && grep -q 'ip.*route.*replace.*default' <<<"$hb" \
       && grep -qE 'pre-up\|up\|post-up' <<<"$hb"; then
        pass "iface-conf-write helper content-validates (enumerated hook allow-list, fail-closed)"
    else
        fail "iface-conf-write helper does NOT content-validate — a verbatim writer lets www-data plant a root-exec pre-up hook (B1 composition escalation)"
    fi
else
    fail "iface-conf-write helper missing ($IFACE_HELPER)"
fi
if [ -f "$USB_HELPER" ]; then
    ub=$(cat "$USB_HELPER")
    for v in sysfs-export-out sysfs-write gpiod-set gpiod-stop gpiod-get; do
        grep -q "$v)" <<<"$ub" || fail "usb-power helper missing verb: $v"
    done
    grep -q 'holder_matches' <<<"$ub" && grep -q 'gpioset' <<<"$ub" \
        && pass "usb-power helper: closed verb set + gpioset-cmdline kill guard" \
        || fail "usb-power helper lost its gpioset-cmdline kill guard"
else
    fail "usb-power helper missing ($USB_HELPER)"
fi

# ── 4. lib_hw holds no raw privileged primitives (all via the helper) ───────
if [ -f "$LIBHW" ]; then
    lcode=$(grep -vE '^[[:space:]]*#' "$LIBHW")
    # The dead I2C sudo fallback invoked `sudo -n "$bin"` (a variable — no literal
    # i2cset to grep), so assert the sa02m_hw_i2c_run_tool BODY carries no sudo at
    # all. Non-vacuous: RED on the pre-fix tree (its body has `sudo -n "$bin"`).
    i2c_body=$(sed -n '/^sa02m_hw_i2c_run_tool() {/,/^}/p' "$LIBHW")
    if [ -z "$i2c_body" ]; then
        fail "lib_hw: sa02m_hw_i2c_run_tool not found (cannot verify the dead fallback removal)"
    elif printf '%s\n' "$i2c_body" | grep -vE '^[[:space:]]*#' | grep -q 'sudo'; then
        fail "lib_hw: sa02m_hw_i2c_run_tool still contains a sudo call — the dead i2cset/i2cget fallback was not removed (I2C goes DIRECT via group i2c)"
    else
        pass "lib_hw: sa02m_hw_i2c_run_tool has no sudo (dead i2cset/i2cget fallback removed)"
    fi
    # Raw sudo tee / gpioset / gpioget / kill (not the pinned helper) must be gone.
    if printf '%s\n' "$lcode" | grep -qE 'sudo( -n)? +(-|[A-Za-z]*/)?(tee|gpioset|gpioget)\b|sudo( -n)? +(-|[A-Za-z]*/)?kill\b|\| *sudo( -n)? tee'; then
        fail "lib_hw still has a raw sudo tee/gpioset/gpioget/kill — route it through sa02m-usb-power.sh"
    else
        pass "lib_hw routes GPIO/USB-power through the pinned helper only"
    fi
    grep -q 'sa02m-usb-power.sh' <<<"$lcode" \
        && pass "lib_hw calls the pinned usb-power helper" \
        || fail "lib_hw no longer calls the pinned usb-power helper (did the wiring drop?)"
else
    fail "lib_hw missing ($LIBHW)"
fi

# ── 5. The i2c prerequisite lives in the SAME change (drop-grant safety) ────
# Dropping the i2cset grant is safe ONLY while www-data stays in group i2c and
# /dev/i2c-* stay group-rw. Assert both installer paths keep the group-add and
# the udev rule is present — remove one without the other and the beeper breaks.
gadd='usermod -aG i2c www-data'
grep -q "$gadd" scripts/03-webserver.sh && pass "03-webserver keeps usermod -aG i2c www-data" \
    || fail "03-webserver dropped 'usermod -aG i2c www-data' — beeper/alarm-LED break without the group"
grep -q "$gadd" scripts/update-www-only.sh && pass "update-www-only keeps usermod -aG i2c www-data" \
    || fail "update-www-only dropped 'usermod -aG i2c www-data'"
[ -f "$UDEV_RULE" ] && pass "i2c udev rule present in the tree ($UDEV_RULE)" \
    || fail "i2c udev rule missing ($UDEV_RULE)"

# ── 6. Single-home install: no heredoc/append; both paths use the helper ────
if grep -qE "cat >+ */etc/sudoers.d/sa02m-www" scripts/03-webserver.sh; then
    fail "03-webserver still writes the grant via a heredoc — must install the committed file wholesale"
else
    pass "03-webserver has no sudoers heredoc"
fi
if grep -qE ">>+ */etc/sudoers.d/sa02m-www" scripts/update-www-only.sh; then
    fail "update-www-only still APPENDS to the grant — must install the committed file wholesale (a stale grant would never be removed)"
else
    pass "update-www-only has no sudoers append blocks"
fi
grep -qE 'sa02m_install_sudoers .+/etc/sudoers\.d/sa02m-www' scripts/03-webserver.sh \
    && pass "03-webserver installs the committed sudoers file via sa02m_install_sudoers" \
    || fail "03-webserver does not install etc/sudoers.d/sa02m-www via sa02m_install_sudoers"
grep -qE 'sa02m_install_sudoers .+/etc/sudoers\.d/sa02m-www' scripts/update-www-only.sh \
    && pass "update-www-only installs the committed sudoers file via sa02m_install_sudoers" \
    || fail "update-www-only does not install etc/sudoers.d/sa02m-www via sa02m_install_sudoers"
[ -f etc/sudoers.d/sa02m-www.fragment ] \
    && fail "etc/sudoers.d/sa02m-www.fragment still present — folded into the single file, delete it" \
    || pass "the .fragment is gone (folded into the single committed file)"

# ── 7. Behavioural sandbox: the iface-conf-write helper allow/refuse/symlink ─
# Retarget the allow-list dir + log path into a temp sandbox (same sed-retarget
# idiom as the repo's other shell harnesses) and drive the SHIPPED validation.
if [ -f "$IFACE_HELPER" ]; then
    sbox=$(mktemp -d 2>/dev/null) || sbox=""
    if [ -n "$sbox" ]; then
        mkdir -p "$sbox/interfaces.d"
        th="$sbox/helper.sh"
        sed -e "s#/etc/network/interfaces.d#$sbox/interfaces.d#g" \
            -e "s#/var/log/sa02m_install.log#$sbox/log#g" \
            "$IFACE_HELPER" > "$th"

        # (a) allow-listed name + valid content: written from stdin, exit 0.
        if printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136\n    netmask 255.255.255.0\n' \
             | bash "$th" "$sbox/interfaces.d/eth0.conf" >/dev/null 2>&1 \
           && grep -q '192.168.1.136' "$sbox/interfaces.d/eth0.conf" 2>/dev/null; then
            pass "sandbox: allow-listed eth0.conf with valid content written from stdin (exit 0)"
        else
            fail "sandbox: the legitimate iface-conf write through the helper did NOT succeed"
        fi

        # (b) non-allow-listed name: refused (exit 2), nothing written.
        printf 'x\n' | bash "$th" "$sbox/interfaces.d/evil.conf" >/dev/null 2>&1
        rc=$?
        if [ "$rc" = 2 ] && [ ! -e "$sbox/interfaces.d/evil.conf" ]; then
            pass "sandbox: non-allow-listed dest refused (exit 2, no write)"
        else
            fail "sandbox: a non-allow-listed dest was NOT refused (rc=$rc)"
        fi

        # (b2) COMPOSITION escalation (B1 round 2): the destination path is
        # allow-listed (eth0.conf) but the CONTENT plants a root-exec hook that
        # `ifup eth0` would run. The verbatim writer wrote it (escalation MOVED,
        # not closed); the content validator must REFUSE it, no write. This case
        # goes RED against a verbatim writer and GREEN against the validator.
        rm -f "$sbox/interfaces.d/eth0.conf"
        printf 'auto eth0\niface eth0 inet manual\n  pre-up /bin/sh -c "cp /bin/bash /tmp/r; chmod 4755 /tmp/r"\n' \
            | bash "$th" "$sbox/interfaces.d/eth0.conf" >/dev/null 2>&1
        rc=$?
        if [ "$rc" != 0 ] && [ ! -e "$sbox/interfaces.d/eth0.conf" ]; then
            pass "sandbox: content with a root-exec pre-up hook REFUSED (composition escalation closed)"
        else
            fail "sandbox: a pre-up root-exec hook was WRITTEN (rc=$rc) — the composition escalation is OPEN (verbatim writer)"
        fi
        # (b3) a hostile post-up and an unknown keyword are also refused.
        printf 'iface eth0 inet static\n    post-up /bin/rm -rf /\n' | bash "$th" "$sbox/interfaces.d/eth0.conf" >/dev/null 2>&1
        [ "$?" != 0 ] && pass "sandbox: hostile post-up refused" || fail "sandbox: a hostile post-up was accepted"
        printf 'auto eth0\nboguskeyword x\n' | bash "$th" "$sbox/interfaces.d/eth0.conf" >/dev/null 2>&1
        [ "$?" != 0 ] && pass "sandbox: unknown keyword refused (fail-closed)" || fail "sandbox: an unknown keyword was accepted"

        # (b4) the LEGITIMATE forms still write — a blanket hook ban would break
        # the panel (real confs carry the klogic hook and the installer route hook).
        rm -f "$sbox/interfaces.d/eth0.conf"
        printf '# Wired adapter #1\nallow-hotplug eth0\nno-auto-down eth0\niface eth0 inet static\n    address 192.168.0.136\n    netmask 255.255.255.0\n    post-up /home/klogic/adjust-eth0 || /bin/true\n' \
            | bash "$th" "$sbox/interfaces.d/eth0.conf" >/dev/null 2>&1
        [ "$?" = 0 ] && grep -q 'adjust-eth0' "$sbox/interfaces.d/eth0.conf" 2>/dev/null \
            && pass "sandbox: legitimate static + klogic post-up hook WRITTEN" \
            || fail "sandbox: a legitimate klogic-hook conf was rejected (over-strict validator breaks the panel)"
        printf 'allow-hotplug eth1\niface eth1 inet dhcp\n    metric 100\n    post-up ip route replace default via 192.168.1.1 dev eth1 metric 100 || true\n' \
            | bash "$th" "$sbox/interfaces.d/eth1.conf" >/dev/null 2>&1
        [ "$?" = 0 ] && pass "sandbox: legitimate installer default-route hook WRITTEN" \
            || fail "sandbox: the installer's default-route post-up hook was rejected"

        # (c) symlink at an allow-listed name: refused (exit 2), target untouched.
        # Only meaningful where the platform makes a REAL symlink — on a dev
        # Windows/MSYS box `ln -s` copies, so [ -L ] cannot fire; skip with a
        # note there (Linux CI is the enforcing run), same posture as shellcheck.
        : > "$sbox/target"
        ln -s "$sbox/target" "$sbox/interfaces.d/eth1.conf" 2>/dev/null
        if [ -L "$sbox/interfaces.d/eth1.conf" ]; then
            printf 'PWN\n' | bash "$th" "$sbox/interfaces.d/eth1.conf" >/dev/null 2>&1
            rc=$?
            if [ "$rc" = 2 ] && [ ! -s "$sbox/target" ]; then
                pass "sandbox: symlink destination refused (exit 2, target untouched)"
            else
                fail "sandbox: a symlink destination was NOT refused (rc=$rc)"
            fi
        else
            printf 'sudoers-pin-contract: note  platform made no real symlink — symlink-refusal case skipped (Linux CI enforces it)\n'
        fi
        rm -rf "$sbox"
    else
        printf 'sudoers-pin-contract: note  mktemp unavailable — sandbox behavioural run skipped\n'
    fi
fi

[ "$fails" = 0 ] || printf 'sudoers-pin-contract: %s check(s) failed — audit B1 escalation not fully closed\n' "$fails"
exit "$fails"
