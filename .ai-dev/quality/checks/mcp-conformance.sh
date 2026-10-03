#!/usr/bin/env bash
# mcp-conformance — initialize, tools/list, tools/call, resources/read.
# comment-mutation-proof-exempt: unittest runner over opt/sa02m-agent-api/tests/test_mcp.py; a commented line in the package changes the behaviour the test asserts, and this shell holds no pin a hash could satisfy.
set -euo pipefail
cd "$(dirname "$0")/../../.." || exit 1
for p in python3 python py; do
  if "$p" -c "import sys" >/dev/null 2>&1; then
    exec "$p" -m unittest discover -s opt/sa02m-agent-api/tests -t opt/sa02m-agent-api -p test_mcp.py -q
  fi
done
echo "mcp-conformance: no working python interpreter"
exit 1
