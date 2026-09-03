#!/usr/bin/env bash
# sh-model-schema - the validating check for docs/contracts/sh-model.md (plan D4 / Q4=docs+test).
#
# ORCHESTRATOR: install this in the sibling repo SA-02m-web-build as
#   .ai-dev/quality/checks/sh-model-schema.sh
# (the sibling convention is checks/<row-id>.sh; the row id is sh-model-schema).
# The exact tools.json registry row to add is printed at the bottom of this header.
#
# WHY THIS EXISTS. docs/contracts/sh-model.schema.json is the executable half of
# the model contract: it fixes the shape of the SA-02m unified device model
# (Device -> functions[] -> points[]) that the Phase-B daemon, the Phase-C UI
# and the Phase-F Alice profile all build against. A schema nobody runs is not a
# contract - a change that dropped `quality` from the required set, loosened
# additionalProperties, or widened the Function.type enum would pass the whole
# build beat green. This row makes the schema real: the reference example
# (sh-model.example.json, a direct translation of the WB-MR6C catalog template)
# MUST validate, and three deliberately broken variants MUST be rejected. The
# negative half is load-bearing - a schema that accepts everything would pass a
# positive-only check while guaranteeing nothing.
#
# METHOD. Deps-guarded (like .ai-dev/quality/checks/pytest-suite.sh): skips
# CLEANLY (exit 0, one INFO line) when no python / no `jsonschema` module is
# present, so local dev without the dep is never blocked; CI installs jsonschema
# (web-quality.yml) and the row is REAL there. Validation uses the stable
# jsonschema library API (Draft202012Validator) rather than the `python -m
# jsonschema` CLI the contract's manual recipe names: that CLI is DEPRECATED and
# slated for removal in a future jsonschema release, and the library call is the
# durable equivalent AND lets the negative cases run in one process. Same
# guarantee, future-proof invocation.
#
# comment-mutation-proof: N/A - this row runs a real validator (a
# schema+examples check), not a source-line grep; a commented-out assertion
# below disappears as a missing check, which the non-vacuity guard (positive
# MUST pass AND every negative MUST be rejected, else exit 1) catches.
set -u

ROW="sh-model-schema"
DIR="docs/contracts"
SCHEMA="$DIR/sh-model.schema.json"
EXAMPLE="$DIR/sh-model.example.json"

PY=""
for p in python3 python py; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys" >/dev/null 2>&1; then PY="$p"; break; fi
done
[ -n "$PY" ] || { echo "$ROW: no working python interpreter"; exit 1; }

if ! "$PY" -c "import jsonschema" >/dev/null 2>&1; then
    echo "$ROW: python module 'jsonschema' not installed - skipped (installed in CI + dev)"; exit 0
fi

[ -r "$SCHEMA" ]  || { echo "$ROW: schema not readable at $SCHEMA"; exit 1; }
[ -r "$EXAMPLE" ] || { echo "$ROW: example not readable at $EXAMPLE"; exit 1; }

SCHEMA="$SCHEMA" EXAMPLE="$EXAMPLE" ROW="$ROW" "$PY" - <<'PY'
import json, os, copy, sys
from jsonschema import Draft202012Validator

row = os.environ["ROW"]
schema = json.load(open(os.environ["SCHEMA"], encoding="utf-8"))
example = json.load(open(os.environ["EXAMPLE"], encoding="utf-8"))

# The schema must itself be a valid draft 2020-12 schema.
try:
    Draft202012Validator.check_schema(schema)
except Exception as e:
    print(f"{row}: schema is not a valid draft 2020-12 schema: {e}")
    sys.exit(1)

v = Draft202012Validator(schema)

# POSITIVE: the reference example must validate.
pos_errs = sorted(v.iter_errors(example), key=lambda e: list(e.path))
if pos_errs:
    print(f"{row}: FAIL - reference example does not validate:")
    for e in pos_errs[:10]:
        print("   ", list(e.path), "-", e.message)
    sys.exit(1)

# NEGATIVE: each broken variant must be REJECTED. A variant that still validates
# means the schema stopped guaranteeing that invariant -> the row FAILS.
negatives = []

m = copy.deepcopy(example); del m["functions"][0]["points"][0]["quality"]
negatives.append(("point missing required 'quality'", m))

m = copy.deepcopy(example); m["functions"][0]["points"][0]["unexpectedField"] = 1
negatives.append(("point carries an unknown field (additionalProperties:false)", m))

m = copy.deepcopy(example); del m["functions"][0]["points"][0]["binding"]
negatives.append(("physical-function point missing 'binding'", m))

failures = []
for label, mut in negatives:
    if not list(v.iter_errors(mut)):
        failures.append(label)

if failures:
    print(f"{row}: FAIL - the schema ACCEPTED a variant it must reject:")
    for f in failures:
        print("   -", f)
    sys.exit(1)

print(f"{row}: ok - reference example validates; {len(negatives)} broken variants rejected "
      "(missing quality, unknown field, missing binding).")
sys.exit(0)
PY

# -----------------------------------------------------------------------------
# tools.json registry row to add in SA-02m-web-build/.ai-dev/quality/tools.json:
#
#     {
#       "id": "sh-model-schema",
#       "checks": "Validates the SA-02m unified device model contract (docs/contracts/sh-model.md): the reference example (sh-model.example.json, a direct translation of the WB-MR6C catalog template) MUST validate against sh-model.schema.json (draft 2020-12), and three broken variants (a point missing the required 'quality', a point with an unknown field, a physical-function point missing 'binding') MUST be rejected. The negative half is load-bearing: it proves the schema's fail-closed guarantees (quality required + no default, additionalProperties:false, binding required on physical points) are actually enforced, not just documented. Deps-guarded: skips cleanly when python/jsonschema is absent (like pytest-suite.sh), real in CI (web-quality.yml installs jsonschema).",
#       "config": null,
#       "run": "bash .ai-dev/quality/checks/sh-model-schema.sh",
#       "beat": "build",
#       "covers": ["docs/contracts/sh-model.md", "docs/contracts/sh-model.schema.json", "docs/contracts/sh-model.example.json", ".ai-dev/quality/checks/sh-model-schema.sh"],
#       "init": "python3 + jsonschema (pip install jsonschema)"
#     }
#
# CI note: add `jsonschema` to the pip install step in
# .github/workflows/web-quality.yml so the row is REAL in CI (skipped locally).
# -----------------------------------------------------------------------------
