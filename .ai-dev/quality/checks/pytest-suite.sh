#!/usr/bin/env bash
# Deps-guarded pytest runner for a daemon's test suite — the ONE home for the
# guard logic shared by every py-unit-<daemon> row that runs pytest-style tests
# (the daemon suites are pytest functions, which `unittest discover` silently
# skips; this row actually executes them).
#
# Usage: pytest-suite.sh <row-id> <daemon-dir> [import-dep ...]
#
# Skips CLEANLY (exit 0, one INFO line) when no python / pytest / a listed
# runtime dep is missing, so local dev without the device deps is never blocked.
# The gate is made REAL in CI, where .github/workflows/web-quality.yml installs
# pytest + the deps (the same skipped-locally / runs-in-CI contract the lint row
# uses). It runs `pytest <daemon-dir>/tests` from inside the daemon dir (the
# suites have no conftest and import their package by cwd on sys.path).
# COMMENT-BLINDNESS AUDIT (1.0.6.24): N/A — this is a runner, not a grep. It
# dispatches a real pytest run; a commented-out test disappears from the run
# as a missing test, which is a coverage question, not a hollow pin.
set -u

id="${1:?row-id required}"
dir="${2:?daemon-dir required}"
shift 2

PY=""
for p in python3 python py; do
    if "$p" -c "import sys" >/dev/null 2>&1; then PY="$p"; break; fi
done
[ -n "$PY" ] || { echo "$id: no working python interpreter"; exit 1; }

if ! "$PY" -c "import pytest" >/dev/null 2>&1; then
    echo "$id: pytest not installed — skipped (installed in CI + dev)"; exit 0
fi
for m in "$@"; do
    if ! "$PY" -c "import $m" >/dev/null 2>&1; then
        echo "$id: runtime dep '$m' missing — skipped (installed in CI)"; exit 0
    fi
done

[ -d "$dir/tests" ] || { echo "$id: no tests dir at $dir/tests"; exit 1; }
cd "$dir" || { echo "$id: cannot cd $dir"; exit 1; }
exec "$PY" -m pytest tests -q -p no:cacheprovider
