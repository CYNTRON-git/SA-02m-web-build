#!/bin/bash
# sudoers-pin-contract — the escalation-CLOSED gate for audit B1.
#
# Proves that an authenticated web session can no longer reach root without the
# device root password. It reads ALL SIX homes that grant www-data root — the
# five committed etc/sudoers.d/ drop-ins plus the tree-wide ban on a runtime
# append — not one file: until 1.0.6.24 it read only etc/sudoers.d/sa02m-www,
# so two unpinned grants (gateway, cloud) and two injection-shaped helpers were
# structurally invisible to the check that was supposed to prove B1 closed
# (.ai-dev/audit/security-verdict.md M1/H1/H2).
#
# The positive assertion is a COMPLETE LEDGER per home, not a handful of needles:
# every www-data Cmnd the home is expected to carry is enumerated, the count is
# asserted, and an UNEXPECTED Cmnd fails too. That is what makes commenting a
# grant line out go RED — the pre-1.0.6.24 weakness was incomplete enumeration,
# not comment-blindness (the file's comments were already stripped).
#
# Two class-closing assertions make the B1 lineage failure mechanically
# impossible to repeat:
#   A. grant path == deploy path, on all three delivery paths (installer for
#      fresh install AND `install.sh --refresh`, OTA map_dst, offline map). A
#      helper whose grant names /usr/local/sbin/x.sh while OTA lands
#      /usr/local/sbin/x is the 1.0.6.11 deploy gap repeating — the fix reaches
#      a path nobody calls and the vulnerable file stays.
#   B. no privileged helper builds code from an UNQUOTED interpreter heredoc,
#      and none builds a sed script from a shell variable — the two shapes H1
#      and H2 were.
#
# Static + a sandboxed behavioural run of the iface-conf-write helper (allow /
# refuse / symlink). The real on-device `sudo -n tee … → refused` proof is
# device-only (needs the sudoers loaded + real sudo) and is modelled here by the
# granted-Cmnd assertions over the committed files. `visudo -cf` runs on every
# install (sa02m_harden_sudoers) — device/CI-only, no visudo on a dev Windows box.
#
# HONESTY, stated so no later reader misreads the gate: the argument pins are
# DEFENCE IN DEPTH, not the fix. sudo matches command arguments with fnmatch and
# a `*` is widely documented as matching `/` too, so a prefix pin does not bound
# a path on its own (UNVERIFIED against this device's sudo — see the plan's
# adversary pass). H1 and H2 are closed by the IN-HELPER validation; the pins
# narrow the surface behind it.
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

# Comment handling is shared: .ai-dev/quality/checks/lib_check.sh is the one
# home for the strippers and for the SIGPIPE rule they encode (row check-lib
# self-tests it). This gate reads sudoers, shell and udev files, where a
# trailing ` #` is unambiguously a comment, so it uses the *_inline variants
# throughout — a rule named in prose above an assertion must never stand in
# for the assertion itself.
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/lib_check.sh" || { echo "sudoers-pin-contract: cannot source lib_check.sh"; exit 1; }

# ── 1. The committed grant exists and lists no dangerous raw Cmnd ───────────
# The POSITIVE side of this home (which pins must be present) is the complete
# ledger in section 9 — one home for that fact. This section keeps only the
# named negatives, each pinning a specific escalation vector B1 closed.
if [ -f "$SUD" ]; then
    pass "single-home sudoers drop-in present ($SUD)"
    grant=$(strip_comments_inline < "$SUD")

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
    acode=$(strip_comments_inline < "$APPLY")
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
    hb=$(strip_comments_inline < "$IFACE_HELPER")
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
    ub=$(strip_comments_inline < "$USB_HELPER")
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
    lcode=$(strip_comments_inline < "$LIBHW")
    # The dead I2C sudo fallback invoked `sudo -n "$bin"` (a variable — no literal
    # i2cset to grep), so assert the sa02m_hw_i2c_run_tool BODY carries no sudo at
    # all. Non-vacuous: RED on the pre-fix tree (its body has `sudo -n "$bin"`).
    i2c_body=$(sed -n '/^sa02m_hw_i2c_run_tool() {/,/^}/p' "$LIBHW")
    if [ -z "$i2c_body" ]; then
        fail "lib_hw: sa02m_hw_i2c_run_tool not found (cannot verify the dead fallback removal)"
    elif printf '%s\n' "$i2c_body" | strip_comments_inline | grep -q 'sudo'; then
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
stripped_has_inline scripts/03-webserver.sh "$gadd" && pass "03-webserver keeps usermod -aG i2c www-data" \
    || fail "03-webserver dropped 'usermod -aG i2c www-data' — beeper/alarm-LED break without the group"
stripped_has_inline scripts/update-www-only.sh "$gadd" && pass "update-www-only keeps usermod -aG i2c www-data" \
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
stripped_matches_inline scripts/03-webserver.sh 'sa02m_install_sudoers .+/etc/sudoers\.d/sa02m-www' \
    && pass "03-webserver installs the committed sudoers file via sa02m_install_sudoers" \
    || fail "03-webserver does not install etc/sudoers.d/sa02m-www via sa02m_install_sudoers"
stripped_matches_inline scripts/update-www-only.sh 'sa02m_install_sudoers .+/etc/sudoers\.d/sa02m-www' \
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

# ── 8. B1 deploy-gap: legacy sudoers removal + OTA helper .sh paths ─────────
stripped_has_inline scripts/lib.sh 'sa02m_remove_obsolete_www_sudoers' \
    && pass "lib.sh defines sa02m_remove_obsolete_www_sudoers (allow-list legacy cleanup)" \
    || fail "lib.sh missing sa02m_remove_obsolete_www_sudoers"
stripped_has_inline scripts/03-webserver.sh 'sa02m_cleanup_b1_deploy_artifacts' \
    && pass "03-webserver calls sa02m_cleanup_b1_deploy_artifacts after sudoers install" \
    || fail "03-webserver does not call sa02m_cleanup_b1_deploy_artifacts"
stripped_has_inline scripts/update-www-only.sh 'sa02m_cleanup_b1_deploy_artifacts' \
    && pass "update-www-only calls sa02m_cleanup_b1_deploy_artifacts" \
    || fail "update-www-only does not call sa02m_cleanup_b1_deploy_artifacts"
stripped_has_inline etc/sa02m-update-runner.sh 'cleanup_b1_deploy_artifacts' \
    && pass "update-runner cleans up B1 deploy artifacts after apply" \
    || fail "update-runner missing cleanup_b1_deploy_artifacts"
# The extension-less OTA twins a PAST release created must still be cleaned on
# every delivery path — a board that took one keeps a stale root helper at a
# path sudo does not grant, and (for the gateway/mqtt pair) the vulnerable file.
for _twin in sa02m-iface-conf-write sa02m-usb-power sa02m-gateway-config-apply \
             sa02m-mqtt-config-apply sa02m-conf-rm sa02m-mplc-project-deploy; do
    # Boundary-matched: the twin path must appear as a whole token, never as the
    # prefix of the .sh path it is the twin OF.
    if stripped_matches_inline etc/sa02m-update-runner.sh "/usr/local/sbin/$_twin([^A-Za-z0-9._-]|$)"; then
        pass "update-runner cleanup removes the extension-less OTA twin: /usr/local/sbin/$_twin"
    else
        fail "update-runner cleanup does NOT remove the extension-less OTA twin /usr/local/sbin/$_twin — a past OTA left a stale root helper there"
    fi
    if stripped_matches_inline scripts/lib.sh "/usr/local/sbin/$_twin([^A-Za-z0-9._-]|$)"; then
        pass "lib.sh twin cleanup covers /usr/local/sbin/$_twin"
    else
        fail "lib.sh sa02m_remove_stale_b1_helper_twins does NOT cover /usr/local/sbin/$_twin"
    fi
done

# ── 9-14. All six grant homes, the ledgers, and the two class assertions ────
# Everything that needs real sudoers parsing (continuation joining, Cmnd_Alias
# expansion, inline-comment stripping) and the deploy-path resolution runs in
# one python pass. It emits `ok<TAB>msg` / `FAIL<TAB>msg` lines plus a sentinel;
# a pass that does not reach the sentinel is itself a FAIL (non-vacuity — a
# crashed analysis must never read as green).
py_out=$(python3 - <<'PYGATE' 2>&1
import json
import os
import re
import sys
from pathlib import Path

BS = chr(92)
OUT = []


def ok(msg):
    OUT.append("ok\t" + msg)


def bad(msg):
    OUT.append("FAIL\t" + msg)


def read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ── sudoers parser ─────────────────────────────────────────────────────────
# Strips inline AND whole-line comments, joins continuations, expands
# Cmnd_Alias, and returns every Cmnd granted to www-data.
def parse_sudoers(path):
    text = read(path)
    if text is None:
        return None
    return parse_sudoers_text(text)


def parse_sudoers_text(text):
    lines = [re.sub(r"#.*$", "", ln) for ln in text.splitlines()]
    joined, buf = [], ""
    for ln in lines:
        s = ln.rstrip()
        if s.endswith(BS):
            buf += s[:-1] + " "
        else:
            joined.append((buf + s).strip())
            buf = ""
    if buf.strip():
        joined.append(buf.strip())
    aliases, grants = {}, []
    for ln in joined:
        if not ln or ln.startswith("Defaults"):
            continue
        m = re.match(r"^Cmnd_Alias\s+([A-Z0-9_]+)\s*=\s*(.*)$", ln)
        if m:
            aliases[m.group(1)] = [c.strip() for c in m.group(2).split(",") if c.strip()]
            continue
        m = re.match(r"^(\S+)\s+ALL\s*=\s*(?:\([^)]*\)\s*)?(?:NOPASSWD:\s*)?(.*)$", ln)
        if m:
            grants.append((m.group(1), [c.strip() for c in m.group(2).split(",") if c.strip()]))
    res = []
    for user, cmds in grants:
        if user != "www-data":
            continue
        for c in cmds:
            for e in aliases.get(c, [c]):
                res.append(re.sub(r"\s+", " ", e).strip())
    return res


# ── The complete grant ledger, per home ────────────────────────────────────
# Every www-data Cmnd each committed drop-in is expected to carry. Missing =>
# FAIL (a deleted or commented-out grant), unexpected => FAIL (a widened grant),
# count asserted. This is the one home for "what www-data may run as root".
LEDGER = {
    "etc/sudoers.d/sa02m-www": [
        "/bin/date",
        "/usr/bin/date",
        "/sbin/hwclock",
        "/usr/sbin/hwclock",
        "/usr/bin/timedatectl",
        "/usr/local/sbin/sa02m-iface-conf-write.sh",
        "/usr/local/sbin/sa02m-ensure-eth1-dhcp-hook.sh",
        "/sbin/ifup eth0",
        "/sbin/ifup eth1",
        "/sbin/ifup end0",
        "/sbin/ifup end1",
        "/sbin/ifdown eth0",
        "/sbin/ifdown eth1",
        "/sbin/ifdown end0",
        "/sbin/ifdown end1",
        "/sbin/reboot",
        "/usr/sbin/reboot",
        "/usr/bin/reboot",
        "/sbin/reboot -f",
        "/usr/sbin/reboot -f",
        "/usr/bin/reboot -f",
        "/sbin/shutdown -r now",
        "/usr/sbin/shutdown -r now",
        "/usr/bin/systemctl reboot",
        "/usr/bin/systemctl restart nginx",
        "/usr/bin/systemctl restart fcgiwrap",
        "/usr/bin/systemctl restart networking",
        "/usr/bin/systemctl restart networking.service",
        "/usr/bin/systemctl restart fix-eth.service",
        "/usr/local/sbin/sa02m-usb-power.sh *",
        "/usr/local/sbin/sa02m-set-storage-auto-format",
        "/usr/local/sbin/sa02m-web-update-check",
        "/usr/local/sbin/sa02m-web-update-apply",
        "/usr/bin/systemctl start sa02m-update.service",
        "/usr/bin/systemctl stop sa02m-update.service",
        "/usr/bin/systemctl reset-failed sa02m-update.service",
        "/usr/bin/systemctl start sa02m-factory-reset.service",
        "/usr/local/libexec/sa02m-update-inspect",
        "/usr/local/sbin/sa02m-web-backup.sh",
        "/usr/local/sbin/sa02m-web-reboot.sh",
        "/usr/local/sbin/sa02m-web-restart-services.sh",
        "/usr/local/sbin/sa02m-web-service-ctl.sh list",
        "/usr/local/sbin/sa02m-web-service-ctl.sh start *",
        "/usr/local/sbin/sa02m-web-service-ctl.sh stop *",
        "/usr/local/sbin/sa02m-web-service-ctl.sh install *",
        "/usr/local/sbin/sa02m-web-service-ctl.sh uninstall *",
        "/usr/local/sbin/sa02m-rs485-stats.sh",
        "/usr/local/sbin/sa02m-kernel-select.sh status --json",
        "/usr/local/sbin/sa02m-kernel-select.sh set smp",
        "/usr/local/sbin/sa02m-kernel-select.sh set rt",
        "/usr/local/sbin/sa02m-kernel-select.sh refresh",
        "/usr/local/sbin/sa02m-set-cpu-profile",
        "/usr/local/sbin/sa02m-cpu-profile.sh status --json",
        "/usr/local/sbin/sa02m-cpu-profile.sh apply",
        "/usr/local/sbin/sa02m-web-root-cmd.sh *",
        "/usr/local/sbin/sa02m-mplc-project-deploy.sh *",
        "/usr/local/sbin/sa02m-conf-rm.sh",
        "/usr/local/sbin/sa02m-commit-web-env",
    ],
    "etc/sudoers.d/sa02m-cloud": [
        "/usr/local/sbin/sa02m-cloud-web-trigger.sh pair",
        "/usr/local/sbin/sa02m-cloud-web-trigger.sh cancel",
        "/usr/local/sbin/sa02m-cloud-web-trigger.sh enable",
        "/usr/local/sbin/sa02m-cloud-web-trigger.sh disable",
        "/usr/local/sbin/sa02m-cloud-web-trigger.sh token *",
    ],
    "etc/sudoers.d/sa02m-mqtt": [
        "/bin/systemctl restart mosquitto",
        "/usr/bin/systemctl restart mosquitto",
        "/bin/systemctl start mosquitto",
        "/usr/bin/systemctl start mosquitto",
        "/bin/systemctl stop mosquitto",
        "/usr/bin/systemctl stop mosquitto",
        "/bin/systemctl restart sa02m-modbus-mqtt",
        "/usr/bin/systemctl restart sa02m-modbus-mqtt",
        "/bin/systemctl start sa02m-modbus-mqtt",
        "/usr/bin/systemctl start sa02m-modbus-mqtt",
        "/bin/systemctl stop sa02m-modbus-mqtt",
        "/usr/bin/systemctl stop sa02m-modbus-mqtt",
        "/bin/systemctl restart sa02m-telemetry",
        "/usr/bin/systemctl restart sa02m-telemetry",
        "/bin/systemctl start sa02m-telemetry",
        "/usr/bin/systemctl start sa02m-telemetry",
        "/bin/systemctl stop sa02m-telemetry",
        "/usr/bin/systemctl stop sa02m-telemetry",
        "/usr/bin/python3 /opt/sa02m-modbus-mqtt/mqtt_bus_scan.py *",
        "/usr/local/sbin/sa02m-mqtt-config-apply.sh *",
        "/usr/local/sbin/sa02m-mqtt-external-info.py",
        "/usr/bin/cat /etc/sa02m_mqtt.env",
    ],
    "etc/sudoers.d/sa02m-gateway": [
        "/usr/local/sbin/sa02m-gateway-config-apply.sh /tmp/sa02m-gwcfg-out.*",
        "/usr/bin/systemctl start sa02m-serial-gateway",
        "/usr/bin/systemctl stop sa02m-serial-gateway",
        "/usr/bin/systemctl restart sa02m-serial-gateway",
        "/usr/bin/systemctl reload sa02m-serial-gateway",
    ],
    "etc/sudoers.d/sa02m-alice": [
        "/usr/local/sbin/sa02m-alice-web-trigger.sh enable",
        "/usr/local/sbin/sa02m-alice-web-trigger.sh disable",
        "/usr/local/sbin/sa02m-alice-web-trigger.sh restart",
        # Second unit (sa02m-cloud-control, 1.0.6.26) — same helper, two more
        # pinned verbs; the CGI sends exactly these on cloud_control_enable /
        # cloud_control_disable.
        "/usr/local/sbin/sa02m-alice-web-trigger.sh cloud-enable",
        "/usr/local/sbin/sa02m-alice-web-trigger.sh cloud-disable",
    ],
}

granted_all = []
for home in sorted(LEDGER):
    want = LEDGER[home]
    if not Path(home).is_file():
        bad(
            "grant home MISSING: %s — a grant written by an installer heredoc "
            "instead of a committed file never reaches an OTA board, and a "
            "`[ ! -f ]`-guarded heredoc never reaches an already-deployed one" % home
        )
        continue
    got = parse_sudoers(home)
    if not got:
        bad("grant home %s parsed to ZERO www-data Cmnd entries (non-vacuous ledger check)" % home)
        continue
    granted_all += got
    missing = [c for c in want if c not in got]
    extra = [c for c in got if c not in want]
    for c in missing:
        bad("grant home %s: LEDGERED Cmnd absent (deleted or commented out): %s" % (home, c))
    for c in extra:
        bad("grant home %s: UNEXPECTED www-data Cmnd, not in the ledger: %s" % (home, c))
    if len(got) != len(want):
        bad("grant home %s: %d www-data Cmnd entries, ledger expects %d" % (home, len(got), len(want)))
    elif not missing and not extra:
        ok("grant home %s: complete ledger holds (%d Cmnd entries, none missing, none extra)" % (home, len(got)))

if not granted_all:
    bad("NO www-data grant parsed from ANY home — the ledger check is vacuous")

# ── Homes 4-6: heredoc-written grants and the runtime append, tree-wide ────
# A `>>` append cannot converge: it never REMOVES a stale dangerous grant, and
# it falsifies the "installed WHOLESALE" claim in sa02m-www's own header. The
# earlier check covered two named scripts; this one is tree-wide.
SCAN_DIRS = ("scripts", "etc", "usr", "www", "opt", "tools")
SKIP_PARTS = {".git", "node_modules", "__pycache__", "private", ".tmp", "artifacts"}
TEXT_EXT = {".sh", ".cgi", ".py", ".bash", ".service", ".conf", ".json", ".yaml", ".yml", ""}

tree_files = []
for d in SCAN_DIRS:
    base = Path(d)
    if not base.is_dir():
        continue
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        if SKIP_PARTS & set(p.parts):
            continue
        tree_files.append(p.as_posix())
for extra_top in ("install.sh",):
    if Path(extra_top).is_file():
        tree_files.append(extra_top)

if len(tree_files) < 50:
    bad("tree sweep found only %d files — the append/heredoc sweeps would be vacuous" % len(tree_files))

APPEND_RE = re.compile(r">>\s*[\"']?\S*/etc/sudoers\.d/")
# A heredoc whose BODY is a sudoers grant. Content-matched, not target-matched:
# the two live instances write to a VARIABLE ($SUDOERS_FILE), so a rule keyed on
# the literal /etc/sudoers.d/ path on the redirect line sees nothing.
GRANT_BODY_RE = re.compile(r"^\s*(\S+\s+ALL\s*=|Cmnd_Alias\b)", re.M)
HEREDOC_OPEN_RE = re.compile(r"<<-?\s*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1")


def heredocs(text):
    """Yield (line_no, opener_line, body, quoted_delimiter) for every heredoc.

    A COMMENT line never opens a heredoc, and skipping it matters twice over: a
    header that documents the banned `<<PYEOF` shape would otherwise read as an
    instance of it, and — because an unterminated pseudo-heredoc consumes to
    EOF — would blind the scanner to every real heredoc below it.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = None if lines[i].lstrip().startswith("#") else HEREDOC_OPEN_RE.search(lines[i])
        if m:
            delim = m.group(2)
            body, j = [], i + 1
            while j < len(lines) and lines[j].strip() != delim:
                body.append(lines[j])
                j += 1
            yield i + 1, lines[i], "\n".join(body), bool(m.group(1))
            i = j
        i += 1


append_hits, heredoc_hits = [], []
granted_heredoc = []
for rel in tree_files:
    if Path(rel).suffix not in TEXT_EXT:
        continue
    text = read(rel)
    if text is None:
        continue
    for i, ln in enumerate(text.splitlines(), 1):
        code = re.sub(r"^\s*#.*$", "", ln)
        if APPEND_RE.search(code):
            append_hits.append("%s:%d" % (rel, i))
    if not rel.endswith((".sh", ".cgi")):
        continue
    for lineno, _opener, body, _quoted in heredocs(text):
        if GRANT_BODY_RE.search(body):
            heredoc_hits.append("%s:%d" % (rel, lineno))
            granted_heredoc += parse_sudoers_text(body)

if append_hits:
    for h in append_hits:
        bad("runtime APPEND to a /etc/sudoers.d/ file at %s — a grant home that never converges (install the committed file wholesale instead)" % h)
else:
    ok("no `>>` append to any /etc/sudoers.d/ file anywhere in the tree (%d files swept)" % len(tree_files))

if heredoc_hits:
    for h in heredoc_hits:
        bad("installer writes a sudoers grant by HEREDOC at %s — promote it to a committed etc/sudoers.d/ file installed via sa02m_install_sudoers, else the pin never reaches an OTA board and a `[ ! -f ]`-guarded heredoc never reaches an already-deployed one" % h)
else:
    ok("no installer writes a sudoers grant by heredoc (%d files swept)" % len(tree_files))

# Every grant this tree can produce, committed or heredoc-written. The ledger
# above judges only the committed homes; pinning, the deploy-path check and the
# helper sweep must see the heredoc-written grants too — that is precisely where
# the two unpinned escalation grants were hiding.
granted_any = granted_all + granted_heredoc

# ── Argument pinning for the three privileged web triggers ─────────────────
# A sudoers Cmnd written WITHOUT arguments permits ANY argument vector. These
# three are the escalation surface B1 is about, so each granted form must carry
# at least one argument. Non-vacuous: zero grants for a helper is a FAIL.
PIN_REQUIRED = [
    "/usr/local/sbin/sa02m-cloud-web-trigger.sh",
    "/usr/local/sbin/sa02m-alice-web-trigger.sh",
    "/usr/local/sbin/sa02m-gateway-config-apply.sh",
]
for helper in PIN_REQUIRED:
    forms = [c for c in granted_any if c.split(" ")[0] == helper]
    if not forms:
        bad("no sudoers grant found for %s anywhere — either the helper is unreachable or the ledger has drifted off it" % helper)
        continue
    bare = [c for c in forms if len(c.split(" ")) < 2]
    if bare:
        bad("UNPINNED grant (any argument vector permitted): %s" % bare[0])
    else:
        ok("argument-pinned in every granted form (%d): %s" % (len(forms), helper))

# ── Assertion A: grant path == deploy path, on all three delivery paths ─────
# For every repo-owned absolute path a committed grant names, the repo source
# that serves it must land at EXACTLY that path on the installer path (fresh
# install and `install.sh --refresh` run the same scripts/), on OTA (map_dst in
# etc/sa02m-update-runner.sh) and on the offline package
# (scripts/offline-update-deploy-map.json). A source that lands somewhere ELSE
# is the 1.0.6.11 B1 deploy gap: the fix goes to a path nobody calls while the
# vulnerable file at the granted path stays exactly as it was.
runner_text = read("etc/sa02m-update-runner.sh")
map_dst = None
if runner_text:
    m = re.search(r"^MPLC_OTA_PLUGINS = .*?(?=^def deploy_mode)", runner_text, re.M | re.S)
    if m:
        ns = {"Path": Path}
        try:
            exec(m.group(0), ns)  # noqa: S102 - the runner's own source is the contract
            map_dst = ns.get("map_dst")
        except Exception as e:  # pragma: no cover - a broken runner is a FAIL below
            bad("could not evaluate map_dst() out of etc/sa02m-update-runner.sh: %s" % e)
if map_dst is None:
    bad("map_dst() not extractable from etc/sa02m-update-runner.sh — the OTA leg of the grant-path check cannot run")

try:
    OFF = json.loads(read("scripts/offline-update-deploy-map.json") or "")
except Exception as e:
    OFF = None
    bad("scripts/offline-update-deploy-map.json unreadable (%s) — the offline leg cannot run" % e)


def offline_dsts(rel):
    """Every destination the offline map could give this source (usually 0 or 1)."""
    if not OFF:
        return []
    res = []
    ex = (OFF.get("exact_rules") or {}).get(rel)
    if ex:
        res.append(ex.get("dst"))
    rn = (OFF.get("etc_helper_renames") or {}).get(rel)
    if rn:
        res.append(rn.get("dst"))
    for pr in OFF.get("prefix_rules") or []:
        if rel.startswith(pr["src_prefix"]):
            res.append(pr["dst_prefix"] + rel[len(pr["src_prefix"]):])
    return res


# Repo sources that could serve a granted absolute path, matched by basename
# (with and without the .sh extension — the exact near-miss that broke B1).
by_name = {}
for rel in tree_files:
    by_name.setdefault(os.path.basename(rel), []).append(rel)


def repo_sources_for(dst):
    base = os.path.basename(dst)
    names = {base, base + ".sh"}
    if base.endswith(".sh"):
        names.add(base[:-3])
    hits = []
    for n in names:
        hits += by_name.get(n, [])
    return sorted(set(hits))


# Sources delivered ONLY by the installer, with the reason. `scripts/` is the
# installer itself and is deliberately never OTA-mapped; a board gets these
# through install.sh / install.sh --refresh. Each entry is accepted debt with a
# stated reason — the same ledger discipline as CONTRAST_WHITELIST in ui-layout.
INSTALLER_ONLY = {
    "scripts/sa02m-rs485-stats.sh": "scripts/ is the installer tree — never OTA-mapped by design; the offline map carries an exact_rule for it",
}

granted_paths = set()
for c in granted_any:
    for tok in c.split(" "):
        if tok.startswith("/usr/local/") or tok.startswith("/opt/sa02m-"):
            granted_paths.add(tok)

if not granted_paths:
    bad("no repo-owned helper path found in any grant — the grant-path/deploy-path check is vacuous")

installer_text = ""
for rel in tree_files:
    if rel.startswith("scripts/") and rel.endswith(".sh"):
        installer_text += (read(rel) or "") + "\n"
installer_text += read("install.sh") or ""

checked_a = 0
for dst in sorted(granted_paths):
    srcs = repo_sources_for(dst)
    if not srcs:
        bad("granted helper %s has NO repo source — the grant names a path this repo never ships" % dst)
        continue
    # Installer leg (fresh install AND `install.sh --refresh` run the same scripts).
    if dst in installer_text or os.path.dirname(dst) in installer_text:
        pass
    else:
        bad("granted helper %s is installed by NO script under scripts/ — a fresh install and an `install.sh --refresh` never place it at the granted path" % dst)
    for src in srcs:
        if src in INSTALLER_ONLY:
            ok("grant/deploy path: %s <- %s (installer-only, ledgered: %s)" % (dst, src, INSTALLER_ONLY[src]))
            checked_a += 1
            continue
        if map_dst is not None:
            try:
                got = map_dst(src)
            except Exception:
                got = None
            if got is not None and got != dst:
                bad(
                    "OTA DEPLOY GAP: %s maps to %s, but sudo grants %s — the OTA-delivered fix lands at a path nobody calls while the granted (vulnerable) file stays"
                    % (src, got, dst)
                )
            elif got is None:
                bad("OTA cannot deliver %s at all (map_dst returned nothing) while sudo grants %s" % (src, dst))
            else:
                checked_a += 1
        offs = offline_dsts(src)
        wrong = [d for d in offs if d != dst]
        if wrong:
            bad("OFFLINE DEPLOY GAP: %s -> %s in scripts/offline-update-deploy-map.json, but sudo grants %s" % (src, wrong[0], dst))
        elif not offs:
            bad("scripts/offline-update-deploy-map.json has no rule for %s — the offline package never delivers the helper sudo grants at %s" % (src, dst))
if checked_a:
    ok("grant path == deploy path on all three delivery paths for %d repo source(s) behind %d granted helper path(s)" % (checked_a, len(granted_paths)))
else:
    bad("the grant-path/deploy-path check verified NOTHING (vacuous)")

# ── Assertion B: no privileged helper builds code from untrusted text ───────
# Sweep every repo file a sudoers grant names.
#  B1. An UNQUOTED interpreter heredoc (`python3 - <<PY`, not `<<'PY'`) whose
#      body interpolates a shell variable IS remote code execution as root when
#      the variable is caller-supplied — exactly H1.
#  B2. A `sed` script built from a double-quoted shell variable is the same
#      class through a different tool — exactly H2 (a `|` in the value closes
#      the s||| expression; GNU sed's `e` flag then executes it as root).
sweep = []
for dst in sorted(granted_paths):
    sweep += repo_sources_for(dst)
sweep = sorted(set(s for s in sweep if s.endswith((".sh", ".py", ".cgi"))))

if len(sweep) < 5:
    bad("privileged-helper sweep resolved only %d files — assertion B would be vacuous" % len(sweep))

INTERP = re.compile(r"\b(python3?|perl|ruby|node|awk|bash|sh)\b")
# A double-quoted sed SCRIPT: an optional address, then an s/y command with its
# delimiter. Deliberately NOT "any double-quoted word on a sed line" — the
# repo's sed calls quote their FILE argument ("$tgt", "$_pkg") while keeping the
# script single-quoted, and flagging those would drown the real finding.
# Known limit, stated rather than hidden: an expression passed as a bare
# variable (`sed -i $EXPR f`) is not detected.
SED_SCRIPT_RE = re.compile(r"^[0-9,^$/]*\s*[sy][^\w\s]")
VAR_RE = re.compile(r"\$[{(]?[A-Za-z_]")

b1_hits, b2_hits = [], []
for rel in sweep:
    text = read(rel)
    if text is None:
        continue
    lines = text.splitlines()
    for lineno, opener, body, quoted in heredocs(text):
        if quoted:
            continue
        if INTERP.search(opener.split("<<")[0]) and VAR_RE.search(body):
            b1_hits.append("%s:%d" % (rel, lineno))
    for k, ln in enumerate(lines, 1):
        code = re.sub(r"^\s*#.*$", "", ln)
        if not re.search(r"\bsed\b", code):
            continue
        for arg in re.findall(r"\"([^\"]*)\"", code):
            if SED_SCRIPT_RE.search(arg) and VAR_RE.search(arg):
                b2_hits.append("%s:%d" % (rel, k))
                break

if b1_hits:
    for h in b1_hits:
        bad("privileged helper builds interpreter source from an UNQUOTED heredoc that interpolates a shell variable at %s — use a quoted delimiter and pass the value as argv/env (root code injection, the H1 shape)" % h)
else:
    ok("no privileged helper builds code from an unquoted interpreter heredoc (%d helper files swept)" % len(sweep))

if b2_hits:
    for h in b2_hits:
        bad("privileged helper builds a sed script from a double-quoted shell variable at %s — a delimiter character in the value escapes the expression (root command execution via sed's `e`/`w` flags, the H2 shape)" % h)
else:
    ok("no privileged helper builds a sed script from a shell variable (%d helper files swept)" % len(sweep))

# ── Helper-side validation: the pin is not the fix ─────────────────────────
# Every arg-taking privileged helper must validate its OWN argv, because the
# sudoers wildcard's reach is not something this repo has verified (a sudo
# argument `*` is documented to match `/` as well).
#
# TWO layers, because the first one alone was hollow. Until the 1.0.6.24
# security review this block searched for literal substrings only, so it could
# not tell a guard that RUNS from a guard that is merely DEFINED: deleting the
# whole `if ! src_path_ok "$TMP_SRC"; then … exit 2; fi` block while leaving the
# function definition left this gate at exit 0 — and that mutation restores
# "install any caller-named file as the root gateway config", the trust-the-
# caller shape the branch exists to remove (review finding F3, reproduced by
# running before this check was written).
#   Layer 1 — `markers`: the guard's CONTENT still bounds the right things.
#   Layer 2 — `calls`:   the guard is INVOKED, in a refusal construct, before
#                        the privileged action, outside its own body.
HELPER_GUARDS = [
    ("usr/local/sbin/sa02m-gateway-config-apply.sh", [
        ("/tmp/", "restricts the caller-supplied path to /tmp"),
        ("sa02m-gwcfg-out", "pins the caller's mktemp basename"),
        ("-L ", "refuses a symlink"),
    ], [
        ("src_path_ok", [r"\binstall\s+-m\s+0660\b", r"\bdd\b[^\n]*\bif="],
         "installs the caller's file as the root gateway config"),
    ]),
    ("usr/local/sbin/sa02m-mqtt-config-apply.sh", [
        ("/tmp/", "restricts the caller-supplied path to /tmp"),
        ("sa02m-mqcfg-out", "pins the caller's mktemp basename"),
        ("-L ", "refuses a symlink"),
    ], [
        ("src_path_ok", [r"\binstall\s+-m\s+0660\b", r"\bdd\b[^\n]*\bif="],
         "installs the caller's file as the root MQTT bridge config"),
    ]),
    ("usr/local/sbin/sa02m-cloud-web-trigger.sh", [
        ("valid_server_host", "validates the server hostname inside the helper"),
        ("A-Za-z0-9", "carries an explicit hostname character allow-list"),
    ], [
        ("valid_server_host", [r"SA02M_SERVER=", r"os\.replace\("],
         "rewrites the root agent.conf with the caller's hostname"),
    ]),
    ("opt/sa02m-modbus-mqtt/mqtt_bus_scan.py", [
        ("sa02m-mqttscan", "pins the caller's mktemp basename"),
        ("islink", "refuses a symlink"),
        ("/dev/", "allow-lists the serial port path it opens as root"),
    ], [
        ("params_path_ok", [r"serial\.Serial\("],
         "opens the caller-named serial port as root"),
    ]),
]

# The function's own body, so a call INSIDE it is not mistaken for a call site.
# Returns (def_index, last_body_index) 0-based inclusive, or None when the
# function is not defined at all.
def _fn_span(lines, fn, python):
    if python:
        dre = re.compile(r"^(\s*)def\s+%s\s*\(" % re.escape(fn))
    else:
        dre = re.compile(r"^(\s*)(?:function\s+)?%s\s*\(\)" % re.escape(fn))
    for i, ln in enumerate(lines):
        m = dre.match(ln)
        if not m:
            continue
        indent = len(m.group(1))
        for j in range(i + 1, len(lines)):
            s = lines[j]
            if not s.strip():
                continue
            if python:
                if len(s) - len(s.lstrip()) <= indent:
                    return i, j - 1
            elif re.match(r"^\s{0,%d}\}\s*$" % max(indent, 0), s):
                return i, j
        return i, len(lines) - 1
    return None

# A call that REFUSES: `if ! guard …` / `guard … ||` in shell, `if not guard(`
# in Python, followed within a few lines by a non-zero exit / refuse / raise.
# A bare `guard "$x"` whose result is discarded is not a guard.
def _refusing_call(lines, idx, fn, python):
    ln = lines[idx]
    if python:
        if not re.search(r"\bnot\s+%s\s*\(" % re.escape(fn), ln):
            return False
        window = "\n".join(lines[idx:idx + 5])
        return bool(re.search(r"\b(refuse|sys\.exit|raise)\b", window))
    if not (re.search(r"^\s*if\s+!\s+%s\b" % re.escape(fn), ln)
            or re.search(r"%s\b[^\n]*\|\|" % re.escape(fn), ln)):
        return False
    window = "\n".join(lines[idx:idx + 8])
    return bool(re.search(r"\b(exit\s+[1-9]|return\s+[1-9])", window))

for rel, markers, calls in HELPER_GUARDS:
    text = read(rel)
    if text is None:
        bad("privileged helper missing from the tree: %s" % rel)
        continue
    # Whole-line comments blanked, never removed — line indexes stay comparable
    # to the real file, and a `#`-disabled guard reads as absent.
    lines = [re.sub(r"^\s*#.*$", "", ln) for ln in text.splitlines()]
    code = "\n".join(lines)
    python = rel.endswith(".py")

    absent = [why for needle, why in markers if needle not in code]
    for why in absent:
        bad("%s no longer %s — the in-helper validation is what closes the hole; the sudoers pin is only defence in depth" % (rel, why))

    invoked = []
    for fn, sink_res, sink_why in calls:
        span = _fn_span(lines, fn, python)
        if span is None:
            bad("%s no longer defines the guard %s() — the argv validation is gone entirely" % (rel, fn))
            continue
        d0, d1 = span
        fre = re.compile(r"\b%s\b" % re.escape(fn))
        call_idxs = [i for i, ln in enumerate(lines)
                     if fre.search(ln) and not (d0 <= i <= d1)]
        if not call_idxs:
            bad("%s DEFINES %s() but never calls it — a defined-but-uncalled guard validates nothing, and the helper is back to trusting the caller's argv (it %s)" % (rel, fn, sink_why))
            continue
        refusing = [i for i in call_idxs if _refusing_call(lines, i, fn, python)]
        if not refusing:
            bad("%s calls %s() but not as a refusal (expected `if ! %s …` / `%s … ||` guarding a non-zero exit) — a call whose result is discarded validates nothing" % (rel, fn, fn, fn))
            continue
        sink_idxs = [i for i, ln in enumerate(lines)
                     if any(re.search(r, ln) for r in sink_res)]
        if not sink_idxs:
            bad("%s: none of the privileged-action patterns %r matched — the helper was restructured and this assertion has stopped watching anything; update the sink pattern, do not delete the check" % (rel, sink_res))
            continue
        if min(refusing) > min(sink_idxs):
            bad("%s calls %s() at line %d, AFTER it already %s at line %d — validation must precede the privileged action" % (rel, fn, min(refusing) + 1, sink_why, min(sink_idxs) + 1))
            continue
        invoked.append("%s() refuses at line %d before it %s at line %d"
                       % (fn, min(refusing) + 1, sink_why, min(sink_idxs) + 1))

    if not absent and len(invoked) == len(calls):
        ok("%s validates its own argv — %s; guard invoked, not merely defined: %s"
           % (rel, ", ".join(why for _, why in markers), "; ".join(invoked)))

# ── A pin never travels without its helper ─────────────────────────────────
# The grant/deploy check above proves each granted path is deliverable; it does
# NOT prove that a script converging a sudoers drop-in also delivers the helper
# that drop-in pins. Two paths did exactly that: scripts/update-www-only.sh and
# etc/sa02m-web-update-apply.sh installed the new sa02m-gateway / sa02m-mqtt
# pins while shipping neither config-apply helper, so a board updated only that
# way kept the vulnerable, trust-the-caller helper and reported the new version
# (review finding F4). Nothing broke — the pins permit the argv the shipped CGIs
# send — which is precisely why it was invisible.
#
# Scoped to the helpers whose in-helper argv validation IS the H1/H2 fix, and
# keyed on the path DEPLOYING THE PANEL rather than on the path installing the
# pin. Keying on the pin was itself hollow (caught while RED-proving this check:
# `sa02m-gateway` matches inside `sa02m-gateway-config-apply.sh`, so the helper
# line satisfied the very precondition it was supposed to be tested against),
# and it under-reaches — the legacy rsync path installs only sudoers.d/sa02m-www
# yet still replaces every CGI that calls these helpers. The true invariant is
# simpler: a path that updates the panel must also update the privileged helpers
# the panel calls, or the new CGI drives the old helper.
PIN_CARRIERS = [
    "scripts/03-webserver.sh",
    "scripts/update-www-only.sh",
    "etc/sa02m-web-update-apply.sh",
]
GUARDED_HELPER_SRCS = [rel for rel, _m, _c in HELPER_GUARDS if rel.startswith("usr/local/sbin/")]
carrier_text = {}
for rel in PIN_CARRIERS:
    t = read(rel)
    if t is None:
        bad("pin-carrier script missing from the tree: %s — this assertion cannot run" % rel)
    carrier_text[rel] = "\n".join(re.sub(r"^\s*#.*$", "", ln) for ln in (t or "").splitlines())

# The installer splits its work across scripts/0*.sh, so 03-webserver.sh is
# read together with its siblings — one installer run executes them all.
installer_siblings = "".join(
    "\n".join(re.sub(r"^\s*#.*$", "", ln) for ln in (read(rel) or "").splitlines()) + "\n"
    for rel in tree_files
    if re.match(r"^scripts/0\d+-.*\.sh$", rel) or rel == "install.sh"
)
carrier_text["scripts/03-webserver.sh"] = installer_siblings

checked_pin = 0
for carrier in PIN_CARRIERS:
    text = carrier_text[carrier]
    # Does this path replace the panel's CGI tree? Every one of them rsyncs /
    # copies www/network_config into the web root.
    if not re.search(r"www/network_config|WEB_ROOT", text):
        bad("%s no longer looks like a panel-deploying path (no www/network_config / WEB_ROOT reference) — this assertion has stopped watching it; update the carrier list, do not delete the check" % carrier)
        continue
    for src in GUARDED_HELPER_SRCS:
        base = os.path.basename(src)
        dst = "/" + src
        grants = sorted(
            os.path.basename(f) for f in tree_files
            if f.startswith("etc/sudoers.d/") and dst in (read(f) or "")
        )
        if not grants:
            bad("no etc/sudoers.d/ file grants %s — either the grant was dropped or this assertion's path shape drifted; it is now watching nothing" % dst)
            continue
        # Whole-token match: the trailing (?![\w.-]) is what stops the drop-in
        # name `sa02m-gateway` from matching inside `sa02m-gateway-config-apply.sh`
        # (the circularity that made the first draft of this check hollow). The
        # leading class must NOT exclude `/` or `.` — every install line writes
        # the basename right after a slash.
        if re.search(r"(?<![\w-])%s(?![\w.-])" % re.escape(base), text):
            ok("%s deploys the panel AND delivers %s (granted by sudoers.d/%s)"
               % (carrier, base, ",".join(grants)))
            checked_pin += 1
        else:
            bad("PIN WITHOUT HELPER: %s replaces the panel's CGI tree but never delivers %s, which sudoers.d/%s grants at %s — a board updated only through that path runs the NEW CGI against the OLD, trust-the-caller helper while reporting the new version (the in-helper argv validation IS the fix; the pin is only defence in depth)"
                % (carrier, base, ",".join(grants), dst))
if checked_pin:
    ok("every panel-deploying path also delivers the privileged helpers the panel calls (%d pairings checked)" % checked_pin)
else:
    bad("the pin-without-helper check verified NOTHING (vacuous) — no carrier/helper pairing resolved")

# ── TOCTOU: validated bytes must BE the installed bytes ────────────────────
# A path check is a statement about an inode at check time, not at use time.
# www-data owns the /tmp file these helpers are handed and can replace it with a
# symlink after `[ -L ]`/`[ -f ]` and before `install`, which follows symlinks on
# its source — any root-readable file (/etc/shadow) then lands in the destination
# as 0660 root:www-data (review finding F2, demonstrated by running the old and
# new shapes side by side with the swap injected into the window). The fix shape:
# open ONCE, verify the OPENED inode through /proc/self/fd, copy into a 0700
# root-owned work dir, and validate + install THAT copy.
TOCTOU_HELPERS = [
    ("usr/local/sbin/sa02m-gateway-config-apply.sh", "TMP_SRC"),
    ("usr/local/sbin/sa02m-mqtt-config-apply.sh", "SRC"),
]
for rel, argv_var in TOCTOU_HELPERS:
    text = read(rel)
    if text is None:
        bad("privileged helper missing from the tree: %s" % rel)
        continue
    lines = [re.sub(r"^\s*#.*$", "", ln) for ln in text.splitlines()]
    code = "\n".join(lines)
    inst = [i for i, ln in enumerate(lines) if re.search(r"\binstall\s+-m\s+0660\b", ln)]
    if not inst:
        bad("%s: no `install -m 0660` line found — the helper was restructured and this TOCTOU assertion has stopped watching anything; update the pattern, do not delete the check" % rel)
        continue
    raw = [i for i in inst
           if re.search(r"\$\{?%s\b" % re.escape(argv_var), lines[i])]
    if raw:
        bad("%s installs the CALLER-SUPPLIED path ($%s) directly at line %d — the path check above it only holds at check time, so www-data can swap in a symlink before `install` follows it and copy any root-readable file into the root config (review F2). Install a private copy taken through one verified open instead."
            % (rel, argv_var, raw[0] + 1))
        continue
    missing = []
    if "/proc/self/fd" not in code:
        missing.append("verify the inode it actually opened via /proc/self/fd (readlink), so a symlink swapped in after the path check is refused rather than followed")
    if not re.search(r"\bmktemp\s+-d\b", code):
        missing.append("stage the copy in a private root-owned work dir (mktemp -d) www-data cannot reach")
    if not re.search(r"\btimeout\s+\d+\s+bash\b", code):
        missing.append("bound the open with `timeout` so a FIFO swapped in cannot hang the helper as root")
    if missing:
        for why in missing:
            bad("%s no longer does one thing the TOCTOU fix rests on: %s" % (rel, why))
    else:
        ok("%s installs a private copy taken through one verified open, never the caller's path (TOCTOU closed at use time, not just check time)" % rel)

# The confession comment H2 shipped with must not come back.
cloud = read("usr/local/sbin/sa02m-cloud-web-trigger.sh") or ""
if re.search(r"already validated by caller", cloud, re.I):
    bad("usr/local/sbin/sa02m-cloud-web-trigger.sh still claims its caller validated the hostname — the sudoers grant makes cloud.cgi not the only caller")
else:
    ok("cloud trigger no longer claims 'validated by caller' (it validates for itself)")

OUT.append("__DONE__\tgrant-registry analysis completed")
sys.stdout.write("\n".join(OUT) + "\n")
PYGATE
)
py_rc=$?
if [ "$py_rc" != 0 ] || [ -z "$py_out" ]; then
    fail "grant-registry analysis did not run (python3 rc=$py_rc) — the six-home checks are NOT covered"
    [ -n "$py_out" ] && printf '%s\n' "$py_out"
else
    saw_done=0
    while IFS=$'\t' read -r verdict msg; do
        case "$verdict" in
            ok) pass "$msg" ;;
            FAIL) fail "$msg" ;;
            __DONE__) saw_done=1 ;;
            "") : ;;
            *) printf 'sudoers-pin-contract: note  %s\n' "$verdict$msg" ;;
        esac
    done <<<"$py_out"
    [ "$saw_done" = 1 ] || fail "grant-registry analysis stopped before its sentinel — treat as NOT run"
fi

[ "$fails" = 0 ] || printf 'sudoers-pin-contract: %s check(s) failed — audit B1 escalation not fully closed\n' "$fails"
exit "$fails"
