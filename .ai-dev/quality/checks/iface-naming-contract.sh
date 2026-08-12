#!/bin/bash
# Static gate for docs/contracts/ethernet-iface-naming.md.
#
# Guards the canonical-interface-naming contract against the regressions that
# would silently undo it: a new consumer hardcoding a legacy name, the installer
# losing the unit, and the F6 `echo -e` write path coming back.
#
# Run: bash .ai-dev/quality/checks/iface-naming-contract.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1

fails=0
fail() { printf 'iface-naming-contract: FAIL  %s\n' "$*"; fails=$((fails + 1)); }
pass() { printf 'iface-naming-contract: ok    %s\n' "$*"; }

# ── 1. The legacy-name ledger ──────────────────────────────────────────────
# The compat layer (D2 keeps resolve_lan_iface/first_existing_iface), the rename
# script, and the installer's migration are the ONLY code allowed to name
# end0/end1. This is an accepted-debt ledger in the project's existing idiom
# (cf. the ui-layout touch/contrast whitelists): non-vacuous BOTH ways — a new
# file entering the set fails, and a stale entry that no longer matches fails
# too. Widening it is a deliberate one-line change, reviewed as such.
LEDGER="etc/sa02m-conf-rm.sh
etc/sa02m-iface-canonical.sh
etc/sa02m_network.conf
install.sh
scripts/02-network.sh
scripts/lib.sh
www/network_config/cgi-bin/apply.cgi
www/network_config/cgi-bin/config.cgi
www/network_config/cgi-bin/lib_net_iface.sh
www/network_config/cgi-bin/status.cgi"

# shellcheck disable=SC2206  # deliberate glob expansion into the file list
CODE_FILES=(install.sh etc/*.sh etc/*.conf scripts/*.sh www/network_config/cgi-bin/*)
found=$(grep -lE '\bend[01]\b' "${CODE_FILES[@]}" 2>/dev/null | tr '\\' '/' | sort -u)

missing=$(comm -23 <(printf '%s\n' "$LEDGER" | sort -u) <(printf '%s\n' "$found"))
extra=$(comm -13 <(printf '%s\n' "$LEDGER" | sort -u) <(printf '%s\n' "$found"))

if [ -n "$extra" ]; then
    fail "new code hardcodes end0/end1 outside the compat layer:"
    printf '        %s\n' $extra
fi
if [ -n "$missing" ]; then
    fail "ledger entry no longer references end0/end1 (stale — remove it):"
    printf '        %s\n' $missing
fi
[ -z "$extra" ] && [ -z "$missing" ] && pass "legacy-name ledger matches ($(printf '%s\n' "$found" | grep -c .) files)"

# ── 2. The installer installs AND enables the unit ────────────────────────
if grep -q 'install -m 644 "\$ETC_DIR/systemd/sa02m-iface-canonical.service"' scripts/02-network.sh \
   && grep -q 'install -m 755 "\$ETC_DIR/sa02m-iface-canonical.sh"' scripts/02-network.sh; then
    pass "02-network.sh installs the unit and its script"
else
    fail "02-network.sh no longer installs sa02m-iface-canonical.{sh,service}"
fi
if grep -q 'systemctl enable sa02m-iface-canonical' scripts/02-network.sh; then
    pass "02-network.sh enables the unit"
else
    fail "02-network.sh no longer enables sa02m-iface-canonical"
fi

# ── 3. F6 regression guard on the conf write path ─────────────────────────
# Comment lines are excluded: the contract's own rationale names both `echo -e`
# and `sudo tee` in prose, and a doc mention must not trip a code gate.
apply_code=$(grep -vE '^[[:space:]]*#' www/network_config/cgi-bin/apply.cgi)
# Here-string, not `printf … | grep -q`: grep -q exits on the first match and
# SIGPIPEs the producer, which under `set -o pipefail` poisons the pipeline
# status (141) — a `fail-if-present` check then MISSES the defect it exists for.
# A here-string has no producer process (single-command pipeline). See
# no-retired-session-token.sh and .ai-dev/notes/quality-gate-environment.md.
if grep -q 'echo -e' <<<"$apply_code"; then
    fail "apply.cgi reintroduced 'echo -e' — it interprets backslash escapes and mangles preserved foreign lines (F6)"
else
    pass "apply.cgi carries no 'echo -e'"
fi

# Raw `sudo rm` in apply.cgi was ALWAYS a silent no-op (www-data sudoers has no
# rm) and its return would bypass the pinned helper — contract §5.4.
if grep -q 'sudo rm' <<<"$apply_code"; then
    fail "apply.cgi reintroduced raw 'sudo rm' — deletion goes only through sa02m-conf-rm.sh (contract §5.4)"
else
    pass "apply.cgi carries no raw 'sudo rm'"
fi
# The sudoers GRANT line sits alone on its continuation line (leading spaces,
# bare path, EOL) — the installer's `install -m 755 … sa02m-conf-rm.sh` line
# does NOT match this shape, so removing only the grant fails the gate
# (mutation-verified in the 1.0.5.54 round-2 review).
if grep -q '^[[:space:]]*/usr/local/sbin/sa02m-conf-rm\.sh$' scripts/03-webserver.sh; then
    pass "sudoers grant line for the pinned conf-rm helper present"
else
    fail "sudoers grant line for sa02m-conf-rm.sh missing from scripts/03-webserver.sh"
fi
tee_lines=$(printf '%s\n' "$apply_code" | grep -c 'sudo tee')
if [ "$tee_lines" = "2" ]; then
    pass "apply.cgi writes confs through exactly the two known writers (write_iface_conf, conf_backup)"
else
    fail "apply.cgi has $tee_lines 'sudo tee' sites, expected 2 (write_iface_conf + conf_backup). A third writer bypasses the preservation merge — route it through write_iface_conf or update this ledger."
fi

# ── 4. systemd ordering + the per-device initialized gate (contract §1.0/§7) ──
UNIT=etc/systemd/sa02m-iface-canonical.service
if grep -qE '^Before=.*networking\.service' "$UNIT"; then
    pass "unit orders itself Before=networking.service"
else
    fail "sa02m-iface-canonical.service lost Before=networking.service — ifup would run against a stale name (the 1.0.3.38 symptom)"
fi

# With DefaultDependencies=no nothing else guarantees udevd or the coldplug
# trigger; losing either brings back the boot-0 pre-udevd no-op ("Cannot find
# device" against an unconfigured end0 — board unreachable on an unattended boot).
if grep -qE '^After=.*systemd-udevd\.service' "$UNIT" \
   && grep -qE '^After=.*systemd-udev-trigger\.service' "$UNIT"; then
    pass "unit is ordered After= systemd-udevd + systemd-udev-trigger"
else
    fail "sa02m-iface-canonical.service lost After= on systemd-udevd/systemd-udev-trigger — with DefaultDependencies=no it can run before udevd, see a kernel-native ethN and no-op (the boot-0 class)"
fi
if grep -qE '^Wants=.*systemd-udevd\.service' "$UNIT" \
   && grep -qE '^Wants=.*systemd-udev-trigger\.service' "$UNIT"; then
    pass "unit Wants= systemd-udevd + systemd-udev-trigger"
else
    fail "sa02m-iface-canonical.service lost Wants= on systemd-udevd/systemd-udev-trigger — with DefaultDependencies=no nothing else pulls them in (the boot-0 class)"
fi

# Whole-queue settle must stay dropped: it burned 120 s on unrelated queue
# churn and its timeout still released networking into the rename race (boot -1).
if grep -qE '^Wants=.*systemd-udev-settle' "$UNIT"; then
    fail "sa02m-iface-canonical.service re-grew Wants=systemd-udev-settle — whole-queue settle stalls up to 120 s on queue churn; the per-device initialized gate replaces it"
else
    pass "unit does not Want systemd-udev-settle"
fi

# The mid-boot rename's udev add event starts fix-eth@eth0 exactly while
# networking's `ifup -a` configures eth0 -> duplicate `ip addr add` ->
# "RTNETLINK answers: File exists" -> networking FAILED (8D boot -1).
if grep -qE '^After=.*networking\.service' etc/fix-eth@.service; then
    pass "fix-eth@.service is ordered After=networking.service"
else
    fail "fix-eth@.service lost After=networking.service — the double-ifup 'File exists' race against networking's ifup -a returns"
fi

# The rename script's per-device gate: a bare name-existence check cannot tell
# a settled canonical name from a kernel-native one udev is about to rename.
# canon_body captured first so the grep -q below is a single-command pipeline —
# `sed … | grep -q` would SIGPIPE sed on the (healthy) match and pipefail would
# read the FOUND gate as absent, failing a correct file (same class as above).
canon_body=$(sed -n '/^canonicalize_pair() {/,/^}/p' etc/sa02m-iface-canonical.sh)
if grep -q '^udev_initialized() {' etc/sa02m-iface-canonical.sh \
   && grep -q 'udev_initialized ' <<<"$canon_body"; then
    pass "rename script gates canonicalize_pair on udev_initialized"
else
    fail "etc/sa02m-iface-canonical.sh lost the udev_initialized gate in canonicalize_pair — the boot-0 'already canonical' no-op against a kernel-native name returns silently"
fi

# ── 5. interfaces.d source filter — backups must stay inert ────────────────
# 02-network.sh writes /etc/network/interfaces; a bare `*` source glob makes
# the <conf>.sa02m-bak backups (contract §3/§5.4) LIVE config — a backed-up
# duplicate `auto eth0` stanza gets ifup'd twice -> the second `ip addr add`
# -> "RTNETLINK: File exists" -> networking FAILED (bench 2026-07-29,
# reboot #1 — contract §1.0 mechanism 3).
if grep -q '^source /etc/network/interfaces\.d/\*\.conf$' scripts/02-network.sh \
   && ! grep -qE '^source /etc/network/interfaces\.d/\*[[:space:]]*$' scripts/02-network.sh; then
    pass "02-network.sh sources interfaces.d with the .conf filter (backups stay inert)"
else
    fail "scripts/02-network.sh must write 'source /etc/network/interfaces.d/*.conf' (and no unfiltered '*' line) — a wider glob sources <conf>.sa02m-bak backups as live config: duplicate stanza -> 'File exists' -> networking FAILED"
fi

# ── 6. Mechanism 4: event-driven canonicalization retry (contract §1.0/§7.5) ──
RULE=etc/98-sa02m-iface-canonical.rules
RETRY_UNIT=etc/systemd/system/sa02m-iface-canonical-retry.service
if [ -f "$RULE" ] \
   && grep -qE 'ACTION=="add\|move".*KERNEL=="end\*".*--no-block start sa02m-iface-canonical-retry\.service' "$RULE"; then
    pass "98-sa02m-iface-canonical.rules retriggers the retry unit on end* add|move with --no-block"
else
    fail "etc/98-sa02m-iface-canonical.rules missing or lost the end* add|move --no-block retry line — the §1.0 mechanism-4 ms race (db entry before rename) reopens"
fi
if [ -f "$RETRY_UNIT" ]; then
    if grep -qE '^Before=.*networking\.service' "$RETRY_UNIT"; then
        pass "retry unit orders Before=networking.service"
    else
        fail "sa02m-iface-canonical-retry.service lost Before=networking.service — the free pre-networking ordering insurance is gone"
    fi
    if grep -qE '^\[Install\]|^WantedBy=' "$RETRY_UNIT"; then
        fail "sa02m-iface-canonical-retry.service grew an [Install]/WantedBy — it is event-driven only, never boot-scheduled"
    else
        pass "retry unit has no [Install]/WantedBy (event-driven only)"
    fi
else
    fail "$RETRY_UNIT missing (mechanism-4 retry unit)"
fi
# 99-lan-recovery must stay end*-free: an end* line there would start
# fix-eth@end0 against a legacy name — the reason the retry rule is a
# separate file.
if grep -qE 'KERNEL=="end' etc/99-lan-recovery.rules; then
    fail "99-lan-recovery.rules grew an end* line — it would start fix-eth@end0 against a legacy name; end* handling lives only in 98-sa02m-iface-canonical.rules"
else
    pass "99-lan-recovery.rules carries no end* lines"
fi
# fix-eth.sh: an exhausted per-boot link-cycle budget must re-arm on
# carrier-up (bench 2026-07-29/30 night: eth1 logged "cycles=5, пропуск" for
# hours and recovery never resumed until reboot).
if grep -q 'link_cycle_count' etc/fix-eth.sh \
   && grep -qE 'rm -f "\$\{LOCK_DIR\}/\$\{iface\}\.link_cycle_count"' etc/fix-eth.sh; then
    pass "fix-eth.sh re-arms the link-cycle budget on carrier-up (counter reset present)"
else
    fail "etc/fix-eth.sh lost the link_cycle_count reset — an exhausted budget then blocks link recovery until reboot"
fi
if grep -q 'install -m 644 "\$ETC_DIR/98-sa02m-iface-canonical.rules"' scripts/02-network.sh \
   && grep -q 'install -m 644 "\$ETC_DIR/systemd/system/sa02m-iface-canonical-retry.service"' scripts/02-network.sh; then
    pass "02-network.sh installs the mechanism-4 rule + retry unit"
else
    fail "scripts/02-network.sh no longer installs 98-sa02m-iface-canonical.rules / sa02m-iface-canonical-retry.service"
fi

[ "$fails" = 0 ] || printf 'iface-naming-contract: %s check(s) failed — see docs/contracts/ethernet-iface-naming.md\n' "$fails"
exit "$fails"
