#!/bin/bash
# Static gate for docs/contracts/installer-refresh-policy.md — the installer's
# service/package policy has ONE home (scripts/lib.sh + etc/sa02m-stacks-policy.sh)
# and every module goes through it:
#   (a) no raw widening systemctl verb (enable|start|unmask|restart|
#       reload-or-restart) in install.sh / scripts/0*.sh / scripts/1*.sh
#       (lib.sh excluded — it IS the home); tightening verbs (stop, disable,
#       mask, reset-failed, daemon-reload, reload, try-restart, is-*, …) stay
#       raw by design;
#   (b) no inline apt-get/pip/npm install there (the tier helpers are the home);
#   (c) the four legacy helpers are gone from the tree;
#   (d) an `sa02m_svc_apply <unit> app` call has a preceding sa02m_svc_capture
#       in the same file (an uncaptured apply cannot see a first install);
#   (e) in the third-party modules (07/08/09/12) the stack verdict is read
#       BEFORE the first side-effecting act (useradd / dpkg -i / vendor
#       install.sh / apt / curl) — the 1.136 leftover-user class;
#   (f) non-vacuity: the helper call sites actually exist in numbers;
#   (g) install.sh still prints the literal completion banner the offline
#       wrapper's post-check greps.
#
# Run: bash .ai-dev/quality/checks/installer-svc-policy-gate.sh
#
# The negative sweeps (a)-(e) already skip comment lines by construction; the
# COUNTS in (f) and the banner pin in (g) did not, and a count that includes
# commented-out call sites is a non-vacuity floor that a mass comment-out slides
# straight under (audit 2026-08-28, finding C3 — the hollow-gate class). Both
# now read comment-stripped text via lib_check.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/lib_check.sh" || { echo "installer-svc-policy-gate: cannot source lib_check.sh"; exit 1; }

fails=0
fail() { printf 'installer-svc-policy-gate: FAIL  %s\n' "$*"; fails=$((fails + 1)); }
pass() { printf 'installer-svc-policy-gate: ok    %s\n' "$*"; }

MODULES=(install.sh scripts/0*.sh scripts/1*.sh)
for f in "${MODULES[@]}"; do
    [ -f "$f" ] || { fail "module set glob broke: $f does not exist"; }
done
[ "${#MODULES[@]}" -ge 10 ] || fail "module sweep sees only ${#MODULES[@]} files — glob broke (non-vacuity)"

# ── (a) raw widening systemctl verbs ───────────────────────────────────────
# Field-wise: find a token equal to `systemctl` or `sa02m_systemctl` (never
# /usr/bin/systemctl — the sudoers heredocs) at a command position (line start
# or right after && || ; ( $( then else do), then check whether any following
# field equals a widening verb (options like --now/--root=... may sit between).
widening=$(awk '
    /^[[:space:]]*#/ { next }
    {
        # a verb inside a log message is prose, not a call — strip from `log X "` on
        sub(/log (OK|WARN|ERR|INFO)[[:space:]].*/, "")
        n = split($0, t, /[[:space:]]+/)
        cmdpos = 1   # start of line is a command position
        for (i = 1; i <= n; i++) {
            w = t[i]
            if (w == "" ) { continue }
            if (w == "systemctl" || w == "sa02m_systemctl") {
                if (cmdpos) {
                    for (j = i + 1; j <= n; j++) {
                        v = t[j]
                        sub(/^"/, "", v); sub(/"$/, "", v)
                        if (v ~ /^--/) continue
                        if (v == "enable" || v == "start" || v == "unmask" || v == "restart" || v == "reload-or-restart") {
                            printf "%s:%d: %s\n", FILENAME, FNR, $0
                        }
                        break
                    }
                }
            }
            # command position after control/connector tokens
            if (w == "&&" || w == "||" || w == ";" || w == "(" || w == "$(" \
                || w == "then" || w == "else" || w == "do" || w == "!" \
                || w ~ /&&$/ || w ~ /\|\|$/ || w ~ /;$/ || w ~ /\($/) cmdpos = 1
            else cmdpos = 0
        }
    }
' "${MODULES[@]}")
if [ -n "$widening" ]; then
    fail "raw widening systemctl verb outside lib.sh:"
    printf '        %s\n' "$widening"
else
    pass "(a) no raw enable/start/unmask/restart/reload-or-restart in the modules"
fi

# ── (b) inline package installs ────────────────────────────────────────────
pkg=$(grep -nE 'apt-get[[:space:]]+(-[A-Za-z-]+[[:space:]]+)*(-y[[:space:]]+)?install|pip3?[[:space:]]+install|npm[[:space:]]+install' "${MODULES[@]}" \
      | grep -vE '^[^:]+:[0-9]+:[[:space:]]*#' \
      | grep -vE 'sa02m_pkg_install_tier|sa02m_pip_install|pip3 install --help' \
      | grep -vE 'log (OK|WARN|ERR|INFO) ' || true)
if [ -n "$pkg" ]; then
    fail "inline apt-get/pip/npm install outside the tier helpers:"
    printf '        %s\n' "$pkg"
else
    pass "(b) no inline apt-get/pip/npm install in the modules"
fi

# ── (c) legacy helpers gone ────────────────────────────────────────────────
legacy=$(grep -rnE '\b(svc_enable|svc_restart|sa02m_capture_svc_state|sa02m_restore_svc_state)\b' \
         install.sh scripts/ etc/ 2>/dev/null || true)
if [ -n "$legacy" ]; then
    fail "legacy service helpers still referenced:"
    printf '        %s\n' "$legacy"
else
    pass "(c) svc_enable/svc_restart/sa02m_capture_svc_state/sa02m_restore_svc_state are gone"
fi

# ── (d) every app apply has a preceding capture in the same file ───────────
d_bad=""
for f in "${MODULES[@]}"; do
    [ "$f" = scripts/lib.sh ] && continue
    while IFS=: read -r ln line; do
        [ -n "$ln" ] || continue
        unit=$(sed -E 's/.*sa02m_svc_apply[[:space:]]+([^[:space:]]+)[[:space:]]+app.*/\1/' <<<"$line")
        first_cap=""
        case "$unit" in
            \$*|\"\$*)
                # variable unit: require ANY capture earlier in the file
                first_cap=$(grep -nE 'sa02m_svc_capture[[:space:]]' "$f" | head -1 | cut -d: -f1)
                ;;
            *)
                first_cap=$(grep -nE "sa02m_svc_capture[[:space:]].*$unit" "$f" | head -1 | cut -d: -f1)
                ;;
        esac
        if [ -z "$first_cap" ] || [ "$first_cap" -ge "$ln" ]; then
            d_bad="$d_bad $f:$ln($unit)"
        fi
    done < <(grep -nE 'sa02m_svc_apply[[:space:]]+[^[:space:]]+[[:space:]]+app\b' "$f" | grep -vE '^[0-9]+:[[:space:]]*#' || true)
done
if [ -n "$d_bad" ]; then
    fail "(d) app apply without a preceding sa02m_svc_capture:$d_bad"
else
    pass "(d) every app-class sa02m_svc_apply is preceded by a capture in its file"
fi

# ── (e) third-party modules: verdict before the first side effect ──────────
for f in scripts/07-nodered.sh scripts/08-codesys.sh scripts/09-mplc.sh scripts/12-docker.sh; do
    [ -f "$f" ] || { fail "(e) $f missing"; continue; }
    vline=$(grep -nE 'sa02m_stack_verdict' "$f" | grep -vE '^[0-9]+:[[:space:]]*#' | head -1 | cut -d: -f1)
    if [ -z "$vline" ]; then
        fail "(e) $f never reads sa02m_stack_verdict"
        continue
    fi
    sline=$(grep -nE '\b(useradd|dpkg -i|bash \./install\.sh|apt|curl)\b' "$f" \
            | grep -vE '^[0-9]+:[[:space:]]*#' | head -1 | cut -d: -f1)
    if [ -n "$sline" ] && [ "$sline" -lt "$vline" ]; then
        fail "(e) $f acts (line $sline) before reading the verdict (line $vline)"
    else
        pass "(e) $f reads the verdict before any side effect"
    fi
done

# ── (f) non-vacuity: the helpers are really used ───────────────────────────
apply_n=0
for f in "${MODULES[@]}"; do
    apply_n=$((apply_n + $(stripped_count "$f" 'sa02m_svc_apply[[:space:]]')))
done
verdict_n=0
for f in scripts/07-nodered.sh scripts/08-codesys.sh scripts/09-mplc.sh scripts/12-docker.sh; do
    verdict_n=$((verdict_n + $(stripped_count "$f" 'sa02m_stack_verdict')))
done
if [ "${apply_n:-0}" -ge 25 ]; then
    pass "(f) >=25 sa02m_svc_apply sites ($apply_n)"
else
    fail "(f) only ${apply_n:-0} sa02m_svc_apply sites — the sweep regressed (expected >=25)"
fi
if [ "${verdict_n:-0}" -ge 4 ]; then
    pass "(f) >=4 sa02m_stack_verdict sites ($verdict_n)"
else
    fail "(f) only ${verdict_n:-0} verdict sites (expected >=4)"
fi

# ── (g) the completion banner literal ──────────────────────────────────────
if stripped_has install.sh 'Установка завершена'; then
    pass "(g) install.sh keeps the literal completion banner"
else
    fail "(g) install.sh lost the 'Установка завершена' banner (the wrapper's post-check greps it)"
fi

if [ "$fails" -eq 0 ]; then
    echo "installer-svc-policy-gate: all checks passed"
    exit 0
fi
echo "installer-svc-policy-gate: $fails FAILURE(S)"
exit 1
