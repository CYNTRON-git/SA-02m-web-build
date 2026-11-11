#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: behavioural harness - it RUNS the shipped json_escape (lib_web_json.sh under bash, the helper's local copy under sh) over a byte corpus and compares outputs and the PATH-shim log; it pins no source line a comment token could satisfy. RED is recorded below against the pre-change sed escaper.
# test-json-escape-equiv.sh — the shared JSON string escaper stays
# byte-compatible while it stops forking (1.0.7.36, A8-06).
#
# What it pins:
#   E  equivalence — for every corpus value whose old output was a valid JSON
#      string body, the bash escaper in www/network_config/cgi-bin/
#      lib_web_json.sh gives the SAME BYTES as the old
#      `printf | sed` escaper (kept verbatim below as the oracle), read the way
#      every caller reads it: through `$(json_escape …)` (pinned by case S).
#   R  repair — the old sed script escaped `\`, `"` and CR on the FIRST line
#      only (its `:a;N;$!ba` loop jumps back past the substitutions), so a value
#      with one of them on line 2+ came out as invalid or injectable JSON
#      (`a\nb"c` -> `a b"c`). The new escaper escapes every line; the old
#      output is asserted invalid here so this list cannot silently go stale.
#   Z  zero external processes — `sed`, `awk`, `tr` and `cut` are PATH shims
#      that log and fail; the new escaper must not reach any of them (the old
#      one forks sed on every value of every status poll).
#   H  the helper's local copy (etc/sa02m-web-service-ctl.sh, POSIX sh — no
#      bash pattern substitution there) gives the same bytes as the lib on the
#      whole corpus, so the two homes cannot drift apart.
#   S  every shipped call site reads the escaper through `$(…)`, the context
#      the equivalence above is stated in.
# Unchanged on purpose: control characters other than CR/LF pass through raw
# (escaping them is A8-14, a change of output, not part of this change).
#
# RED (2026-11-11, pre-change tree): Z fails — the shim log shows `sed` once
# per corpus value; R fails — the "new" (= old) output of every repair input is
# invalid JSON; H passes vacuously-equal there (both are the old sed).
#
# Run: bash scripts/dev/test-json-escape-equiv.sh   (bash, sh, python3)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

LIB=${JSON_LIB_SRC:-www/network_config/cgi-bin/lib_web_json.sh}
CTL=${SVC_CTL_SRC:-etc/sa02m-web-service-ctl.sh}
[ -f "$LIB" ] || { echo "FAIL  json lib not found: $LIB"; exit 1; }
[ -f "$CTL" ] || { echo "FAIL  service helper not found: $CTL"; exit 1; }
PY=""
for p in python3 python; do command -v "$p" >/dev/null 2>&1 && "$p" -c 'import json' >/dev/null 2>&1 && { PY=$p; break; }; done
[ -n "$PY" ] || { echo "FAIL  no python interpreter (JSON decoding needs one)"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# The pre-1.0.7.36 escaper, verbatim — the oracle for case E.
old_json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g; :a;N;$!ba;s/\n/ /g'
}

# shellcheck source=www/network_config/cgi-bin/lib_web_json.sh
. "$LIB"
declare -F json_escape >/dev/null || { echo "FAIL  $LIB does not define json_escape"; exit 1; }

# The helper copy, extracted by its anchor and run under POSIX sh.
awk '/^json_escape\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$CTL" > "$T/ctl_escape.sh"
grep -q '^json_escape() {' "$T/ctl_escape.sh" || { echo "FAIL  json_escape() not found in $CTL"; exit 1; }
printf '%s\n' 'json_escape "$1"' >> "$T/ctl_escape.sh"
ctl_json_escape() { sh "$T/ctl_escape.sh" "$1"; }

# CR is spliced in from a variable: MSYS bash (Git Bash 5.2) drops a $'\r'
# escape inside an array literal once that literal spans a newline (measured).
CR=$'\r'; NL=$'\n'
# Corpus E: every value the old escaper handled correctly.
E_CORPUS=(
    ''
    'plain'
    '\'
    '"'
    'a"b\c'
    "$CR"
    "a${CR}b"
    $'\n'
    $'\n\n'
    "$CR$NL"
    "a$CR$NL"
    $'a\n'
    $'a\n\n'
    $'a\nb'
    "l1 \"q\" \\x$CR${NL}plain line two"
    'Привет, мир'
    $'a\tb'
    $'ctl\x01\x1f\x7fend'
    $'trailing space \n'
    '-n'
    '%s %d'
)
# Corpus R: a `\`, `"` or CR on line 2+ — broken by the old escaper.
R_CORPUS=(
    $'a\nb"c'
    $'a\nb\\c'
    "a${NL}b${CR}c"
    "x$CR$NL\"y\""
    $'one\ntwo\nthree "3"'
)

# Decode `"<body>"` and print repr of the string, or INVALID. strict=False for
# the control-character values the escaper deliberately leaves raw.
decode() {  # $1 = escaped body, $2 = strict (1|0)
    printf '%s' "$1" | "$PY" -c '
import json, sys
body = sys.stdin.buffer.read().decode("utf-8")
try:
    print(repr(json.loads("\"" + body + "\"", strict=sys.argv[1] == "1")))
except ValueError:
    print("INVALID")
' "$2"
}
# What a value means once escaped: one trailing LF dropped, CR removed, LF -> space.
expected_repr() {
    "$PY" -c '
import sys
v = sys.argv[1]
if v.endswith("\n"): v = v[:-1]
print(repr(v.replace("\r", "").replace("\n", " ")))
' "$1"
}

n=0
for v in "${E_CORPUS[@]}"; do
    n=$((n + 1))
    o=$(old_json_escape "$v"); w=$(json_escape "$v")
    if [ "$o" = "$w" ]; then
        ok "E$n byte-identical to the old escaper ($(printf '%q' "$v"))"
    else
        bad "E$n differs from the old escaper for $(printf '%q' "$v"): old=$(printf '%q' "$o") new=$(printf '%q' "$w")"
    fi
    got=$(decode "$w" 0); want=$(expected_repr "$v")
    [ "$got" = "$want" ] || bad "E$n new output decodes to $got, expected $want"
done
[ "$n" -ge 20 ] || bad "E  corpus shrank to $n values — the equivalence would be vacuous"

n=0
for v in "${R_CORPUS[@]}"; do
    n=$((n + 1))
    o=$(old_json_escape "$v"); w=$(json_escape "$v")
    [ "$(decode "$o" 1)" = INVALID ] \
        || bad "R$n the old escaper got $(printf '%q' "$v") right — this is not a repair case, move it to E"
    got=$(decode "$w" 1); want=$(expected_repr "$v")
    if [ "$got" = "$want" ]; then
        ok "R$n line 2+ escaped: $(printf '%q' "$v") -> valid JSON $got"
    else
        bad "R$n $(printf '%q' "$v") -> $got (expected $want) — a quote/backslash/CR past line 1 escapes unescaped"
    fi
done

# Z: zero external processes.
SHIM="$T/shim"; mkdir -p "$SHIM"
for tool in sed awk tr cut; do
    printf '#!/bin/sh\necho %s >> "%s/forks.log"\nexit 1\n' "$tool" "$T" > "$SHIM/$tool"
    chmod +x "$SHIM/$tool"
done
for v in "${E_CORPUS[@]}" "${R_CORPUS[@]}"; do
    a=$(json_escape "$v")
    b=$(PATH="$SHIM:$PATH" json_escape "$v")
    [ "$a" = "$b" ] || bad "Z  output changed with the text tools shimmed out for $(printf '%q' "$v")"
done
if [ -s "$T/forks.log" ]; then
    bad "Z  json_escape forked external tools: $(sort "$T/forks.log" | uniq -c | tr -s ' ' | tr '\n' ';')"
else
    ok "Z  json_escape starts no external process (sed/awk/tr/cut shimmed to fail)"
fi
PATH="$SHIM:$PATH" old_json_escape x >/dev/null 2>&1
[ -s "$T/forks.log" ] || bad "Z  the shim never fired even for the sed oracle — the PATH shim is broken, Z is vacuous"

# H: the helper's POSIX copy agrees byte for byte with the lib.
n=0; hbad=0
for v in "${E_CORPUS[@]}" "${R_CORPUS[@]}"; do
    n=$((n + 1))
    a=$(json_escape "$v"); b=$(ctl_json_escape "$v")
    [ "$a" = "$b" ] || { hbad=1; bad "H$n helper copy differs for $(printf '%q' "$v"): lib=$(printf '%q' "$a") helper=$(printf '%q' "$b")"; }
done
[ "$hbad" -eq 0 ] && ok "H  the helper's local copy matches the lib on all $n values"

# S: every call site uses $(json_escape …).
sites=0; loose=""
for f in www/network_config/cgi-bin/*.cgi www/network_config/cgi-bin/*.sh "$CTL"; do
    while IFS= read -r line; do
        case "$line" in *'json_escape() {'*|*'#'*json_escape*) continue ;; esac
        sites=$((sites + 1))
        rest=$line
        while [[ "$rest" == *json_escape* ]]; do
            pre=${rest%%json_escape*}
            [[ "$pre" == *'$(' ]] || loose="$loose
  $f: $line"
            rest=${rest#*json_escape}
        done
    done < <(grep -h 'json_escape' "$f" 2>/dev/null)
done
[ "$sites" -ge 20 ] || bad "S  only $sites call sites found — the sweep is vacuous"
if [ -z "$loose" ]; then
    ok "S  all $sites call-site lines read the escaper through \$(…)"
else
    bad "S  call sites outside \$(…) — the equivalence above does not describe them:$loose"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "PASS  json-escape-equiv"
    exit 0
fi
echo "FAIL  $fails case(s) failed"
exit 1
