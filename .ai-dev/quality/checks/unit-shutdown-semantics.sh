#!/bin/bash
# Static gate: systemd unit text that silently does not do what it says.
#
# Defect this closes (bench 1.136, 2026-10-23, systemd 255): after a clean
# `systemctl reboot` every boot logged «REBOOT REASON: unexpected — no
# clean-shutdown marker». etc/sa02m-shutdown-marker.service had
# DefaultDependencies=no and no Conflicts=shutdown.target, so nothing stopped it
# at shutdown and its ExecStop — the only writer of the marker — never ran. The
# same line carried `%s`/`%Y`/`%m`/`%d`/`%H`/`%M`/`%S`, which systemd expands as
# specifiers at load (`systemctl show -p ExecStop` on the board printed
# `printf "halt at /usr/bin/bash" "$(date +/etc/systemd/system-<machine-id>-…)"`),
# so even a run ExecStop would have written garbage. Both defects pass
# `systemd-analyze verify`: the unit is valid, it just never fires.
#
# Pins (non-vacuous: a missing file, a sweep below its floor, or a rule that
# judged nothing FAILS):
#   A. Open-world over every unit file under SWEEP_ROOTS: a unit with
#      DefaultDependencies=no that carries ExecStop=/ExecStopPost= must name
#      shutdown.target in BOTH Conflicts= and Before= (systemd.special(7),
#      shutdown.target: the implicit pair is exactly what DefaultDependencies=no
#      drops). Accepted debt lives in KNOWN_OPEN with its reason; an entry that
#      no longer excuses anything (file gone, unit now compliant, or out of
#      scope) FAILS, so the ledger cannot outlive the debt.
#   B. Open-world over the same sweep: every `%` on an uncommented line is
#      `%%`, a sanctioned instance specifier (%i %I %n %N), or a whole-value
#      percentage (CPUQuota=25%). An allow-list of what is meant, not a denylist
#      of the specifiers somebody remembered — a date format is the classic way
#      in, and %s %Y %m %d %H %M %S are all valid specifiers, so systemd
#      accepts the line without a word.
#   C. The marker pair agrees: pre-start's SA02M_CLEAN_MARKER= is a plain
#      literal path (optionally double-quoted, nothing else on the line), and
#      the unit's ExecStop redirects (`>` or `> `) to exactly that path. The
#      match is anchored at both ends: the path must be followed by whitespace,
#      `&`, `;`, a quote or the end of the value, so `…-shutdown2`, `….tmp`, or
#      a pre-start path that is only a prefix of the unit's all FAIL. Pre-start
#      tests it with `[ -f "$SA02M_CLEAN_MARKER" ]`, the unit orders
#      After=local-fs.target (stop runs in reverse, so the write lands before
#      unmount), and the unit is not in KNOWN_OPEN.
#   D. Installer: scripts/01-system.sh lands the unit through
#      sa02m_atomic_install (web-code-rigor.md, live-path files) and applies it
#      with `sa02m_svc_apply … infra start`; under the installer homes no line
#      NAMING the unit lands it by a bare install/cp/mv or restarts it — a
#      restart runs ExecStop and leaves a marker that would report the next
#      crash as CLEAN. Known limit: a static per-line pin, so a restart through
#      a variable (`u=…marker.service; systemctl restart "$u"`) is not seen.
#
# Comment handling: systemd treats only WHOLE lines starting with `#` or `;` as
# comments (an inline ` # …` is part of the value), so unit text is stripped of
# whole-line comments only. Installer text uses lib_check's whole-line strip.
# Commenting a pinned line out therefore FAILS the gate (comment-mutation-proof).
#
# Run: bash .ai-dev/quality/checks/unit-shutdown-semantics.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/lib_check.sh" || { echo "unit-shutdown-semantics: cannot source lib_check.sh"; exit 1; }

SWEEP_ROOTS=(etc opt usr tools)
SWEEP_FLOOR=40          # 67 unit files on 2026-10-23; a dead find must not read as "all clean"
MARKER_UNIT=etc/sa02m-shutdown-marker.service
PRE_START=etc/sa02m-pre-start.sh
INSTALLER=scripts/01-system.sh
INSTALLER_HOMES=(install.sh scripts tools)

# Accepted debt for rule A: path|reason. Each entry must still excuse a live
# violation, or the run FAILS.
KNOWN_OPEN='
etc/systemd/sa02m-pre-start.service|ExecStop=sa02m-rtc-sync.sh (system time -> DS3231 at shutdown) never runs for the same reason; arming it adds an i2c-2 write to every shutdown on a bus that wedges, so the fix is its own change (.ai-dev/backlog.md, 2026-10-23)
'

fails=0
ok()  { printf 'unit-shutdown-semantics: ok    %s\n' "$*"; }
bad() { printf 'unit-shutdown-semantics: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

# Unit text with whole-line `#` and `;` comments blanked (line numbers kept).
unit_text() {  # $1=path
    [ -r "$1" ] || return 0
    sed -e 's/^[[:space:]]*[#;].*$//' < "$1"
}

# Space-joined values of every `Key=` line in TEXT.
unit_values() {  # $1=text  $2=key
    local out
    out=$(grep -E "^[[:space:]]*$2[[:space:]]*=" <<<"$1") || return 0
    sed -E "s/^[[:space:]]*$2[[:space:]]*=//" <<<"$out" | tr '\n' ' '
}

names_word() {  # $1=space-separated list  $2=word
    case " $1 " in *" $2 "*) return 0 ;; *) return 1 ;; esac
}

# True when TEXT redirects (`>` or `> `) to exactly PATH: the path must be
# followed by whitespace, `&`, `;`, a quote or the end of TEXT — a longer path
# (`PATH2`, `PATH.tmp`) is a different file.
writes_exact_path() {  # $1=text  $2=path
    local needle rest c
    for needle in "> $2" ">$2"; do
        rest=$1
        while [[ $rest == *"$needle"* ]]; do
            rest=${rest#*"$needle"}
            c=${rest:0:1}
            case "$c" in
                ''|' '|$'\t'|'&'|';'|"'"|'"') return 0 ;;
            esac
        done
    done
    return 1
}

known_open_reason() {  # $1=path -> prints reason, rc 0 when listed
    local line
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        if [ "${line%%|*}" = "$1" ]; then printf '%s' "${line#*|}"; return 0; fi
    done <<<"$KNOWN_OPEN"
    return 1
}

# ── Sweep ───────────────────────────────────────────────────────────────────
units=()
while IFS= read -r -d '' f; do
    units+=("${f#./}")
done < <(find "${SWEEP_ROOTS[@]}" -type f \( -name '*.service' -o -name '*.timer' \
            -o -name '*.socket' -o -name '*.path' -o -name '*.mount' -o -name '*.target' \) \
            -not -path '*/node_modules/*' -print0 2>/dev/null)
if [ "${#units[@]}" -lt "$SWEEP_FLOOR" ]; then
    bad "sweep found ${#units[@]} unit files under ${SWEEP_ROOTS[*]} (floor $SWEEP_FLOOR) — the sweep is dead, nothing was judged"
fi

# ── A: DefaultDependencies=no + ExecStop => Conflicts/Before=shutdown.target ─
a_scope=0
a_violators=" "
for f in "${units[@]}"; do
    case "$f" in *.service) ;; *) continue ;; esac
    text=$(unit_text "$f")
    text_matches "$text" '^[[:space:]]*DefaultDependencies[[:space:]]*=[[:space:]]*(no|false|off|0)[[:space:]]*$' || continue
    text_matches "$text" '^[[:space:]]*ExecStop(Post)?[[:space:]]*=' || continue
    a_scope=$((a_scope + 1))
    conf=$(unit_values "$text" Conflicts)
    bef=$(unit_values "$text" Before)
    missing=""
    names_word "$conf" shutdown.target || missing="Conflicts=shutdown.target"
    names_word "$bef" shutdown.target || missing="${missing:+$missing, }Before=shutdown.target"
    if [ -z "$missing" ]; then
        ok "A $f: stop runs at shutdown (DefaultDependencies=no + explicit shutdown.target pair)"
        continue
    fi
    a_violators="$a_violators$f "
    if reason=$(known_open_reason "$f"); then
        printf 'unit-shutdown-semantics: KNOWN %s: no %s — accepted debt: %s\n' "$f" "$missing" "$reason"
    else
        bad "A $f: DefaultDependencies=no with ExecStop but no $missing — nothing stops this unit at shutdown, so its ExecStop never runs"
    fi
done
[ "$a_scope" -ge 2 ] || bad "A judged $a_scope units (expected >= 2: the marker and pre-start) — the rule is vacuous"
while IFS= read -r line; do
    [ -n "$line" ] || continue
    p=${line%%|*}
    [ -n "${line#*|}" ] && [ "${line#*|}" != "$line" ] || bad "KNOWN_OPEN entry '$p' carries no reason"
    if [ ! -f "$p" ]; then
        bad "KNOWN_OPEN entry '$p': file is gone — drop the entry"
    elif ! names_word "$a_violators" "$p"; then
        bad "KNOWN_OPEN entry '$p' excuses nothing (compliant or out of scope) — drop the entry"
    fi
done <<<"$KNOWN_OPEN"

# ── B: every `%` is meant ───────────────────────────────────────────────────
b_lines=0
b_bad=0
for f in "${units[@]}"; do
    text=$(unit_text "$f")
    n=0
    while IFS= read -r l; do
        n=$((n + 1))
        case "$l" in *%*) ;; *) continue ;; esac
        b_lines=$((b_lines + 1))
        r=${l//%%/}
        r=$(sed -E -e 's/%[iInN]//g' -e 's/=[[:space:]]*[0-9]+(\.[0-9]+)?%[[:space:]]*$/=/' <<<"$r")
        case "$r" in
            *%*)
                bad "B $f:$n: unescaped % (a systemd specifier, expanded at unit load) — write %% for a literal: $l"
                b_bad=$((b_bad + 1)) ;;
        esac
    done <<<"$text"
done
[ "$b_lines" -ge 5 ] || bad "B judged $b_lines lines carrying % (expected >= 5: the instance units) — the rule is vacuous"
[ "$b_bad" -eq 0 ] && ok "B every % in $b_lines unit lines is %%, %i/%I/%n/%N, or a percentage value"

# ── C: the marker pair agrees ───────────────────────────────────────────────
if [ ! -f "$MARKER_UNIT" ] || [ ! -f "$PRE_START" ]; then
    bad "C $MARKER_UNIT or $PRE_START is missing"
else
    mtext=$(unit_text "$MARKER_UNIT")
    ptext=$(stripped_text "$PRE_START")
    aline=$(grep -E '^[[:space:]]*SA02M_CLEAN_MARKER=' <<<"$ptext") || aline=""
    aline=${aline%%$'\n'*}
    mpath=""
    # The whole value, exact: a plain absolute path, bare or double-quoted.
    # Two patterns, not one with a `\1` backreference: ERE has none, and the
    # MSYS regex engine rejects it where glibc accepts it.
    path_re_bare='^[[:space:]]*SA02M_CLEAN_MARKER=(/[A-Za-z0-9._/-]+)[[:space:]]*$'
    path_re_quoted='^[[:space:]]*SA02M_CLEAN_MARKER="(/[A-Za-z0-9._/-]+)"[[:space:]]*$'
    if [[ $aline =~ $path_re_bare ]] || [[ $aline =~ $path_re_quoted ]]; then
        mpath=${BASH_REMATCH[1]}
    fi
    if [ -z "$aline" ]; then
        bad "C $PRE_START sets no SA02M_CLEAN_MARKER= on an uncommented line"
    elif [ -z "$mpath" ]; then
        bad "C $PRE_START SA02M_CLEAN_MARKER= is not a plain literal path: $aline"
    else
        stop=$(unit_values "$mtext" ExecStop)
        if writes_exact_path "$stop" "$mpath"; then
            ok "C $MARKER_UNIT ExecStop writes exactly $mpath, the path $PRE_START reads"
        else
            bad "C $MARKER_UNIT ExecStop does not redirect to exactly $mpath (the path $PRE_START reads) — the marker can never be found"
        fi
    fi
    # shellcheck disable=SC2016  # literal needle: the "$…" is the source text
    if text_has "$ptext" '[ -f "$SA02M_CLEAN_MARKER" ]'; then
        ok "C $PRE_START tests the marker"
    else
        bad "C $PRE_START does not test [ -f \"\$SA02M_CLEAN_MARKER\" ] — nobody reads the marker"
    fi
    if names_word "$(unit_values "$mtext" After)" local-fs.target; then
        ok "C $MARKER_UNIT orders After=local-fs.target (marker written before unmount)"
    else
        bad "C $MARKER_UNIT has no After=local-fs.target — the stop may run after /var/lib is gone"
    fi
    if known_open_reason "$MARKER_UNIT" >/dev/null; then
        bad "C $MARKER_UNIT is in KNOWN_OPEN — the marker unit cannot be accepted debt"
    fi
fi

# ── D: installer lands it atomically and never restarts it ──────────────────
itext=$(stripped_text "$INSTALLER")
if [ -z "$itext" ]; then
    bad "D $INSTALLER is missing or empty"
else
    # shellcheck disable=SC2016  # literal needle: the "$…" is the source text
    if text_has "$itext" 'sa02m_atomic_install -m 644 "$ETC_REPO/sa02m-shutdown-marker.service" /etc/systemd/system/sa02m-shutdown-marker.service'; then
        ok "D $INSTALLER lands the unit via sa02m_atomic_install"
    else
        bad "D $INSTALLER does not land $MARKER_UNIT via sa02m_atomic_install — a repo fix never reaches a board"
    fi
    if text_has "$itext" 'sa02m_svc_apply sa02m-shutdown-marker.service infra start'; then
        ok "D $INSTALLER enables and starts the unit (no restart)"
    else
        bad "D $INSTALLER does not apply the unit with 'sa02m_svc_apply sa02m-shutdown-marker.service infra start' — an inactive unit has no stop to run"
    fi
fi
# Pre-select by raw text (comments included — stripping happens per file
# below); the installer itself must be among the hits or the sweep is dead.
d_files=()
while IFS= read -r -d '' f; do d_files+=("${f#./}"); done < <(grep -rlZ --include='*.sh' --include='*.py' \
    --exclude-dir=node_modules -e sa02m-shutdown-marker "${INSTALLER_HOMES[@]}" 2>/dev/null)
names_word "${d_files[*]}" "$INSTALLER" || bad "D installer sweep did not reach $INSTALLER — the sweep is dead"
d_bad=0
for f in "${d_files[@]}"; do
    t=$(stripped_text "$f")
    text_has "$t" sa02m-shutdown-marker || continue
    hits=$(grep -nE 'sa02m-shutdown-marker' <<<"$t") || continue
    while IFS= read -r h; do
        body=${h#*:}
        if text_matches "$body" '(^|[^_[:alnum:]])(restart|try-restart|reload-or-restart)([^[:alnum:]]|$)'; then
            bad "D $f:${h%%:*}: restarts the marker unit — a restart runs ExecStop and leaves a false CLEAN marker: $body"
            d_bad=$((d_bad + 1))
        fi
        if text_matches "$body" '(^|[[:space:];&|])(install|cp|mv)[[:space:]]' && ! text_has "$body" sa02m_atomic_install; then
            bad "D $f:${h%%:*}: lands the marker unit without sa02m_atomic_install: $body"
            d_bad=$((d_bad + 1))
        fi
    done <<<"$hits"
done
[ "$d_bad" -eq 0 ] && ok "D no installer line naming the marker unit restarts it or lands it non-atomically"

if [ "$fails" -gt 0 ]; then
    echo "unit-shutdown-semantics: $fails failure(s)"
    exit 1
fi
echo "unit-shutdown-semantics: all checks passed (${#units[@]} unit files)"
exit 0
