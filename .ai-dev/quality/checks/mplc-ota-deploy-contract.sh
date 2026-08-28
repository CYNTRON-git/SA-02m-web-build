#!/bin/bash
# mplc-ota-deploy-contract — MPLC plugins ship via web OTA / offline pack.
#
# Pins the gap found on 1.0.6.11 field boards: GitHub OTA never deployed
# firmware/mplc4/*.so, so mplc_cyntron.so stayed on the vendor baseline and
# /run/sa02m-mplc-license.json was never published (web licence card fell back
# to a log scrape / «не активирована»).
#
# Every needle below is matched against COMMENT-STRIPPED text: the allow-list
# file comments with `#`, so `#firmware/mplc4/mplc_cyntron.so` used to satisfy
# the pin while the offline pack silently dropped the MPLC RT plugin — the very
# regression this row exists for (audit 2026-08-28, finding C3).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/lib_check.sh" || { echo "mplc-ota-deploy-contract: cannot source lib_check.sh"; exit 1; }

fails=0
fail() { printf 'mplc-ota-deploy-contract: FAIL  %s\n' "$*"; fails=$((fails + 1)); }
pass() { printf 'mplc-ota-deploy-contract: ok    %s\n' "$*"; }

for so in mplc_cyntron.so mplc_protocol_fast_modbus.so; do
    [ -f "firmware/mplc4/$so" ] && pass "firmware/mplc4/$so present in tree" \
        || fail "firmware/mplc4/$so missing"
done

stripped_has scripts/offline-update-allowlist.txt 'firmware/mplc4/mplc_cyntron.so' \
    && pass "offline allowlist includes mplc_cyntron.so" \
    || fail "offline allowlist missing mplc_cyntron.so"
stripped_has scripts/offline-update-allowlist.txt 'firmware/mplc4/mplc_protocol_fast_modbus.so' \
    && pass "offline allowlist includes mplc_protocol_fast_modbus.so" \
    || fail "offline allowlist missing mplc_protocol_fast_modbus.so"

python3 -c "
import json
m=json.load(open('scripts/offline-update-deploy-map.json',encoding='utf-8'))
r=m.get('etc_helper_renames') or {}
e=m.get('exact_rules') or {}
for k,d in [
    ('firmware/mplc4/mplc_cyntron.so','/opt/mplc4/mplc_cyntron.so'),
    ('firmware/mplc4/mplc_protocol_fast_modbus.so','/opt/mplc4/mplc_protocol_fast_modbus.so'),
]:
    got=(r.get(k) or e.get(k) or {}).get('dst')
    if got!=d: raise SystemExit(k+' -> '+str(got))
print('ok')
" >/dev/null 2>&1 \
    && pass "deploy-map maps firmware/mplc4 plugins to /opt/mplc4/" \
    || fail "deploy-map missing/wrong MPLC plugin destinations"

stripped_has etc/sa02m-update-runner.sh 'MPLC_OTA_PLUGINS' \
    && pass "update-runner defines MPLC_OTA_PLUGINS closed set" \
    || fail "update-runner missing MPLC_OTA_PLUGINS"
stripped_has etc/sa02m-update-runner.sh 'opt/mplc4/' \
    && pass "update-runner DST_RE allows /opt/mplc4/" \
    || fail "update-runner DST_RE missing /opt/mplc4/"
stripped_matches etc/sa02m-update-runner.sh '"mplc4"' \
    && pass "update-runner restarts mplc4 after apply" \
    || fail "update-runner does not restart mplc4 after plugin deploy"

stripped_has scripts/update-www-only.sh 'mplc_cyntron.so' \
    && pass "update-www-only installs mplc_cyntron.so" \
    || fail "update-www-only does not install mplc_cyntron.so"

stripped_has opt/sa02m-update/lib/validate_package.py 'opt/mplc4/(mplc_cyntron|mplc_protocol_fast_modbus)' \
    && pass "validate_package allows closed-set /opt/mplc4/*.so" \
    || fail "validate_package missing /opt/mplc4 plugin allow"

[ "$fails" = 0 ] || printf 'mplc-ota-deploy-contract: %s check(s) failed\n' "$fails"
exit "$fails"
