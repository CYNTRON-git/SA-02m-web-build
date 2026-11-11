#!/usr/bin/env bash
# no-timeout-on-shell-function — no shell FUNCTION is ever run through
# `timeout` (or a wrapper around it).
#
# WHY. `timeout N name` execs `name` as a program. When `name` is a shell
# function, there is no such program: timeout exits 127, and every caller here
# writes `… || true` around a best-effort call — so the call silently never
# happens. apply.cgi did exactly that with `timeout_run 12 sync_rtc_from_system`
# (audit 2026-11-11 A8-02): after a manual time set the battery RTC was never
# written, and the next cold boot restored the old time from it. Bound a shell
# function by bounding the external calls INSIDE it (lib_rtc.sh does), never by
# wrapping the function.
#
# WHAT IT READS — every home a device-side shell function can live in: the CGI
# tree (www/network_config/cgi-bin/*, libs included — apply.cgi's function came
# from lib_rtc.sh), etc/*.sh, usr/local/sbin/*, scripts/*.sh. Function names are
# collected from ALL of them (a function sourced from a lib is still a
# function), comment-stripped through lib_check.sh. A call site is a wrapper
# word — `timeout`, `timeout_run`, `sa02m_rtc_timeout_run`, `run_to` — followed
# by its options (`-k N`, `-s SIG`, `--kill-after=…`, `--foreground`, …), one
# duration token, an optional `sudo [-n]` (sudo cannot run a function either),
# and then the command word. FAIL when that word is a collected function name.
#
# Non-vacuous: fewer than 200 collected function names, fewer than 4 wrapper
# words seen at a call site, or an empty file sweep FAILS — a sweep that stopped
# seeing the tree must not pass on nothing.
#
# comment-mutation-proof-exempt: every pin here is a fail-IF-PRESENT sweep (a function name after a timeout wrapper); commenting a line out can only REMOVE a violation, never hide one — the RED is re-introducing the pattern (`timeout_run 12 sync_rtc_from_system` in apply.cgi, recorded in the commit), and the non-vacuity floors (>=200 functions, >=4 wrapper words, non-empty sweep) are counts over the whole tree that no single comment-out moves below their floor.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. .ai-dev/quality/checks/lib_check.sh || { echo "FAIL  lib_check.sh not loadable"; exit 1; }

files=()
for f in www/network_config/cgi-bin/* etc/*.sh usr/local/sbin/* scripts/*.sh; do
    [ -f "$f" ] || continue
    files+=("$f")
done
[ "${#files[@]}" -ge 50 ] || { echo "FAIL  sweep saw only ${#files[@]} file(s) — the tree layout moved"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

# Stripped copy of every file (comments blanked, line numbers kept).
for f in "${files[@]}"; do
    out="$T/s/$f"
    mkdir -p "$(dirname "$out")"
    stripped_text "$f" > "$out"
done

# 1. every shell function name defined anywhere in the sweep.
( cd "$T/s" && cat -- "${files[@]}" ) \
  | awk '{
        if (match($0, /^[[:space:]]*(function[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*[[:space:]]*\(\)/)) {
            s = substr($0, RSTART, RLENGTH)
            sub(/^[[:space:]]*(function[[:space:]]+)?/, "", s)
            sub(/[[:space:]]*\(\)$/, "", s)
            print s
        }
    }' | sort -u > "$T/names"
n_names=$(wc -l < "$T/names")

# 2. every wrapper call site whose command word is one of those names.
( cd "$T/s" && awk -v NAMES="$T/names" '
    BEGIN {
        while ((getline n < NAMES) > 0) fn[n] = 1
        w["timeout"] = 1; w["timeout_run"] = 1; w["sa02m_rtc_timeout_run"] = 1; w["run_to"] = 1
        opt_arg["-k"] = 1; opt_arg["-s"] = 1; opt_arg["--kill-after"] = 1; opt_arg["--signal"] = 1
    }
    FNR == 1 { file = FILENAME }
    {
        nt = split($0, t, /[[:space:]]+/)
        for (i = 1; i <= nt; i++) {
            tok = t[i]
            sub(/^(\$\(|\(|!|`)+/, "", tok)
            if (!(tok in w)) continue
            seen[tok] = 1
            j = i + 1
            while (j <= nt && t[j] ~ /^-/) {
                o = t[j]; j++
                if ((o in opt_arg) && o !~ /=/) j++
            }
            j++                                   # the duration
            if (j <= nt && t[j] == "sudo") {
                j++
                while (j <= nt && t[j] ~ /^-/) { if (t[j] == "-u" || t[j] == "-g") j++; j++ }
            }
            if (j > nt) continue
            cmd = t[j]
            gsub(/^["\047]+|["\047;)]+$/, "", cmd)
            if (cmd in fn) printf "%s:%d: %s runs shell function %s\n", file, FNR, tok, cmd
        }
    }
    END {
        k = 0; for (x in seen) k++
        printf "WRAPPERS %d\n", k > "/dev/stderr"
    }' "${files[@]}" ) > "$T/hits" 2> "$T/wrappers"
n_wrap=$(awk '/^WRAPPERS /{print $2}' "$T/wrappers")
n_hits=$(wc -l < "$T/hits")

fails=0
if [ "$n_names" -lt 200 ]; then
    echo "FAIL  only $n_names shell function names collected (floor 200) — the collector stopped seeing definitions"
    fails=$((fails + 1))
fi
if [ "${n_wrap:-0}" -lt 4 ]; then
    echo "FAIL  only ${n_wrap:-0} of the 4 wrapper words seen at a call site (floor 4) — the call-site scan stopped matching"
    fails=$((fails + 1))
fi
if [ "$n_hits" -gt 0 ]; then
    while IFS= read -r h; do echo "FAIL  $h — timeout cannot exec a function (exit 127)"; done < "$T/hits"
    fails=$((fails + 1))
fi
if [ "$fails" -eq 0 ]; then
    echo "no-timeout-on-shell-function: ALL OK — ${#files[@]} files, $n_names function names, $n_wrap wrapper words, 0 wrapped functions"
    exit 0
fi
exit 1
