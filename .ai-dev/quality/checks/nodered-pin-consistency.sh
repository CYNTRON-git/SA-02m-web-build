#!/bin/bash
# Static gate for the Node-RED pin. Four homes have to agree or a device gets a
# different Node-RED than the one that was built and verified:
#
#   etc/sa02m-web-service-ctl.sh          runtime install (the panel button)
#   scripts/07-nodered.sh                 install-time path
#   scripts/dev/build-nodered-payload.sh  the payload build recipe
#   vendor-src/nodered/package{,-lock}.json  the tree npm ci actually installs
#   docs/vendor-integrations.md           the documented payload shape
#
# Pins (non-vacuous: a missing file, an empty extraction, or a sweep that stops
# matching FAILS — it never passes on zero matches):
#   1. one and only one node-red version string across all five sources.
#   2. --node22 in BOTH install homes, and no surviving --node20.
#   3. no surviving `node-red@3` anywhere under etc/ scripts/ tools/ — the EOL
#      pin this change exists to remove.
#   4. --omit=optional in the build recipe: dropping it silently reintroduces
#      the native @node-rs/bcrypt dep, which re-binds the payload to one Node
#      ABI and puts a compiler back on the target's prerequisites.
#   5. the repo-owned unit exists and resolves node EXPLICITLY (never `env
#      node`): a board can carry two Node installs and PATH order decides.
#
# Run: bash .ai-dev/quality/checks/nodered-pin-consistency.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# Presence pins read COMMENT-STRIPPED text (lib_check.sh) wherever a leading `#`
# would leave the needle behind. Pins anchored at line start (`^ExecStart=`,
# `^User=`, `^Environment=`, the `^NODERED_PIN_VERSION=` extractions) are
# comment-safe as written and are left alone.
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/lib_check.sh" || { echo "nodered-pin-consistency: cannot source lib_check.sh"; exit 1; }

fails=0
fail() { printf 'nodered-pin-consistency: FAIL  %s\n' "$*"; fails=$((fails + 1)); }
pass() { printf 'nodered-pin-consistency: ok    %s\n' "$*"; }

CTL=etc/sa02m-web-service-ctl.sh
S07=scripts/07-nodered.sh
BUILD=scripts/dev/build-nodered-payload.sh
PKG=vendor-src/nodered/package.json
LOCK=vendor-src/nodered/package-lock.json
DOC=docs/vendor-integrations.md
UNIT=etc/systemd/system/nodered.service

missing=0
for f in "$CTL" "$S07" "$BUILD" "$PKG" "$LOCK" "$DOC" "$UNIT"; do
    if [ ! -f "$f" ]; then
        fail "$f missing — the pin has lost one of its homes"
        missing=1
    fi
done

# ── 1. One node-red version across every home ──────────────────────────────
if [ "$missing" -eq 0 ]; then
    v_ctl=$(sed -n 's/^NODERED_PIN_VERSION=\([0-9][0-9.]*\)$/\1/p' "$CTL" | head -n1)
    v_07=$(sed -n 's/^NODERED_PIN_VERSION="\([0-9][0-9.]*\)"$/\1/p' "$S07" | head -n1)
    v_build=$(sed -n 's/^NODERED_PIN_VERSION="\([0-9][0-9.]*\)"$/\1/p' "$BUILD" | head -n1)
    v_pkg=$(sed -n 's/.*"node-red"[[:space:]]*:[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' "$PKG" | head -n1)
    # The lock's own record of what `npm ci` will install — the tree is the
    # ground truth; a hand-edited constant that disagrees with it is the drift.
    v_lock=$(sed -n '/"node_modules\/node-red"/,/}/p' "$LOCK" \
        | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' | head -n1)
    # Every node-red x.y.z spelled in the doc must be the same one.
    v_doc=$(grep -oE 'node-red[[:space:]@_-]*[0-9]+\.[0-9]+\.[0-9]+' "$DOC" \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | sort -u)

    empty=0
    for pair in "ctl:${v_ctl}" "07:${v_07}" "build:${v_build}" "package.json:${v_pkg}" "package-lock.json:${v_lock}" "doc:${v_doc}"; do
        if [ -z "${pair#*:}" ]; then
            fail "no node-red version extracted from ${pair%%:*} — the file moved or the pattern drifted; fix this gate, do not delete the pin"
            empty=1
        fi
    done

    doc_count=$(printf '%s\n' "$v_doc" | grep -c '[0-9]')
    if [ "$empty" -eq 0 ] && [ "$doc_count" -ne 1 ]; then
        fail "$DOC names $doc_count different node-red versions ($(printf '%s' "$v_doc" | tr '\n' ' ')) — one pin, one number"
    elif [ "$empty" -eq 0 ]; then
        if [ "$v_ctl" = "$v_07" ] && [ "$v_ctl" = "$v_build" ] && [ "$v_ctl" = "$v_pkg" ] \
           && [ "$v_ctl" = "$v_lock" ] && [ "$v_ctl" = "$v_doc" ]; then
            pass "node-red pin $v_ctl agrees across ctl / 07 / build recipe / package.json / package-lock.json / doc"
        else
            fail "node-red pin disagrees: ctl=$v_ctl 07=$v_07 build=$v_build package.json=$v_pkg lock=$v_lock doc=$v_doc"
        fi
    fi
fi

# ── 2. --node22 in both install homes, no surviving --node20 ───────────────
if [ -f "$CTL" ] && [ -f "$S07" ]; then
    if stripped_has "$CTL" '--node22' && stripped_has "$S07" '--node22'; then
        pass "--node22 present in both install homes"
    else
        fail "--node22 missing from $CTL and/or $S07 — the Node major is part of the pin"
    fi
fi

# ── 3./2b. Sweep etc/ scripts/ tools/ for the EOL pins ─────────────────────
# Non-vacuous: the sweep must actually be looking at files.
sweep_count=$(find etc scripts tools -type f \( -name '*.sh' -o -name '*.service' -o -name '*.cgi' \) 2>/dev/null | wc -l)
if [ "$sweep_count" -lt 20 ]; then
    fail "the etc/ scripts/ tools/ sweep found only $sweep_count files — the tree moved and this gate stopped checking anything"
else
    hits=$(grep -rn -- 'node-red@3\|--node20' etc scripts tools \
        --include='*.sh' --include='*.service' --include='*.cgi' 2>/dev/null)
    if [ -n "$hits" ]; then
        fail "EOL pin survives in $sweep_count swept files:"
        printf '%s\n' "$hits" | sed 's/^/    /'
    else
        pass "no node-red@3 / --node20 pin under etc/ scripts/ tools/ ($sweep_count files swept)"
    fi
fi

# ── 4. --omit=optional in the build recipe ─────────────────────────────────
if [ -f "$BUILD" ]; then
    if stripped_has "$BUILD" '--omit=optional'; then
        pass "build recipe keeps --omit=optional (pure-JS tree, no Node-ABI binding)"
    else
        fail "$BUILD lost --omit=optional — the payload would carry native code and bind to one Node major"
    fi
    if grep -qE '^[^#]*npm ci' "$BUILD"; then
        pass "build recipe installs with npm ci (the committed lockfile, not a re-resolve)"
    else
        fail "$BUILD no longer uses npm ci — an npm install re-resolves ranges and the payload stops being reproducible"
    fi
fi

# ── 6. The Node floor mirrors the lockfile's engines.node ──────────────────
# The ctl carries the floor as two constants (no JSON parser in POSIX sh, and
# the lockfile never reaches the device). This is what keeps them honest: a
# lockfile bump that raises engines.node — node-red 5 wants >=22.9 — fails here
# instead of leaving a board installing a runtime that cannot boot.
if [ -f "$CTL" ] && [ -f "$LOCK" ]; then
    eng=$(sed -n '/"node_modules\/node-red"/,/^    }/p' "$LOCK" \
        | sed -n 's/.*"node"[[:space:]]*:[[:space:]]*">=\([0-9][0-9.]*\)".*/\1/p' | head -n1)
    floor_maj=$(sed -n 's/^NODERED_NODE_MIN_MAJOR=\([0-9][0-9]*\)$/\1/p' "$CTL" | head -n1)
    floor_min=$(sed -n 's/^NODERED_NODE_MIN_MINOR=\([0-9][0-9]*\)$/\1/p' "$CTL" | head -n1)
    if [ -z "$eng" ]; then
        fail "could not read engines.node for node-red out of $LOCK — the lockfile shape or this pattern drifted; fix this gate, do not drop the floor"
    elif [ -z "$floor_maj" ] || [ -z "$floor_min" ]; then
        fail "could not read NODERED_NODE_MIN_MAJOR/MINOR out of $CTL (got maj='${floor_maj:-}' min='${floor_min:-}')"
    elif [ "$eng" != "${floor_maj}.${floor_min}" ]; then
        fail "the ctl Node floor ${floor_maj}.${floor_min} disagrees with the pinned node-red's engines.node >=$eng — a board would install a runtime its Node cannot run"
    else
        pass "Node floor ${floor_maj}.${floor_min} mirrors engines.node >=$eng from the lockfile"
    fi
fi

# ── 7. The flow verdict survives a non-English board ───────────────────────
# The install verdict is read out of the unit's journal, and node-red localises
# that log. Two independent things must hold, or a broken install reports
# success on a ru-locale board (which is what the bench actually runs):
#   a. our own unit pins the runtime locale;
#   b. BOTH readers match the ru spelling as well as the English one.
if [ -f "$UNIT" ]; then
    if grep -qE '^Environment=LC_ALL=' "$UNIT"; then
        pass "$UNIT pins the runtime locale (LC_ALL) so its journal language is deterministic"
    else
        fail "$UNIT no longer pins LC_ALL — node-red would log in the system locale and the flow verdict would stop matching"
    fi
fi
for reader in "$CTL" "$S07"; do
    [ -f "$reader" ] || continue
    # EXECUTABLE lines only. The ctl's own LANGUAGE note names «Запущены потоки»
    # in prose (the comment block above nodered_flows_healthy), so a whole-file
    # grep kept printing `ok` after the spelling had been deleted from the
    # matcher — the check passed on a comment while the verdict was English-only
    # again. scripts/07-nodered.sh spells its ru literal in code only, which is
    # why the same mutation was caught there and not here.
    code=$(grep -vE '^[[:space:]]*#' "$reader")
    # Here-strings, not `printf … | grep -q`: grep -q exits on the first match
    # and SIGPIPEs the producer; under `set -o pipefail` a FOUND marker then
    # reads back as absent (141). #103 grew $CTL and pushed the marker past the
    # pipe buffer, flaking THIS pass-if-present check RED on CI while it passed
    # under WSL — a race, not an env split. A here-string has no producer process.
    # (.ai-dev/notes/quality-gate-environment.md)
    if [ -z "$code" ]; then
        fail "$reader has no uncommented lines — emptied or unreadable; this gate cannot judge it"
    elif grep -q 'Started flows' <<<"$code" \
      && grep -q 'Запущены потоки' <<<"$code"; then
        pass "$reader matches the started-flows marker in both en and ru (uncommented lines only)"
    else
        fail "$reader lost one of the started-flows spellings (en 'Started flows' / ru 'Запущены потоки') from its executable code — on a ru board the verdict silently stops firing"
    fi
done
# The marker that never existed: 'Flows stopped due to missing node types' is an
# EDITOR-CLIENT notification string, never written to the runtime log. Matching
# it is a verdict that cannot fire in any locale.
if grep -rn 'Flows stopped due to missing node types' "$CTL" "$S07" 2>/dev/null; then
    fail "a reader matches 'Flows stopped due to missing node types' — that string is an editor-UI notification, never logged by the runtime, so the check is dead"
else
    pass "no reader depends on the editor-only 'Flows stopped…' string"
fi

# ── 5. The unit resolves node explicitly ───────────────────────────────────
if [ -f "$UNIT" ]; then
    if stripped_matches "$UNIT" '^ExecStart=' && stripped_has "$UNIT" '/usr/lib/node_modules/node-red/red.js'; then
        pass "$UNIT starts node-red from the global module tree"
    else
        fail "$UNIT has no ExecStart naming /usr/lib/node_modules/node-red/red.js"
    fi
    if grep -qE '^ExecStart=.*(env node|env node-red|/usr/bin/env)' "$UNIT"; then
        fail "$UNIT resolves node through env/PATH — with two Node installs on the board that makes 'which node runs' ambient"
    else
        pass "$UNIT pins the node interpreter by absolute path"
    fi
    if grep -q '^User=nodered' "$UNIT"; then
        pass "$UNIT runs as the nodered user"
    else
        fail "$UNIT does not run as User=nodered"
    fi
fi

if [ "$fails" -gt 0 ]; then
    printf 'nodered-pin-consistency: %d FAILED check(s)\n' "$fails"
    exit 1
fi
printf 'nodered-pin-consistency: all checks passed\n'
