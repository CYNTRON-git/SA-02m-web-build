#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# test-build-homekit-wheelhouse.sh — offline harness for the dev tool
# scripts/dev/build-homekit-wheelhouse.sh (quality row `homekit-wheelhouse`).
#
# Method: the SHIPPED script runs with `timeout` and `python3` as PATH shims.
# The python3 shim stands in for `pip download`: it records its argv and drops
# the fake wheel files named in $HK_FAKE_WHEELS into the `-d` directory, and
# refuses every other invocation — nothing here reaches PyPI or needs pip. The
# lock is a fixture (HK_WHEELHOUSE_LOCK), so the cases do not drift with the
# real requirements.lock. What is NOT covered: pip's own --require-hashes
# check against real wheels (needs the network; the argv is asserted instead).
#
# Cases:
#   W1 no argument -> exit 2 + usage, pip never called
#   W2 exactly the pinned set (armv7l + pure-Python) -> exit 0, success line;
#      pip got --no-deps --only-binary=:all: --require-hashes, an armv7l
#      platform, -r <lock>, and was reached through `timeout`
#   W3 a pinned wheel missing -> exit 1, "do not match the lock"
#   W4 an extra (unpinned) wheel -> exit 1, "do not match the lock"
#   W5 a foreign-arch wheel for a pinned package -> exit 1, "not an armv7l"
#   W6 pip download fails -> non-zero, no success line
#   W7 a lock that parses to zero packages -> exit 1 WITH the "vacuous" line
#      (RED on the pre-fix `n_want=$(… | grep -c .)`: under set -e/pipefail
#      the zero-count grep aborted the script silently before the message)
#
# Comment-mutation case registered with the row (measured RED here):
#   commenting out the arch-check `*) echo … is not an armv7l …` line -> W5 RED.
#
# Run: bash scripts/dev/test-build-homekit-wheelhouse.sh      (bash only)
#      HK_WHEELHOUSE_SRC=<file> judges another copy of the tool (the RED run).
# ═══════════════════════════════════════════════════════════════════════════
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="${HK_WHEELHOUSE_SRC:-$HERE/scripts/dev/build-homekit-wheelhouse.sh}"

fails=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -r "$SRC" ] || { echo "homekit-wheelhouse: FAIL — cannot read $SRC"; exit 1; }

SB="$(mktemp -d "${TMPDIR:-/tmp}/hk-wheelhouse.XXXXXX")" || { echo "homekit-wheelhouse: FAIL — mktemp"; exit 1; }
trap 'rm -rf -- "$SB"' EXIT
SHIM="$SB/shim"
mkdir -p "$SHIM"

# timeout: `timeout N cmd…` -> run cmd marked as bounded.
cat > "$SHIM/timeout" <<'EOF'
#!/usr/bin/env bash
shift
HK_VIA_TIMEOUT=1 exec "$@"
EOF
# python3: only `-m pip download … -d <dir> …` is served.
cat > "$SHIM/python3" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" != "-m" ] || [ "${2:-}" != "pip" ] || [ "${3:-}" != "download" ]; then
    echo "python3 shim: refused: $*" >&2
    exit 97
fi
printf 'via_timeout=%s\n' "${HK_VIA_TIMEOUT:-0}" > "$HK_PIP_LOG"
printf '%s\n' "$@" >> "$HK_PIP_LOG"
out=""
while [ $# -gt 0 ]; do
    [ "$1" = "-d" ] && { out=$2; break; }
    shift
done
[ -n "$out" ] || { echo "python3 shim: no -d" >&2; exit 98; }
[ "${HK_FAKE_PIP_RC:-0}" -eq 0 ] || exit "$HK_FAKE_PIP_RC"
for w in ${HK_FAKE_WHEELS:-}; do : > "$out/$w"; done
exit 0
EOF
chmod +x "$SHIM"/*

LOCK="$SB/requirements.lock"
cat > "$LOCK" <<'EOF'
# fixture lock: two pinned packages, names needing PEP 503 normalisation
Foo-Bar==1.0 \
    --hash=sha256:0000000000000000000000000000000000000000000000000000000000000000
baz.qux==2.0 \
    --hash=sha256:1111111111111111111111111111111111111111111111111111111111111111
EOF
EMPTY_LOCK="$SB/empty.lock"
printf '# nothing pinned\n' > "$EMPTY_LOCK"

ARM=foo_bar-1.0-cp312-cp312-manylinux_2_17_armv7l.whl
PURE=baz_qux-2.0-py3-none-any.whl

# run_tool <lock> [args…] — stdout+stderr -> $SB/out, rc -> $RC.
run_tool() {
    local lock=$1
    shift
    rm -rf -- "$SB/wh" "$SB/pip.log"
    PATH="$SHIM:$PATH" HK_PIP_LOG="$SB/pip.log" HK_WHEELHOUSE_LOCK="$lock" \
        bash "$SRC" "$@" > "$SB/out" 2>&1
    RC=$?
}
out_has() { grep -qF -- "$1" "$SB/out"; }
pip_has() { grep -qxF -- "$1" "$SB/pip.log" 2>/dev/null; }

HK_FAKE_WHEELS="$ARM $PURE" run_tool "$LOCK"
if [ "$RC" -eq 2 ] && out_has 'usage:' && [ ! -e "$SB/pip.log" ]; then
    ok "W1 no argument: exit 2 with usage, pip never called"
else
    bad "W1 no argument: rc=$RC pip-called=$([ -e "$SB/pip.log" ] && echo yes || echo no) out=$(tr '\n' ' ' < "$SB/out")"
fi

HK_FAKE_WHEELS="$ARM $PURE" run_tool "$LOCK" "$SB/wh"
if [ "$RC" -eq 0 ] && out_has '2 wheels for armv7l/cp312' \
   && pip_has 'via_timeout=1' && pip_has '--no-deps' && pip_has '--only-binary=:all:' \
   && pip_has '--require-hashes' && pip_has 'manylinux_2_17_armv7l' && pip_has "$LOCK"; then
    ok "W2 the exact pinned set: exit 0, success line; pip bounded by timeout, --no-deps --only-binary --require-hashes, armv7l, -r <lock>"
else
    bad "W2 exact set: rc=$RC out=$(tr '\n' ' ' < "$SB/out") pip=$(tr '\n' ' ' < "$SB/pip.log" 2>/dev/null)"
fi

HK_FAKE_WHEELS="$ARM" run_tool "$LOCK" "$SB/wh"
if [ "$RC" -eq 1 ] && out_has 'do not match the lock' && ! out_has 'wheels for armv7l'; then
    ok "W3 a pinned wheel missing: exit 1, mismatch reported"
else
    bad "W3 missing wheel: rc=$RC out=$(tr '\n' ' ' < "$SB/out")"
fi

HK_FAKE_WHEELS="$ARM $PURE extra_pkg-9.9-py3-none-any.whl" run_tool "$LOCK" "$SB/wh"
if [ "$RC" -eq 1 ] && out_has 'do not match the lock' && ! out_has 'wheels for armv7l'; then
    ok "W4 an extra unpinned wheel: exit 1, mismatch reported"
else
    bad "W4 extra wheel: rc=$RC out=$(tr '\n' ' ' < "$SB/out")"
fi

HK_FAKE_WHEELS="foo_bar-1.0-cp312-cp312-manylinux_2_17_x86_64.whl $PURE" run_tool "$LOCK" "$SB/wh"
if [ "$RC" -eq 1 ] && out_has 'is not an armv7l or pure-Python wheel' && ! out_has 'wheels for armv7l'; then
    ok "W5 a foreign-arch wheel for a pinned package: exit 1, named"
else
    bad "W5 foreign arch: rc=$RC out=$(tr '\n' ' ' < "$SB/out")"
fi

HK_FAKE_PIP_RC=1 HK_FAKE_WHEELS="$ARM $PURE" run_tool "$LOCK" "$SB/wh"
if [ "$RC" -ne 0 ] && ! out_has 'wheels for armv7l'; then
    ok "W6 pip download fails: non-zero (rc=$RC), no success line"
else
    bad "W6 pip failure: rc=$RC out=$(tr '\n' ' ' < "$SB/out")"
fi

HK_FAKE_WHEELS="" run_tool "$EMPTY_LOCK" "$SB/wh"
if [ "$RC" -eq 1 ] && out_has '(vacuous)'; then
    ok "W7 a lock that parses to zero packages: exit 1 with the vacuous line"
else
    bad "W7 empty lock: rc=$RC, vacuous line printed=$(out_has '(vacuous)' && echo yes || echo no) out=$(tr '\n' ' ' < "$SB/out")"
fi

if [ "$fails" -eq 0 ]; then
    echo "homekit-wheelhouse: all checks passed"
    exit 0
fi
echo "homekit-wheelhouse: $fails FAILURE(S)"
exit 1
