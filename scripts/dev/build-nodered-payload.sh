#!/bin/bash
# Canonical entry point; the Python builder owns payload and provenance logic.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/build-nodered-payload.py" "$@"
