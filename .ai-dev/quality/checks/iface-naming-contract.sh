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
if printf '%s\n' "$apply_code" | grep -q 'echo -e'; then
    fail "apply.cgi reintroduced 'echo -e' — it interprets backslash escapes and mangles preserved foreign lines (F6)"
else
    pass "apply.cgi carries no 'echo -e'"
fi
tee_lines=$(printf '%s\n' "$apply_code" | grep -c 'sudo tee')
if [ "$tee_lines" = "2" ]; then
    pass "apply.cgi writes confs through exactly the two known writers (write_iface_conf, conf_backup)"
else
    fail "apply.cgi has $tee_lines 'sudo tee' sites, expected 2 (write_iface_conf + conf_backup). A third writer bypasses the preservation merge — route it through write_iface_conf or update this ledger."
fi

# ── 4. The unit still orders itself ahead of ifupdown ─────────────────────
if grep -qE '^Before=.*networking\.service' etc/systemd/sa02m-iface-canonical.service; then
    pass "unit orders itself Before=networking.service"
else
    fail "sa02m-iface-canonical.service lost Before=networking.service — ifup would run against a stale name (the 1.0.3.38 symptom)"
fi

[ "$fails" = 0 ] || printf 'iface-naming-contract: %s check(s) failed — see docs/contracts/ethernet-iface-naming.md\n' "$fails"
exit "$fails"
