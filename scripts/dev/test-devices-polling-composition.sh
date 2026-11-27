#!/usr/bin/env bash
# comment-mutation-proof-exempt: runs the real CGI producer and Chromium DOM assertions.
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
export PRESENCE_DASHBOARD="$stage/ports.json"
bash scripts/dev/test-rs485-roster-consumer.sh
test -s "$PRESENCE_DASHBOARD"
node scripts/dev/test-devices-stale-on-fetch-fail.mjs
