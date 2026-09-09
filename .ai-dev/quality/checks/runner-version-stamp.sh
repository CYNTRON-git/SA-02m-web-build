#!/usr/bin/env bash
# runner-version-stamp — "which release installed this runner" has ONE home
# ($STATEDIR/runner.version) and every site that installs the runner writes it.
#
# WHY THIS EXISTS. The update runner reports UPDATER_VERSION against a package's
# min_updater. Until 1.0.6.39 it was a literal every board reported forever;
# 1.0.6.39 derived it from the deployed www/network_config/VERSION, which
# OVER-reports on a www-only delivery without etc/ (VERSION moves, the runner
# does not — ship review 1.0.6.39, item 6). 1.0.6.40 adds the stamp. Three
# things must all hold or the stamp lies in the other direction:
#
#   1. BOTH install-site scripts stamp right after they install the binary
#      (scripts/03-webserver.sh, scripts/update-www-only.sh via
#      sa02m_stamp_runner_version in scripts/lib.sh). A site that forgets
#      leaves the OLD stamp behind a NEW binary — the board then UNDER-reports,
#      and a raised MIN_UPDATER E_COMPAT-rejects a board that could apply.
#   2. The runner's own apply stamps when its manifest deploys the runner
#      binary (OTA / offline pack — the paths that install the runner without
#      either script). Same under-report otherwise.
#   3. Both readers (runner, inspect preflight) read the stamp BEFORE VERSION —
#      the reverse order re-creates the over-report the stamp exists to close.
#
# METHOD. Static pins read through lib_check.sh so a commented-out call cannot
# satisfy one (the comment-blindness class, docs/agent-rules/quality-gate-rigor.md):
# order pins for (1) — the stamp call within 3 lines AFTER the runner install
# line in each script; presence pins for (2) and the helper; order pins for (3).
# Behavioural half: the shipped helper is awk-extracted from lib.sh and RUN in a
# sandbox (SA02M_UPDATE_STATEDIR) — CRLF + comment VERSION stamps 9.8.7.6 at
# mode 0644; a garbage VERSION returns 1 and leaves the previous stamp intact.
# The runner-side derivation (stamp → VERSION → floor, env override) and the
# journal ride of the apply-time stamp are proven by RUNNING the shipped scripts
# elsewhere — py-unit-update (test_validate_package.py) and update-deploy-skip
# (case 4b/5b) — and are NOT restated here.
#
# NON-VACUOUS: a missing file, a helper that vanished from lib.sh, an install
# line the order pin cannot find, or a sandbox run that writes nothing FAILS.
#
# Proven RED (1.0.6.40) by mutation:
#   * commenting out the stamp call in 03-webserver.sh     -> case 1 FAIL
#   * commenting out the stamp call in update-www-only.sh  -> case 2 FAIL
#   * commenting out the helper's `install -m 0644` line   -> case 7 FAIL
#   * swapping the two read_version_line calls in the runner -> case 4 FAIL
#   * swapping stamped/installed in the inspect expansion  -> case 5 FAIL
#   * commenting out the cmd_apply call to the stamp step  -> case 6 FAIL
#
# Run: bash .ai-dev/quality/checks/runner-version-stamp.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1

# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. "$ROOT/.ai-dev/quality/checks/lib_check.sh"

LIB=scripts/lib.sh
RUNNER=etc/sa02m-update-runner.sh
INSPECT=etc/sa02m-update-inspect.sh
HELPER=sa02m_stamp_runner_version
INSTALL_RE='install -m 755 "[^"]*sa02m-update-runner\.sh" /usr/local/libexec/sa02m-update-runner$'

fails=0
ok()  { printf 'runner-version-stamp: ok    %s\n' "$1"; }
bad() { printf 'runner-version-stamp: FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── non-vacuity: everything below reads must exist ──────────────────────────
for f in "$LIB" "$RUNNER" "$INSPECT" scripts/03-webserver.sh scripts/update-www-only.sh; do
    [ -f "$f" ] || { echo "runner-version-stamp: FAIL — missing $f"; exit 1; }
done

# ── cases 1-2: each install site stamps right after it installs the binary ──
case_site() {  # $1=script  $2=label
    local inst call
    inst=$(stripped_first_line "$1" "$INSTALL_RE")
    call=$(stripped_first_line "$1" "^[[:space:]]*$HELPER ")
    if [ -z "$inst" ]; then
        bad "$2: runner install line not found — the order pin has nothing to anchor on (script moved?)"
    elif [ -z "$call" ]; then
        bad "$2: never calls $HELPER — a board updated this way keeps the OLD stamp behind a NEW runner (under-report)"
    elif [ "$call" -gt "$inst" ] && [ $((call - inst)) -le 3 ]; then
        ok "$2 stamps within 3 lines after the runner install (lines $inst → $call)"
    else
        bad "$2: stamp call at line $call is not right after the runner install at line $inst — it may sit outside the [ -f ] guard"
    fi
}
case_site scripts/03-webserver.sh    "1 full install"
case_site scripts/update-www-only.sh "2 www-only refresh"

# ── case 3: the helper is real and targets the one home ─────────────────────
if stripped_has "$LIB" "$HELPER() {" && stripped_has "$LIB" '"$statedir/runner.version"'; then
    ok "3 lib.sh defines $HELPER writing \$STATEDIR/runner.version"
else
    bad "3 lib.sh lost $HELPER or it no longer targets runner.version"
fi

# ── cases 4-5: both readers read the stamp BEFORE the VERSION file ──────────
case_reader() {  # $1=script  $2=label  $3=stamp-read ERE  $4=version-read ERE
    local s v
    s=$(stripped_first_line "$1" "$3")
    v=$(stripped_first_line "$1" "$4")
    if [ -z "$s" ]; then
        bad "$2: no stamp read (RUNNER_VERSION_FILE) — reports VERSION alone, the over-report is back"
    elif [ -z "$v" ]; then
        bad "$2: VERSION read not found — the order pin has nothing to anchor on"
    elif [ "$s" -lt "$v" ] && stripped_has "$1" 'RUNNER_VERSION_FILE="$STATEDIR/runner.version"'; then
        ok "$2 reads the stamp before VERSION (lines $s < $v) from \$STATEDIR/runner.version"
    else
        bad "$2: stamp read (line $s) does not precede the VERSION read (line $v), or the stamp path moved"
    fi
}
case_reader "$RUNNER" "4 runner" 'read_version_line "\$RUNNER_VERSION_FILE"' 'read_version_line "\$VERSION_FILE"'
# The inspect preflight expresses the precedence in ONE parameter expansion, not
# in read order — pin that expansion literally (stamp inside VERSION inside floor).
if stripped_has "$INSPECT" 'UPDATER_VERSION="${SA02M_UPDATER_VERSION:-${stamped:-${installed:-$UPDATER_VERSION_FALLBACK}}}"' \
   && stripped_has "$INSPECT" 'stamped=$(read_version_line "$RUNNER_VERSION_FILE")' \
   && stripped_has "$INSPECT" 'RUNNER_VERSION_FILE="$STATEDIR/runner.version"'; then
    ok "5 inspect derives env → stamp → VERSION → floor from \$STATEDIR/runner.version"
else
    bad "5 inspect: the stamp → VERSION → floor expansion or the stamp read/path changed — the panel and the runner may disagree"
fi

# ── case 6: the runner's own apply stamps after it deploys itself ───────────
if stripped_has "$RUNNER" 'stamp_runner_version_after_deploy() {' \
   && stripped_has "$RUNNER" 'if ! stamp_runner_version_after_deploy "$txn"; then'; then
    ok "6 cmd_apply calls stamp_runner_version_after_deploy (OTA / offline pack self-deploy)"
else
    bad "6 the runner no longer stamps after deploying itself — an OTA'd board under-reports until the next full install"
fi

# ── case 7 (behavioural): run the shipped helper in a sandbox ───────────────
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
awk -v start="$HELPER() {" 'index($0,start)==1{f=1} f{print} f&&/^\}/{exit}' "$LIB" > "$T/helper.sh"
if ! grep -q "^$HELPER() {" "$T/helper.sh"; then
    bad "7 could not extract $HELPER from $LIB — fix this gate, do not delete it"
else
    log() { :; }
    # shellcheck disable=SC1090
    . "$T/helper.sh"
    export SA02M_UPDATE_STATEDIR="$T/state"
    printf '# comment\r\n9.8.7.6\r\n' > "$T/VERSION"
    if "$HELPER" "$T/VERSION" && [ "$(cat "$T/state/runner.version" 2>/dev/null)" = "9.8.7.6" ]; then
        ok "7a helper stamps the delivered VERSION (CRLF + comment tolerated)"
    else
        bad "7a helper did not stamp 9.8.7.6 (got '$(cat "$T/state/runner.version" 2>/dev/null)')"
    fi
    # Mode: only where the sandbox filesystem can represent POSIX modes (Git Bash
    # on Windows cannot — the deploy-skip harness probes the same way).
    printf 'x\n' > "$T/probe.src"; install -m 0755 "$T/probe.src" "$T/probe.dst" 2>/dev/null
    if [ "$(stat -c '%a' "$T/probe.dst" 2>/dev/null)" = "755" ]; then
        if [ "$(stat -c '%a' "$T/state/runner.version")" = "644" ]; then
            ok "7b stamp is 0644 (world-readable: the inspect preflight runs unprivileged)"
        else
            bad "7b stamp mode is $(stat -c '%a' "$T/state/runner.version"), not 0644"
        fi
    else
        echo "runner-version-stamp: skip  7b mode check (sandbox filesystem cannot represent POSIX modes)"
    fi
    printf 'garbage\n' > "$T/VERSION"
    if "$HELPER" "$T/VERSION"; then
        bad "7c helper accepted a VERSION with no version line"
    elif [ "$(cat "$T/state/runner.version" 2>/dev/null)" = "9.8.7.6" ]; then
        ok "7c garbage VERSION refused (rc 1), previous stamp intact"
    else
        bad "7c garbage VERSION clobbered the previous stamp"
    fi
    unset SA02M_UPDATE_STATEDIR
fi

if [ "$fails" -eq 0 ]; then
    echo "runner-version-stamp: ALL OK"
    exit 0
fi
echo "runner-version-stamp: $fails FAILURE(S)"
exit 1
