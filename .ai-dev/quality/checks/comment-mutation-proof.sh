#!/usr/bin/env bash
# comment-mutation-proof — the standing proof that the static gates cannot be
# defeated by commenting a line out.
#
# WHY THIS EXISTS AS A ROW, not as a transcript in a commit message. The
# 2026-08-28 audit found five gates that went RED on DELETING a pinned line and
# GREEN on prefixing that same line with `#`: the offline pack silently dropping
# the MPLC RT plugin, CSRF gone from an endpoint that launches a root helper,
# the ~17 s multi-user boot hold back, an operator-disabled unit rolling an OTA
# back. Each gate was fixed and each fix was proven by a hand-run mutation —
# and a hand-run mutation protects nothing the day after. This row re-runs the
# proof on every review beat, so a gate that REGROWS the blindness (a new pin
# written with a plain `grep -q`) fails here instead of in the next audit.
#
# METHOD. A pristine copy of HEAD is extracted into a temp dir, the CURRENT
# working tree is overlaid on it (the whole `.ai-dev/quality/` tree, plus every
# path git reports as changed or untracked — so the proof covers the gates and
# the sources AS THEY ARE BEING HANDED BACK, not as they were last committed;
# a gate a Builder wrote five minutes ago is exactly the one that has never
# been measured), and each case then:
#   1. runs its gate on the untouched copy               -> must be GREEN,
#   2. comments out every line carrying the pinned text  -> must go RED,
#   3. restores the file from the pristine copy.
# The real tree is NEVER mutated.
#
# THE COMMENT TOKEN FOLLOWS THE FILE, not the habit of shell. Commenting a
# JavaScript line out with `#` is a syntax error, not a disabled line, and an
# HTML attribute cannot be commented per-line at all — so a `#`-only mutation
# would "prove" a JS or markup gate RED for the wrong reason, or leave it
# untestable. `comment_token()` picks `#`, `//` or an `<!-- ... -->` wrap from
# the extension, so each case mutates the file the way a developer actually
# would.
#
# ADDING A GATE: one row in CASES below — `gate|file|literal text`. The gate id
# is a REGISTRY id (tools.json `id`), and the gate is run by that row's own `run`
# command — so a harness living in scripts/dev/ is as casable as one in
# .ai-dev/quality/checks/. (It used to resolve `<id>.sh|.mjs` under checks/
# only, which is why a scripts/dev/ harness could not be cased even when it
# pinned a commentable line — review Q1/Q6, 1.0.6.24.) A case whose text matches
# no line FAILS (non-vacuity): a pin that moved must be re-pinned here, never
# silently dropped.
#
# COVERAGE — WHY A NEW GATE CANNOT FORGET ITS CASE. Below the mutation loop,
# coverage_check() enumerates EVERY row of the registry (tools.json), whatever
# directory its check lives in, and FAILS unless each one EITHER carries a case
# in CASES above OR carries a recorded exemption. This is the mechanical half of
# the Operator's 2026-08-28 "prevent recurrence" directive: "register your case"
# was discipline; this makes it can't-forget
# (docs/agent-rules/quality-gate-rigor.md — the floors table and its "exemption
# is recorded in the gate's header" convention).
#
# WHERE AN EXEMPTION LIVES — two homes, decided by the row, never by choice:
#   * the row runs a check SCRIPT of ours (a path under .ai-dev/quality/ or
#     scripts/dev/) -> the marker goes in that script's own HEADER: a comment
#     line whose text, right after the `#`/`//`, is the hyphenated token
#     comment<->mutation<->proof<->exempt, a colon, and a NON-EMPTY single-line
#     reason. It must sit ABOVE the first executable line — a marker further
#     down is REFUSED, so "in the gate's header" is measured, not asserted
#     (review Q10) — and there must be exactly ONE of them: only the first is
#     read, so a second marker is invisible text and FAILS. One line, because a
#     reason that wraps would be reported truncated.
#   * the row runs no such script — an inline interpreter command (`bash -n`,
#     `compileall`, `unittest discover`) or a repo tool outside those two check
#     homes -> there is no gate header of ours to annotate, so the exemption is
#     the row's own `mutationExempt` field in tools.json. Putting that field on
#     a row that DOES have a script FAILS: the header is that row's only home.
# Either way the exemption dies with the thing it describes (the file, or the
# row) — a stale exemption for a deleted gate is structurally impossible, and
# there is no side list. An exemption that is ALSO cased FAILS (the exemption is
# stale), and an empty reason FAILS (non-vacuity, the way CONTRAST_WHITELIST
# catches a stale entry). This harness pins no source line of its own, so it
# exempts ITSELF with a recorded reason (marker line just below) — its
# completeness is floored by coverage_check()'s own non-vacuity and the n_cases
# check, not by a case.
#
# comment-mutation-proof-exempt: this IS the comment-out mutation harness — it pins no source line of its own; its completeness is floored by coverage_check() and the n_cases>=10 check below, not by a mutation case.
#
# `covers` IS WIDE ON PURPOSE. This row's cases pin lines in www/, etc/, opt/,
# scripts/, tools/imaging/ and install.sh, so an edit to any of those can move a
# pin and make a case vacuous — and `covers` must name what can BREAK the check,
# not only where the check lives (docs/agent-rules/quality-gate-rigor.md (c)).
# The cost is that a `--touched` review run almost always includes this row's
# ~3.5 min (29 mutations plus one green baseline per gate; it was ~60 s at 20
# cases). That is the fail-safe direction, and CI runs the full set regardless.
#
# NOT COVERED, and why: a gate whose pins are all fail-IF-PRESENT sweeps
# (no-retired-session-token, the negative halves of installer-svc-policy-gate)
# cannot be defeated by commenting a line out — a comment removes a needle such
# a gate wants ABSENT. Those gates need the opposite mutation (re-introducing
# the banned pattern), which each already documents in its own header.
#
# Run: bash .ai-dev/quality/checks/comment-mutation-proof.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

fails=0
ok()  { printf 'comment-mutation-proof: ok    %s\n' "$*"; }
bad() { printf 'comment-mutation-proof: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

# gate id | file to mutate | literal text whose line gets commented out
CASES='
mplc-ota-deploy-contract|scripts/offline-update-allowlist.txt|firmware/mplc4/mplc_cyntron.so
mplc-project-deploy-contract|www/network_config/cgi-bin/mplc_project_deploy.cgi|web_csrf_require
kernel-policy-contract|opt/sa02m-mqtt-opcua/sa02m-mqtt-opcua.py|sd_notify("READY=1")
kernel-policy-contract|scripts/01-system.sh|sa02m_svc_apply sa02m-kernel-service-guard.service infra
health-gate-operator-disabled|etc/sa02m-update-runner.sh|systemctl is-enabled "$u"
ota-deploy-mode-contract|etc/sa02m-update-runner.sh|mode = deploy_mode(rel, dst)
imaging-samefile-guard|tools/imaging/make-image.sh|-ef "$FINAL_IMG_KEEP"
installer-svc-policy-gate|install.sh|Установка завершена
iface-naming-contract|scripts/02-network.sh|install -m 755 "$ETC_DIR/sa02m-iface-canonical.sh"
nodered-pin-consistency|scripts/dev/build-nodered-payload.sh|--omit=optional
watchdog-cap|scripts/01-system.sh|install -m 644 "$ETC_REPO/systemd/sa02m-watchdog.conf"
telemetry-device-id-contract|opt/sa02m-modbus-mqtt/sa02m_telemetry.py|self._clear_legacy_retained()
telemetry-device-id-contract|opt/sa02m-modbus-mqtt/sa02m_telemetry.py|"HW not ready — %s command dropped"
sudoers-pin-contract|etc/sudoers.d/sa02m-www|/usr/local/sbin/sa02m-mplc-project-deploy.sh *
gateway-acl-contract|www/network_config/cgi-bin/gateway_config.cgi|norm_allow_from(name, pcfg, all_errors)
mqtt-set-contract|www/network_config/cgi-bin/mqtt_set.cgi|timeout 5 mosquitto_pub
mqtt-set-contract|www/network_config/cgi-bin/mqtt_set.cgi|web_csrf_validate
web-update-csrf-contract|www/network_config/cgi-bin/web_update_apply.cgi|web_csrf_validate
i18n-dict-contract|www/network_config/static/js/i18n.js|'"'"'Сеть'"'"': '"'"'Network'"'"',
html-id-contract|www/network_config/index.html|id="nav-gateway-sub"
rs485-roster-consumer|www/network_config/cgi-bin/status.cgi|modules_frag=
web-auth-behaviour|www/network_config/cgi-bin/login.cgi|web_login_check
version-consistency|www/network_config/static/js/app.js|const APP_VERSION
mqtt-install-secret|scripts/05-mqtt.sh|mosquitto_passwd
rtc-utc-convention|www/network_config/cgi-bin/lib_rtc.sh|--systohc --utc
nodered-ctl-install|etc/sa02m-web-service-ctl.sh|nodered_guard_major_upgrade || return 1
iface-dns-ensure|etc/fix-eth.sh|dns_ensure "$iface"
uboot-bootscr-format|tools/imaging/make-image.sh|run_firstboot_patch "$RAW_IMG" ||
storage-automount-decision|etc/storage-mount.sh|mount -t ntfs3 -o rw,noatime
storage-automount-decision|etc/storage-mount.sh|label_fits_exfat "${LABEL}" || return 1
'

command -v git >/dev/null 2>&1 || { echo "comment-mutation-proof: FAIL — git is required to build the pristine copy"; exit 1; }
command -v tar >/dev/null 2>&1 || { echo "comment-mutation-proof: FAIL — tar is required to build the pristine copy"; exit 1; }
command -v node >/dev/null 2>&1 || { echo "comment-mutation-proof: FAIL — node is required to read the registry (a gate is run by its registry row)"; exit 1; }

# ── The registry, read ONCE: it is both how a gate is run (its row's `run`) and
# what coverage enumerates. Emitted as one record per row, four fields:
#   id US our-check-script (or empty) US mutationExempt (or empty) US run
# The separator is US (\x1f), NOT a tab: a tab is IFS whitespace, so bash `read`
# collapses a run of them and an empty middle field silently shifts every field
# after it. "Our check script" is the first path in `run` under one of the two
# homes this project keeps checks in; a row naming none (an inline `bash -n` /
# compileall / unittest command, or a repo tool outside those homes) gets an
# empty field and is exempted in the row instead of a header.
TOOLS="$ROOT/.ai-dev/quality/tools.json"
[ -f "$TOOLS" ] || { echo "comment-mutation-proof: FAIL — $TOOLS not found; the gate set cannot be enumerated"; exit 1; }
REGISTRY_TSV=$(node -e '
    const fs = require("fs");
    const j = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
    const re = /(?:\.ai-dev\/quality\/|scripts\/dev\/)[A-Za-z0-9_.\/-]+\.(?:sh|mjs|py)/;
    const clean = (s) => String(s == null ? "" : s).replace(new RegExp("[\t\r\n\u001f]+", "g"), " ").trim();
    for (const t of (j.tools || [])) {
        const id = clean(t.id);
        if (!id) continue;
        const run = clean(t.run);
        const m = run.match(re);
        console.log([id, m ? m[0] : "", clean(t.mutationExempt), run].join(String.fromCharCode(31)));
    }
' "$TOOLS") || { echo "comment-mutation-proof: FAIL — could not parse $TOOLS"; exit 1; }

declare -A ROW_RUN=() ROW_SCRIPT=() ROW_EXEMPT=()
ROW_IDS=()
while IFS=$'\x1f' read -r _id _script _exempt _run; do
    [ -n "${_id:-}" ] || continue
    ROW_IDS+=("$_id"); ROW_SCRIPT[$_id]=$_script; ROW_EXEMPT[$_id]=$_exempt; ROW_RUN[$_id]=$_run
done <<< "$REGISTRY_TSV"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
TREE="$TMP/tree"
PRISTINE="$TMP/pristine"
mkdir -p "$TREE" "$PRISTINE"

( cd "$ROOT" && git archive HEAD ) | tar -x -C "$TREE" 2>/dev/null || {
    echo "comment-mutation-proof: FAIL — could not extract a pristine copy of HEAD"; exit 1; }
n_extracted=$(find "$TREE" -type f | wc -l)
if [ "$n_extracted" -lt 200 ]; then
    echo "comment-mutation-proof: FAIL — pristine copy holds only $n_extracted files; the extraction is broken, not the tree small"
    exit 1
fi

# The gates under test are the WORKING-TREE ones, not HEAD's: this row must
# prove the gate a Builder is handing back, before it is ever committed.
rm -rf "$TREE/.ai-dev/quality"
mkdir -p "$TREE/.ai-dev"
cp -r "$ROOT/.ai-dev/quality" "$TREE/.ai-dev/quality"

# ...and so must the SOURCES those gates read. A gate added together with the
# doc pointer or the harness it asserts on would otherwise be measured against
# HEAD's copy of them and fail its own green baseline for a reason that has
# nothing to do with the mutation. Overlay every path git reports as changed
# or untracked (deletions skipped — the pristine copy is already the "before").
while IFS= read -r -d '' rec; do
    st=${rec:0:2}
    p=${rec:3}
    case "$st" in
        R*|C*) IFS= read -r -d '' _old || true ;;   # rename/copy: consume the source path
    esac
    case "$st" in
        *D*) continue ;;
    esac
    [ -f "$ROOT/$p" ] || continue
    mkdir -p "$TREE/$(dirname "$p")"
    cp "$ROOT/$p" "$TREE/$p"
done < <(cd "$ROOT" && git status --porcelain -z -uall 2>/dev/null)

# Several gates sweep via `git ls-files`; without an index they would see one
# file and (correctly) fail their own non-vacuity floor, which would read here
# as a mutation success it is not. Give the copy a real index.
( cd "$TREE" && git init -q && git add -A ) >/dev/null 2>&1 || {
    echo "comment-mutation-proof: FAIL — could not init a git index in the pristine copy"; exit 1; }

# A gate is run by its REGISTRY ROW's own command — the same string
# `run.mjs` executes — so bash/node/python and any directory are covered
# alike, and the proof can never diverge from what the beat actually runs.
gate_cmd() {  # $1 = gate id ; prints the row's run command, empty when unknown
    printf '%s\n' "${ROW_RUN[$1]:-}"
}

run_gate() {  # $1 = gate id ; returns the gate's exit status
    local cmd="${ROW_RUN[$1]:-}"
    [ -n "$cmd" ] || return 2
    ( cd "$TREE" && bash -c "$cmd" ) >/dev/null 2>&1
}

# The token a developer would actually use to disable a line in THIS file type.
comment_token() {  # $1 = path ; prints `#`, `//`, or `html`
    case "$1" in
        *.js|*.mjs|*.cjs) printf '//\n' ;;
        *.html|*.htm)     printf 'html\n' ;;
        *)                printf '#\n' ;;
    esac
}

# One green baseline per gate, cached: a gate that is already RED would make
# every mutation below look successful.
declare -A baseline_done=()
green_baseline() {  # $1 = gate id
    local g="$1"
    [ -n "${baseline_done[$g]:-}" ] && return "${baseline_done[$g]}"
    if run_gate "$g"; then
        baseline_done[$g]=0
    else
        baseline_done[$g]=1
        bad "$g is not green on an unmutated tree — its mutation results below prove nothing"
    fi
    return "${baseline_done[$g]}"
}

while IFS='|' read -r gate file needle; do
    [ -n "$gate" ] || continue
    if [ -z "$(gate_cmd "$gate")" ]; then
        bad "$gate: no registry row with that id — the case table names a gate the beat does not run"
        continue
    fi
    if [ ! -f "$TREE/$file" ]; then
        bad "$gate: target file $file is absent from HEAD — the pin moved; re-point this case"
        continue
    fi
    green_baseline "$gate" || continue

    cp "$TREE/$file" "$PRISTINE/current"
    # Comment out every non-comment line carrying the pinned text. index() is a
    # literal match — the needles carry regex metacharacters ($ * " ( ) ).
    #
    # The token goes AFTER the indentation, not at column 0. That is how a
    # person actually comments a line out, and it is the harder mutation: a
    # column-0 `#` inside a Python method also terminates several gates' own
    # body extractors (an unindented line ends the block), which turns them RED
    # for a reason that has nothing to do with the pin — a proof that would pass
    # while the pin stayed blind.
    #
    # An HTML line is WRAPPED (`<!-- … -->`), the only per-line disable markup
    # has; a line already carrying either delimiter is skipped rather than
    # producing malformed nesting.
    tok=$(comment_token "$file")
    awk -v needle="$needle" -v tok="$tok" '
        tok == "html" {
            if (index($0, needle) > 0 && index($0, "<!--") == 0 && index($0, "-->") == 0) {
                sub(/^[[:space:]]*/, "&<!-- "); $0 = $0 " -->"; hits++
            }
            print; next
        }
        index($0, needle) > 0 && $0 !~ /^[[:space:]]*(#|\/\/)/ {
            sub(/^[[:space:]]*/, "&" tok); hits++
        }
        { print }
        END { exit (hits > 0 ? 0 : 3) }
    ' "$PRISTINE/current" > "$TMP/mutated"
    rc=$?
    if [ "$rc" -eq 3 ]; then
        bad "$gate: '$needle' matches no live line in $file — the pin moved or was already commented (non-vacuity)"
        continue
    elif [ "$rc" -ne 0 ]; then
        bad "$gate: mutation of $file failed (awk rc=$rc)"
        continue
    fi
    cp "$TMP/mutated" "$TREE/$file"

    if run_gate "$gate"; then
        bad "$gate stays GREEN with '$needle' commented out in $file — the gate is hollow"
    else
        ok "$gate goes RED when '$needle' is commented out in $file"
    fi
    cp "$PRISTINE/current" "$TREE/$file"
done <<< "$CASES"

n_cases=$(printf '%s\n' "$CASES" | grep -c '|')
if [ "$n_cases" -lt 10 ]; then
    bad "only $n_cases mutation case(s) — the table was gutted (expected >=10)"
fi

# ── COVERAGE: EVERY registry row is cased here, or carries a recorded
# exemption — in its check script's own header where the row runs one of ours,
# in the row itself where it does not. The mechanical half of the Operator's
# "prevent recurrence": a new check added without a mutation case can no longer
# slip in unmeasured, whatever directory it lives in. This section reads the
# REAL working tree (tools.json + each gate's header), not the pristine copy —
# it governs what is being handed back.
#
# The line number of a file's first EXECUTABLE line: everything above it is the
# header, and an exemption marker must live there. `/* … */` blocks count as
# header too, so a .mjs gate is measured the same way (review Q10).
header_end() {  # $1 = path ; prints the first executable line number, empty when none
    awk '
        inblk { if (index($0, "*/") > 0) inblk = 0; next }
        /^[[:space:]]*\/\*/ { rest = substr($0, index($0, "/*") + 2); if (index(rest, "*/") == 0) inblk = 1; next }
        /^[[:space:]]*(#|\/\/)/ { next }
        /^[[:space:]]*$/ { next }
        { print NR; exit }
    ' "$1"
}

coverage_check() {
    # Gate ids that carry >=1 mutation case (field 1 of CASES) — read from the
    # same table the loop above used, so there is no second enumeration.
    local cased
    cased=$(awk -F'|' 'NF && $1 != "" { print $1 }' <<<"$CASES" | sort -u)

    local n=0 n_scripts=0 id script path reason marker_line marker_no is_cased marker_present hdr row_exempt
    for id in "${ROW_IDS[@]}"; do
        n=$((n + 1))
        script="${ROW_SCRIPT[$id]:-}"
        row_exempt="${ROW_EXEMPT[$id]:-}"
        is_cased=0
        grep -qxF "$id" <<<"$cased" && is_cased=1

        marker_present=0; reason=""
        if [ -n "$script" ]; then
            n_scripts=$((n_scripts + 1))
            path="$ROOT/$script"
            if [ ! -f "$path" ]; then
                bad "coverage: registry row $id runs $script but the file is absent — stale registry row"
                continue
            fi
            # Exemption marker: a header comment line (`#` or `//`) whose text is
            # the hyphenated exempt token, a colon, then the reason. Only the
            # FIRST marker is read, and it must sit above the first executable
            # line — a marker at the bottom of the file is not a header note.
            marker_line=$(grep -nE '^[[:space:]]*(#+|//)[[:space:]]*comment-mutation-proof-exempt:' "$path")
            n_markers=$(grep -cE '^[[:space:]]*(#+|//)[[:space:]]*comment-mutation-proof-exempt:' "$path")
            if [ "$n_markers" -gt 1 ]; then
                bad "coverage: $id carries $n_markers exemption markers — only the first is read, so the others are invisible text; keep ONE, on ONE line"
                continue
            fi
            marker_line=${marker_line%%$'\n'*}
            if [ -n "$marker_line" ]; then
                marker_no=${marker_line%%:*}
                hdr=$(header_end "$path")
                if [ -n "$hdr" ] && [ "$marker_no" -ge "$hdr" ]; then
                    bad "coverage: $id's exemption marker sits at line $marker_no, below the first executable line ($hdr) — the exemption belongs in the gate's OWN HEADER where the next reader meets it"
                    continue
                fi
                marker_present=1
                reason=$(printf '%s\n' "$marker_line" | sed -E 's/^[0-9]+:[[:space:]]*(#+|\/\/)[[:space:]]*comment-mutation-proof-exempt:[[:space:]]*//')
            fi
            if [ -n "$row_exempt" ]; then
                bad "coverage: $id carries a 'mutationExempt' field in tools.json while it runs a check script of ours ($script) — the exemption's one home is that script's header; move it there"
                continue
            fi
        elif [ -n "$row_exempt" ]; then
            # No check script of ours to annotate (an inline interpreter command,
            # or a repo tool outside .ai-dev/quality/ and scripts/dev/), so the
            # row itself is the exemption's home.
            marker_present=1
            reason="[registry row] $row_exempt"
        fi

        if [ "$is_cased" -eq 1 ] && [ "$marker_present" -eq 1 ]; then
            bad "coverage: $id is BOTH cased (CASES) and exempt — the exemption is stale; remove it"
        elif [ "$is_cased" -eq 1 ]; then
            ok "coverage: $id has a mutation case"
        elif [ "$marker_present" -eq 1 ] && [ -n "$reason" ]; then
            ok "coverage: $id exempt — $reason"
        elif [ "$marker_present" -eq 1 ]; then
            bad "coverage: $id carries an exemption marker with an EMPTY reason — record why it is exempt"
        elif [ -n "$script" ]; then
            bad "coverage: $id is a registered check with NEITHER a mutation case in CASES NOR a 'comment-mutation-proof-exempt:' marker in $script's header — add one (docs/agent-rules/quality-gate-rigor.md)"
        else
            bad "coverage: $id is a registered check that runs no check script of ours, with NEITHER a mutation case in CASES NOR a non-empty 'mutationExempt' reason on its tools.json row — add one (docs/agent-rules/quality-gate-rigor.md)"
        fi
    done

    # Non-vacuity, both dimensions: the enumeration must have found the real row
    # set, and the path resolution must still be resolving scripts. A broken
    # parse or a regex that stopped matching would otherwise pass by checking
    # nothing.
    if [ "$n" -lt 40 ]; then
        bad "coverage: enumerated only $n registry row(s) — the registry read is broken, not the set small (expected >=40)"
    fi
    if [ "$n_scripts" -lt 15 ]; then
        bad "coverage: resolved a check script for only $n_scripts row(s) — the path resolution is broken (expected >=15)"
    fi
}
coverage_check

echo
if [ "$fails" -eq 0 ]; then
    echo "comment-mutation-proof: ALL OK — $n_cases comment-out mutation(s) each turned their gate RED"
    exit 0
fi
echo "comment-mutation-proof: $fails FAILURE(S)"
exit 1
