#!/usr/bin/env python3
"""Package only committed dashboard files and their expected live base hashes."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ('www/network_config/cgi-bin/status.cgi', '/var/www/network_config/cgi-bin/status.cgi', '0755', 'www-data'),
    ('etc/sa02m-status-part.sh', '/usr/local/libexec/sa02m-status-part', '0755', 'root'),
    ('etc/sa02m-status-cache.py', '/usr/local/libexec/sa02m-status-cache.py', '0644', 'root'),
    ('www/network_config/cgi-bin/status_snapshot.cgi', '/var/www/network_config/cgi-bin/status_snapshot.cgi', '0755', 'www-data'),
    ('www/network_config/static/js/app/status.js', '/var/www/network_config/static/js/app/status.js', '0644', 'www-data'),
    ('www/network_config/static/js/i18n.js', '/var/www/network_config/static/js/i18n.js', '0644', 'www-data'),
    ('www/network_config/index.html', '/var/www/network_config/index.html', '0644', 'www-data'),
    ('etc/systemd/system/sa02m-status-cache.service', '/etc/systemd/system/sa02m-status-cache.service', '0644', 'root'),
)


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', default='HEAD')
    parser.add_argument('--base', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    ref = git('rev-parse', '--verify', '--end-of-options', args.ref+'^{commit}').decode().strip()
    base = git('rev-parse', '--verify', '--end-of-options', args.base+'^{commit}').decode().strip()
    git('merge-base', '--is-ancestor', base, ref)
    allowed = {row[0].encode() for row in FILES}
    changed = git('diff', '--name-only', '-z', '--no-renames', base, ref, '--', 'www').split(b'\0')
    outside = [path.decode('utf-8', errors='backslashreplace') for path in changed if path and path not in allowed]
    if outside:
        raise SystemExit('Changed WWW files outside fixed pilot whitelist: '+json.dumps(outside))
    existing = set(git('ls-tree', '-r', '--name-only', base).decode().splitlines())
    payload, runtime = {}, []
    for source, destination, mode, group in FILES:
        data = git('show', ref+':'+source)
        before = hashlib.sha256(git('show', base+':'+source)).hexdigest() if source in existing else None
        payload['payload/'+source] = data
        runtime.append({'source': 'payload/'+source, 'destination': destination, 'mode': mode,
                        'owner': 'root', 'group': group, 'sha256': hashlib.sha256(data).hexdigest(),
                        'expected_before_sha256': before})
    for source in ('scripts/status-cache-pilot-transaction.py',
                   'docs/contracts/status-snapshot-pilot.md'):
        payload['recipes/'+source] = git('show', ref+':'+source)
    manifest = {'schema': 1, 'kind': 'status-cache-pilot', 'commit': ref, 'base': base,
                'runtime': runtime,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}}
    payload['manifest.json'] = (json.dumps(manifest, indent=2)+'\n').encode()
    args.output.mkdir(parents=False, exist_ok=False)
    (args.output/'manifest.json').write_bytes(payload['manifest.json'])
    with tarfile.open(args.output/'status-cache-pilot.tar.gz', 'w:gz') as archive:
        for name, data in payload.items():
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), 0o644
            archive.addfile(entry, io.BytesIO(data))
    print(json.dumps({'commit': ref, 'base': base, 'runtime_files': len(runtime), 'output': str(args.output)}))


if __name__ == '__main__':
    main()
