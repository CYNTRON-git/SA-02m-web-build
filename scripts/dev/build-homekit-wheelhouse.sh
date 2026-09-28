#!/usr/bin/env bash
# build-homekit-wheelhouse.sh — the offline wheelhouse for the HomeKit bridge
# venv (plan D9; lock provenance: docs/decisions/homekit-home-connect.md G1).
#
# Downloads EXACTLY the wheels opt/sa02m-homekit/requirements.lock pins, for
# the board (manylinux armv7l, CPython 3.12), into <out_dir>. pip checks every
# file against the lock's sha256 (--require-hashes; the lock carries the armv7l
# hash for each package). Copy <out_dir>/*.whl to /opt/vendor-installers/homekit/
# on the board or the offline media: scripts/06c-homekit.sh then installs with
# `--no-index --find-links` (scripts/lib.sh sa02m_venv_install_locked) and needs
# no network. cffi is deliberately absent — the board takes _cffi_backend from
# apt python3-cffi-backend (why: the decision doc, G1).
#
# Refuses to report success on a partial or wrong download: one wheel per
# pinned package, nothing extra, and every wheel is armv7l or pure Python.
#
# Usage: bash scripts/dev/build-homekit-wheelhouse.sh <out_dir>
# Needs: python3 with pip, and PyPI (or a configured pip index) reachable.
# Dev-only; never shipped to the device.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

LOCK=opt/sa02m-homekit/requirements.lock
OUT=${1:-}
[ -n "$OUT" ] || { echo "usage: bash scripts/dev/build-homekit-wheelhouse.sh <out_dir>" >&2; exit 2; }
[ -r "$LOCK" ] || { echo "build-homekit-wheelhouse: $LOCK not readable" >&2; exit 1; }
mkdir -p -- "$OUT"

timeout 900 python3 -m pip download --no-deps --only-binary=:all: --require-hashes \
    --platform manylinux_2_31_armv7l --platform manylinux2014_armv7l \
    --platform manylinux_2_17_armv7l \
    --python-version 3.12 --implementation cp --abi cp312 --abi abi3 --abi none \
    -d "$OUT" -r "$LOCK"

# Package names the lock pins (PEP 503-normalised: lower case, [-_.] -> _).
want=$(sed -nE 's/^([A-Za-z0-9_.-]+)==.*/\1/p' "$LOCK" | tr 'A-Z' 'a-z' | tr -- '-.' '__' | sort -u)
n_want=$(printf '%s\n' "$want" | grep -c .)
[ "$n_want" -ge 1 ] || { echo "build-homekit-wheelhouse: no pinned package parsed from $LOCK (vacuous)" >&2; exit 1; }

fails=0
got=""
for w in "$OUT"/*.whl; do
    [ -e "$w" ] || continue
    b=$(basename -- "$w")
    case "$b" in
        *armv7l.whl|*-none-any.whl) ;;
        *) echo "build-homekit-wheelhouse: $b is not an armv7l or pure-Python wheel" >&2; fails=$((fails + 1)) ;;
    esac
    got="$got
$(printf '%s\n' "${b%%-*}" | tr 'A-Z' 'a-z' | tr -- '-.' '__')"
done
got=$(printf '%s\n' "$got" | grep . | sort -u || true)
if [ "$got" != "$want" ]; then
    echo "build-homekit-wheelhouse: wheels in $OUT do not match the lock" >&2
    echo "  lock:  $(printf '%s ' $want)" >&2
    echo "  found: $(printf '%s ' $got)" >&2
    fails=$((fails + 1))
fi
if [ "$fails" -ne 0 ]; then
    exit 1
fi
echo "build-homekit-wheelhouse: $n_want wheels for armv7l/cp312 in $OUT — copy them to /opt/vendor-installers/homekit/"
