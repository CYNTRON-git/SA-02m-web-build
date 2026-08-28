#!/bin/bash
# ota-deploy-mode-contract — OTA manifest mode must follow destination role.
#
# Pins private/BUG-ota-mode-0644.md: extension-less helpers in /usr/local/sbin
# (sa02m-set-storage-auto-format, sa02m-set-cpu-profile) were deployed 0644
# because mode was derived from the source filename extension.
# The grep pins read COMMENT-STRIPPED text (lib_check.sh): `#mode = deploy_mode(
# rel, dst)` would otherwise satisfy the pin while the manifest builder went back
# to deriving the mode from the source extension — the 0644 sbin-helper bug.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/lib_check.sh" || { echo "ota-deploy-mode-contract: cannot source lib_check.sh"; exit 1; }

fails=0
fail() { printf 'ota-deploy-mode-contract: FAIL  %s\n' "$*"; fails=$((fails + 1)); }
pass() { printf 'ota-deploy-mode-contract: ok    %s\n' "$*"; }

RUNNER=etc/sa02m-update-runner.sh

stripped_has "$RUNNER" 'def deploy_mode' \
    && pass "update-runner defines deploy_mode(rel, dst)" \
    || fail "update-runner missing deploy_mode — mode must follow dst, not source extension"

stripped_has "$RUNNER" 'mode = deploy_mode(rel, dst)' \
    && pass "manifest builder calls deploy_mode(rel, dst)" \
    || fail "manifest builder still derives mode from rel.endswith only"

if stripped_matches "$RUNNER" 'mode = "0755" if.*rel\.endswith'; then
    fail "legacy rel.endswith-only mode derivation still present"
else
    pass "no legacy rel.endswith-only mode assignment in manifest builder"
fi

python3 -c "
import re, pathlib, sys
text = pathlib.Path('$RUNNER').read_text(encoding='utf-8')
m = re.search(r'^def deploy_mode\(.*?(?=^deploy = \[\])', text, re.M | re.S)
if not m:
    raise SystemExit('deploy_mode body not found')
ns = {}
exec(m.group(0), ns)
deploy_mode = ns['deploy_mode']
cases = [
    ('etc/sa02m-set-storage-auto-format', '/usr/local/sbin/sa02m-set-storage-auto-format', '0755'),
    ('etc/sa02m-set-cpu-profile', '/usr/local/sbin/sa02m-set-cpu-profile', '0755'),
    ('etc/sa02m-web-update-check.sh', '/usr/local/sbin/sa02m-web-update-check', '0755'),
    ('etc/sa02m-iface-conf-write.sh', '/usr/local/sbin/sa02m-iface-conf-write.sh', '0755'),
    ('etc/nginx/network_config.conf', '/etc/nginx/network_config.conf', '0644'),
    ('etc/sudoers.d/sa02m-www', '/etc/sudoers.d/sa02m-www', '0644'),
    ('www/network_config/cgi-bin/apply.cgi', '/var/www/network_config/cgi-bin/apply.cgi', '0755'),
    ('firmware/mplc4/mplc_cyntron.so', '/opt/mplc4/mplc_cyntron.so', '0755'),
]
for rel, dst, want in cases:
    got = deploy_mode(rel, dst)
    if got != want:
        raise SystemExit(f'{rel} -> {dst}: got {got}, want {want}')
print('ok')
" >/dev/null 2>&1 \
    && pass "deploy_mode: sbin helpers 0755, conf/sudoers 0644, cgi/so 0755" \
    || fail "deploy_mode cases failed (extension-less sbin helpers must be 0755)"

python3 -c "
import json
m=json.load(open('scripts/offline-update-deploy-map.json',encoding='utf-8'))
r=m.get('etc_helper_renames') or {}
for k in ('etc/sa02m-set-storage-auto-format','etc/sa02m-set-cpu-profile'):
    mode=(r.get(k) or {}).get('mode')
    if mode!='0755':
        raise SystemExit(k+' mode='+str(mode))
print('ok')
" >/dev/null 2>&1 \
    && pass "offline deploy-map keeps extension-less sbin helpers at 0755" \
    || fail "offline deploy-map wrong mode for extension-less sbin helpers"

[ "$fails" = 0 ] || printf 'ota-deploy-mode-contract: %s check(s) failed — see private/BUG-ota-mode-0644.md\n' "$fails"
exit "$fails"
