#!/bin/bash
# Cross-check the single locked payload and fixed installer wiring.
# Real compatibility/activation/rollback cases live in the vendor installer suite.
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
python3 - <<'PY'
import ast,hashlib,json,re
from pathlib import Path
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
package=json.loads(Path('vendor-src/nodered/package.json').read_text())
lock=json.loads(Path('vendor-src/nodered/package-lock.json').read_text())
release=json.loads(Path('etc/vendor-payloads.json').read_text())['packages']['node-red']
version=package['dependencies']['node-red']
assert re.fullmatch(r'\d+\.\d+\.\d+',version)
assert version==lock['packages']['node_modules/node-red']['version']==release['version']
assert release['source_lock_sha256']==sha('vendor-src/nodered/package-lock.json')
assert release['source_manifest_sha256']==sha('vendor-src/nodered/package.json')
assert release['unit']['sha256']==sha('etc/systemd/system/nodered.service')
builder=ast.parse(Path('scripts/dev/build-nodered-payload.py').read_text())
constants={n.targets[0].id:ast.literal_eval(n.value) for n in builder.body if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id in ('NODE_VERSION','NODE_SHA256')}
assert release['node_version']==constants['NODE_VERSION']
assert release['node']['sha256']==constants['NODE_SHA256']
assert any(isinstance(n,ast.List) and [v.value for v in n.elts if isinstance(v,ast.Constant)]==['npm','ci','--ignore-scripts','--omit=optional','--no-audit','--no-fund'] for n in ast.walk(builder)), 'locked pure-JS npm ci missing'
assert release['node_engines']==lock['packages']['node_modules/npm']['engines']['node']
for name in ('etc/sa02m-web-service-ctl.sh','scripts/07-nodered.sh'):
    code='\n'.join(l for l in Path(name).read_text().splitlines() if not l.lstrip().startswith('#'))
    assert '/usr/local/lib/sa02m-vendor-install.py' in code, name
    assert '/usr/bin/python3 -I ' in code and 'install node-red' in code, name
    assert '--node22' not in code and 'update-nodejs-and-nodered' not in code, name
helper=ast.parse(Path('etc/sa02m-vendor-install.py').read_text())
strings=[n.value for n in ast.walk(helper) if isinstance(n,ast.Constant) and isinstance(n.value,str)]
assert any('Started flows' in s and 'Запущены потоки' in s for s in strings), 'flow-level bilingual verdict missing'
unit='\n'.join(l for l in Path('etc/systemd/system/nodered.service').read_text().splitlines() if not l.lstrip().startswith('#'))
assert re.search(r'^Environment=LC_ALL=',unit,re.M)
assert re.search(r'^User=nodered$',unit,re.M)
assert re.search(r'^ExecStart=/[^\n]* /usr/lib/node_modules/node-red/red.js',unit,re.M)
assert '/usr/bin/env' not in unit and 'env node' not in unit
print('nodered-pin-consistency: locked source/manifest/Node/unit/installer agreement PASS')
PY
