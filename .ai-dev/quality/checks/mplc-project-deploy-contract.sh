#!/bin/bash
# Static gate for docs/contracts/mplc-project-deploy.md.
#
# Guards the MPLC4 project-deploy surface against the regressions that would
# silently undo its security/safety invariants: auth/CSRF slipping after a
# mutation, the deploy target becoming request-derived (zip-slip), the sudoers
# grant widening, the load-verify success signal degrading to a bare start, or
# the reversible-deploy floors (backup-before-delete, restore-on-failure,
# retention cap, timeouts) disappearing.
#
# Run: bash .ai-dev/quality/checks/mplc-project-deploy-contract.sh
# Every needle is matched against COMMENT-STRIPPED text (lib_check.sh): a `#`
# in front of `web_csrf_require` in the CGI used to satisfy the CSRF pin while
# the endpoint that launches a root sudo helper had no CSRF at all (audit
# 2026-08-28, finding C3).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/lib_check.sh" || { echo "mplc-project-deploy-contract: cannot source lib_check.sh"; exit 1; }

CGI=www/network_config/cgi-bin/mplc_project_deploy.cgi
HELPER=etc/sa02m-mplc-project-deploy.sh
LIBPY=opt/sa02m-mplc/lib/project_zip.py
INSTALLER=scripts/03-webserver.sh

fails=0
fail() { printf 'mplc-project-deploy-contract: FAIL  %s\n' "$*"; fails=$((fails + 1)); }
pass() { printf 'mplc-project-deploy-contract: ok    %s\n' "$*"; }

for f in "$CGI" "$HELPER" "$LIBPY" "$INSTALLER"; do
    [ -f "$f" ] || fail "missing file: $f"
done
[ "$fails" = 0 ] || { printf 'mplc-project-deploy-contract: %s check(s) failed\n' "$fails"; exit "$fails"; }

# ── 1. CGI: auth + CSRF BEFORE any mutation ────────────────────────────────
if stripped_has "$CGI" 'web_session_check_cookie'; then
    pass "CGI checks the session cookie"
else
    fail "CGI lost web_session_check_cookie"
fi
# CSRF must be required, and the require must sit before the helper launch.
csrf_line=$(stripped_first_line "$CGI" 'web_csrf_require')
launch_line=$(stripped_first_line "$CGI" 'nohup sudo -n "\$HELPER"')
if [ -n "$csrf_line" ] && [ -n "$launch_line" ] && [ "$csrf_line" -lt "$launch_line" ]; then
    pass "CGI requires CSRF before launching the deploy helper"
else
    fail "CGI must call web_csrf_require before the sudo helper launch (mutation)"
fi

# ── 2. The deploy target is a hard-coded constant, never request-derived ───
if stripped_matches "$HELPER" '^MPLC_CFG_DIR=/opt/mplc4/server/cfg'; then
    pass "helper hard-codes MPLC_CFG_DIR=/opt/mplc4/server/cfg"
else
    fail "helper must hard-code MPLC_CFG_DIR=/opt/mplc4/server/cfg (zip-slip floor)"
fi
# The service id must be a literal, not interpolated from an argument.
if stripped_matches "$HELPER" '^SVC_ID=mplc4'; then
    pass "helper pins the service id to the literal mplc4"
else
    fail "helper must pin SVC_ID=mplc4 (no request value in a systemctl/service word)"
fi

# ── 3. Zip handling is in Python, and the allow-list is closed to 4 members ─
if stripped_has "$LIBPY" 'import zipfile' && stripped_has "$LIBPY" 'ProjectZipError'; then
    pass "zip handling is in Python (zipfile), fail-closed"
else
    fail "$LIBPY must handle the zip in Python zipfile with fail-closed errors"
fi
allow_count=$(stripped_count "$LIBPY" '^\s*"cfg/(config\.bin|ProjInfo\.json|VMInfo\.json|_files\.xml)":')
if [ "$allow_count" = "4" ]; then
    pass "ALLOW-list is the exact 4 cfg/ members"
else
    fail "the ALLOWED map must be exactly the 4 cfg/ members (found $allow_count)"
fi
if stripped_has "$LIBPY" 'E_TRAVERSAL' && stripped_has "$LIBPY" '_S_IFLNK'; then
    pass "traversal + symlink rejection present"
else
    fail "$LIBPY must reject traversal and symlink members"
fi

# ── 4. Sudoers: ONE pinned grant for the helper, no unit wildcard ──────────
# The grant sits on its own continuation line: leading spaces, the pinned path,
# a single trailing `*`, then either EOL or a `, \` continuation. No broader
# wildcard on any unit name (the helper hard-codes mplc4). Audit B1 moved the
# grant from the 03-webserver heredoc into the single committed drop-in.
SUDOERS=etc/sudoers.d/sa02m-www
if stripped_matches "$SUDOERS" '^[[:space:]]*/usr/local/sbin/sa02m-mplc-project-deploy\.sh \*(,[[:space:]]*\\)?[[:space:]]*$'; then
    pass "sudoers grants exactly the pinned helper path"
else
    fail "the committed sudoers file must grant /usr/local/sbin/sa02m-mplc-project-deploy.sh * (one pinned line)"
fi
# The installer must install the helper and the Python module.
if stripped_has "$INSTALLER" 'install -m 755 "$SCRIPT_DIR/../etc/sa02m-mplc-project-deploy.sh"' \
   && stripped_has "$INSTALLER" 'opt/sa02m-mplc/lib'; then
    pass "installer installs the helper + the Python module"
else
    fail "installer must install the helper and /opt/sa02m-mplc"
fi

# ── 5. Reversible-deploy floors in the helper ──────────────────────────────
# Backup before delete: the tar backup site must precede the rm of the cfg dir.
backup_line=$(stripped_first_line "$HELPER" 'tar czf "\$BACKUP_FILE"')
delete_line=$(stripped_first_line "$HELPER" '^rm -rf "\$MPLC_CFG_DIR"')
if [ -n "$backup_line" ] && [ -n "$delete_line" ] && [ "$backup_line" -lt "$delete_line" ]; then
    pass "helper backs up before deleting the live project"
else
    fail "helper must back up (tar czf) before the rm -rf of MPLC_CFG_DIR (fail-closed delete)"
fi
if stripped_matches "$HELPER" '^restore\(\) \{' && stripped_matches "$HELPER" 'restore$'; then
    pass "helper defines and calls restore() on the failure path"
else
    fail "helper must restore() on any replace/start/verify failure"
fi
# Load-verify is the success signal — success only on 'load successful'.
if stripped_has "$HELPER" '"load successful"' && stripped_has "$HELPER" 'write_status done success'; then
    pass "helper reports success only on the RT 'load successful' signal"
else
    fail "helper must gate success on 'load successful' (no false green on a bare start)"
fi
# LoadConfig error must be treated as a failure, not ignored.
if stripped_has "$HELPER" 'LoadConfig() error' && stripped_has "$HELPER" 'E_VERIFY'; then
    pass "helper treats LoadConfig() error / default-config fallback as failure"
else
    fail "helper must treat LoadConfig() error as an E_VERIFY failure"
fi
# Retention cap present.
if stripped_matches "$HELPER" '^BACKUP_KEEP=[0-9]+' && stripped_has "$HELPER" 'prune_backups'; then
    pass "helper enforces a backup retention cap"
else
    fail "helper must cap kept backups (BACKUP_KEEP + prune_backups)"
fi
# Timeouts on the hangable calls.
if stripped_has "$HELPER" 'timeout "$STOP_TIMEOUT"' \
   && stripped_has "$HELPER" 'timeout "$START_TIMEOUT"' \
   && stripped_matches "$HELPER" '^VERIFY_TIMEOUT=[0-9]+'; then
    pass "helper bounds stop/start/verify with timeouts"
else
    fail "helper must bound stop, start, and verify with timeouts"
fi
# Flasher port-lease refusal.
if stripped_has "$HELPER" 'flasher_busy'; then
    pass "helper refuses while the flasher holds the RS-485 lease"
else
    fail "helper must refuse when the flasher holds the COM lease"
fi

# ── 6. GET license read is bounded, correctly mapped, and fails SAFE ───────
# The GET meta reads the log-free addin file first and the newest MPLC4 runtime
# log second, mapping PLCConnectionsLimit→points, SessionsLimit→clients,
# InstancesLimit→instances, LicNumber→lic_number; any read/parse error must
# degrade to unknown, never crash the GET.
if stripped_has "$CGI" '/run/sa02m-mplc-license.json'; then
    pass "CGI reads the log-free addin license file as the primary source"
else
    fail "CGI must read /run/sa02m-mplc-license.json (primary, log-free source)"
fi
if stripped_has "$CGI" '/var/log/mplc4/0'; then
    pass "CGI reads the MPLC4 runtime log dir as the license fallback"
else
    fail "CGI must read /var/log/mplc4/0 for the license field"
fi
if stripped_has "$CGI" 'read_license_file() or read_license_log()'; then
    pass "license source order is addin file first, runtime log second"
else
    fail "CGI must try the addin file before the log (read_license_file() or read_license_log())"
fi
# точки = fpPLCConnectionsLimit (SDK core/main_imp.h). Mapping точки←InstancesLimit
# shipped through 1.0.6.3 and is fixed here: it printed 1 instead of 100.
# Assert the ASSIGNMENT, not just the token: each limit regex must be followed
# by the variable it feeds, so swapping the two limits back would fail here.
_maps_to() {  # <limit-name> <variable>
    grep -A2 -E "$1=\(\\\\d\+\)" "$CGI" | grep -qE "^\s+$2 = int\(m\.group\(1\)\)"
}
if _maps_to PLCConnectionsLimit points && _maps_to SessionsLimit clients \
   && _maps_to InstancesLimit instances && _maps_to LicNumber lic_number; then
    pass "CGI maps PLCConnectionsLimit→points, SessionsLimit→clients, InstancesLimit→instances, LicNumber→lic_number"
else
    fail "CGI must map точки←PLCConnectionsLimit (NOT InstancesLimit), клиенты←SessionsLimit, экземпляры←InstancesLimit, номер←LicNumber"
fi
if stripped_has "$CGI" 'lic["instances"]' && stripped_has "$CGI" 'lic["lic_number"]'; then
    pass "CGI emits the additive lic_number + instances fields"
else
    fail "CGI must emit lic_number and instances alongside points/clients"
fi
# Not-activated demo state must be detected (numbers suppressed).
if stripped_has "$CGI" 'Not activated'; then
    pass "CGI detects the 'Not activated' demo state"
else
    fail "CGI must detect 'Not activated' (report not-activated, not demo numbers)"
fi
# Fail-safe: the unknown fallback + the bounded tail read must both be present.
if stripped_has "$CGI" '"unknown": True' && stripped_matches "$CGI" 'activated.*false.*unknown'; then
    pass "CGI license read fails safe to unknown (in-Python + shell fallback)"
else
    fail "CGI license read must fail safe to license:{activated:false,unknown:true}"
fi
if stripped_matches "$CGI" 'cap = [0-9]+' && stripped_has "$CGI" 'read > cap'; then
    pass "CGI bounds the license log read (byte cap on the streaming pass)"
else
    fail "CGI must bound the license log read (a byte cap, not an unbounded read)"
fi

[ "$fails" = 0 ] || printf 'mplc-project-deploy-contract: %s check(s) failed — see docs/contracts/mplc-project-deploy.md\n' "$fails"
exit "$fails"
