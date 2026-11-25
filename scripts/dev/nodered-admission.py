#!/usr/bin/env python3
# comment-mutation-proof-exempt: policy and archive coverage are exercised by test-nodered-admission.py and the official online advisory composition; comment-only changes do not alter admission behavior.
"""Admit exact Node-RED bytes using the official npm bulk advisory API."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import re

REPO = Path(__file__).resolve().parents[2]
POLICY = REPO / 'vendor-src/nodered/admission-policy.json'
LIMIT = 16 * 1024 * 1024


class AdmissionError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise AdmissionError(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    require(Path(path).stat().st_size <= LIMIT, 'input_size')
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def validate_policy(policy):
    require(policy['schema'] == 1 and policy['scope'] == 'node-red-release-payload' and policy['provider'] == 'npm', 'policy_schema')
    require(policy['block'] == ['HIGH', 'CRITICAL'] and policy['ttl_seconds'] == 86400, 'policy_threshold')
    require(policy['exceptions'] == [], 'exceptions_not_approved')
    require(bool(policy['lower_findings_owner']) and bool(policy['unscanned']), 'policy_disclosure')


def run_bounded(argv, out, err, timeout=180, cwd=None, clean_env=False):
    # Disk-backed bounded streams also bound scanner diagnostics on a failing network.
    with Path(out).open('wb') as stdout, Path(err).open('wb') as stderr:
        process = subprocess.Popen(argv, stdout=stdout, stderr=stderr, cwd=cwd,
                                   start_new_session=(os.name != 'nt'),
                                   env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'} if clean_env else None)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                require(time.monotonic() < deadline, 'scanner_timeout')
                require(Path(out).stat().st_size <= LIMIT and Path(err).stat().st_size <= LIMIT, 'scanner_output_limit')
                time.sleep(0.05)
            require(Path(out).stat().st_size <= LIMIT and Path(err).stat().st_size <= LIMIT, 'scanner_output_limit')
            return process.returncode
        finally:
            if process.poll() is None:
                if os.name != 'nt':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()


def tools_verified(policy, tool_dir, download=False):
    node = tool_dir / 'node'
    entry = policy['tools']['node']
    require(node.is_file() and not node.is_symlink(), 'tool_missing')
    require(node.stat().st_size == entry['size'] and sha(node) == entry['sha256'], 'tool_unverified')


def identity(package):
    require(isinstance(package, dict) and package.get('ecosystem') == 'npm', 'package_ecosystem')
    name, version = package.get('name'), package.get('version')
    require(isinstance(name, str) and bool(name) and isinstance(version, str) and bool(version), 'package_identity')
    return name, version


def collect_inputs(repo, artifact, policy):
    c = policy['coverage']
    release = read_json(repo / c['release_manifest'])['packages']['node-red']
    manifest, lock = repo / c['manifest'], repo / c['lock']
    require(sha(manifest) == release['source_manifest_sha256'] and sha(lock) == release['source_lock_sha256'], 'source_hash_mismatch')
    archive = artifact / release['runtime']['filename']
    require(archive.stat().st_size == release['runtime']['size'] and sha(archive) == release['runtime']['sha256'], 'artifact_hash_mismatch')
    inventory_file = artifact / c['inventory']
    inventory = read_json(inventory_file)
    require(isinstance(inventory, list) and len(inventory) == c['runtime_copies'] > 0, 'inventory_count')
    actual = []
    # The immutable reviewed archive digest is checked before reading any members.
    # No archive entry is extracted or executed here.
    with tarfile.open(archive, 'r:gz') as tf:
        count = total = 0
        seen = set()
        for member in tf:
            count += 1
            total += member.size
            require(count <= 60000 and total <= 640 * 1024 * 1024, 'archive_limit')
            require(member.name not in seen, 'archive_duplicate')
            seen.add(member.name)
            if member.isfile() and member.name.endswith('/package.json'):
                require(member.size <= LIMIT and member.name.startswith('node-red/'), 'package_path')
                data = json.load(tf.extractfile(member))
                if isinstance(data.get('name'), str) and isinstance(data.get('version'), str):
                    actual.append({'path': member.name[len('node-red/'):], 'name': data['name'],
                                   'version': data['version'], 'engines': data.get('engines')})
    require(sorted(actual, key=lambda x: x['path']) == sorted(inventory, key=lambda x: x['path']), 'artifact_inventory_mismatch')
    runtime = {}
    for package in inventory:
        key = identity(dict(package, ecosystem='npm'))
        runtime.setdefault(key, []).append('archive:' + package['path'])
    locked = {}
    lock_data = read_json(lock)
    count = 0
    for path, entry in lock_data['packages'].items():
        if not path:
            continue
        count += 1
        require('/node_modules/' in '/' + path and isinstance(entry.get('version'), str), 'lock_entry')
        name = entry.get('name') or path.rsplit('node_modules/', 1)[1]
        locked.setdefault((name, entry['version']), []).append(c['lock'] + ':' + path)
    require(count == c['lock_copies'] > 0 and runtime and locked, 'lock_coverage')
    # Bundles can also ship example package manifests absent from the npm lock.
    # Keep those in the artifact scan; never hide them to make lock coverage match.
    files = {'source_manifest': manifest, 'source_lock': lock, 'release_manifest': repo / c['release_manifest'],
             'archive': archive, 'inventory': inventory_file, 'policy': repo / 'vendor-src/nodered/admission-policy.json',
             'wrapper': repo / 'scripts/dev/nodered-admission.py'}
    digests = {name: sha(path) for name, path in files.items()}
    return digests, {'package-lock.json': locked, 'runtime-packages.json': runtime}, inventory


def build_query(expected):
    query = {}
    for source in expected.values():
        for name, version in source:
            query.setdefault(name, set()).add(version)
    require(query and all(query.values()), 'query_empty')
    return {name: sorted(versions) for name, versions in sorted(query.items())}


def validate_response(raw, query):
    require(isinstance(raw, dict) and set(raw).issubset(query), 'response_schema')
    count = 0
    for name, advisories in raw.items():
        require(isinstance(advisories, list), 'response_schema')
        seen = set()
        for advisory in advisories:
            require(isinstance(advisory, dict), 'advisory_schema')
            aid = advisory.get('id')
            require(type(aid) in (int, str) and bool(str(aid)) and str(aid) not in seen, 'advisory_identity')
            seen.add(str(aid))
            require(isinstance(advisory.get('vulnerable_versions'), str) and 0 < len(advisory['vulnerable_versions']) <= 2048, 'advisory_range')
            require(isinstance(advisory.get('url'), str) and advisory['url'].startswith('https://'), 'advisory_url')
            count += 1
    require(count <= 10000, 'advisory_limit')


def fetch_bulk(query, out, opener=urllib.request.urlopen):
    body = json.dumps(query, sort_keys=True, separators=(',', ':')).encode()
    (out / 'query.json').write_bytes(body)
    endpoint = 'https://registry.npmjs.org/-/npm/v1/security/advisories/bulk'
    request = urllib.request.Request(endpoint, data=body, headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    deadline = time.monotonic() + 90
    with opener(request, timeout=20) as response, (out / 'raw.json').open('wb') as dest:
        require(response.status == 200 and response.geturl() == endpoint, 'provider_http_failed')
        size = 0
        while chunk := response.read1(65536):
            size += len(chunk)
            require(size <= LIMIT and time.monotonic() < deadline, 'provider_response_limit')
            dest.write(chunk)
    require(time.monotonic() < deadline, 'provider_timeout')
    raw = read_json(out / 'raw.json')
    validate_response(raw, query)
    return raw


def semver_module(artifact, repo, out, policy):
    release = read_json(repo / policy['coverage']['release_manifest'])['packages']['node-red']
    archive = artifact / release['runtime']['filename']
    require(sha(archive) == release['runtime']['sha256'], 'artifact_hash_mismatch')
    root = Path(tempfile.mkdtemp(prefix='semver-', dir=out))
    prefix = 'node-red/node_modules/semver/'
    total = 0
    with tarfile.open(archive, 'r:gz') as tf:
        for member in tf:
            if not member.name.startswith(prefix) or member.isdir():
                continue
            name = member.name[len(prefix):]
            require(member.isfile() and name and not name.startswith('/') and '..' not in name.split('/'), 'semver_path')
            total += member.size
            require(total <= 5 * 1024 * 1024, 'semver_size')
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tf.extractfile(member).read())
    require(read_json(root / 'package.json')['version'] == policy['semver_version'], 'semver_version')
    return root


def range_matches(raw, query, node, module, out):
    cases = [{'name': name, 'id': str(a['id']), 'version': version, 'range': a['vulnerable_versions']}
             for name, advisories in raw.items() for a in advisories for version in query[name]]
    write_json(out / 'range-input.json', cases)
    # Upstream semver from the verified shipped archive owns all range semantics.
    script = "const fs=require('fs'),s=require(process.argv[1]); const a=JSON.parse(fs.readFileSync(process.argv[2])); const r=a.map(x=>{if(!s.valid(x.version)||!s.validRange(x.range))throw Error('invalid semver'); return s.satisfies(x.version,x.range)}); fs.writeFileSync(process.argv[3],JSON.stringify(r));"
    require(run_bounded([str(node), '-e', script, str(module), str(out / 'range-input.json'), str(out / 'range-output.json')],
                        out / 'semver.stdout', out / 'semver.stderr', 30, clean_env=True) == 0, 'semver_failed')
    matched = read_json(out / 'range-output.json')
    require(isinstance(matched, list) and len(matched) == len(cases) and all(type(v) is bool for v in matched), 'semver_output')
    return {(x['name'], x['version'], x['id']): value for x, value in zip(cases, matched)}


def normalize(raw, expected, policy, matches):
    query = build_query(expected)
    validate_response(raw, query)
    required = {(name, version, str(a['id'])) for name, advisories in raw.items() for a in advisories for version in query[name]}
    require(set(matches) == required and all(type(x) is bool for x in matches.values()), 'match_coverage')
    findings = []
    levels = {'critical': 'CRITICAL', 'high': 'HIGH', 'moderate': 'MODERATE', 'low': 'LOW', 'info': 'NONE'}
    for name, advisories in sorted(raw.items()):
        for a in advisories:
            affected = [version for version in query[name] if matches[(name, version, str(a['id']))]]
            require(affected, 'provider_range_mismatch')
            level = levels.get(a.get('severity'), 'UNKNOWN')
            alias = a['url'].rsplit('/', 1)[-1]
            aliases = ['npm:' + str(a['id'])] + ([alias] if re.fullmatch(r'GHSA-[a-z0-9-]+', alias) else [])
            for version in affected:
                paths = sorted({path for source in expected.values() for path in source.get((name, version), [])})
                require(paths, 'finding_without_path')
                findings.append({'package': name, 'version': version, 'ecosystem': 'npm', 'aliases': sorted(aliases),
                                 'severity': level, 'range': a['vulnerable_versions'], 'url': a['url'], 'paths': paths,
                                 'owner': policy['lower_findings_owner']})
    status = 'INDETERMINATE' if any(x['severity'] == 'UNKNOWN' for x in findings) else (
        'FAIL' if any(x['severity'] in policy['block'] for x in findings) else 'PASS')
    return status, findings


def freshness(report, digests, tool_hashes, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    start, finish = (dt.datetime.fromisoformat(report[k]) for k in ('scan_started', 'scan_finished'))
    require(start.tzinfo is not None and finish.tzinfo is not None, 'timestamp_timezone')
    require(start <= finish <= now and (now - start).total_seconds() <= 86400, 'scan_stale_or_future')
    require(report['inputs'] == digests and report['tools'] == tool_hashes, 'receipt_input_mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['scan', 'check'])
    parser.add_argument('--artifact-dir', type=Path, required=True)
    parser.add_argument('--tool-dir', type=Path, required=True, help='Directory containing the verified Node 22 x64 binary named node')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.tool_dir = args.tool_dir.resolve()
    args.artifact_dir = args.artifact_dir.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'provider': 'npm', 'status': 'INDETERMINATE'}
    try:
        policy = read_json(POLICY)
        validate_policy(policy)
        digests, expected, inventory = collect_inputs(REPO, args.artifact_dir, policy)
        tools_verified(policy, args.tool_dir)
        tool_hashes = {name: entry['sha256'] for name, entry in policy['tools'].items()}
        query = build_query(expected)
        module = semver_module(args.artifact_dir, REPO, args.out, policy)
        if args.action == 'scan':
            report.update({'scope': policy['scope'], 'policy': policy['id'], 'inputs': digests, 'tools': tool_hashes,
                           'unscanned': policy['unscanned'], 'exceptions': [], 'ttl_seconds': policy['ttl_seconds'],
                           'scan_started': utc(), 'runtime_copies': len(inventory), 'lock_copies': policy['coverage']['lock_copies'],
                           'submitted_names': len(query), 'submitted_versions': sum(map(len, query.values())),
                           'runtime_names': len({p['name'] for p in inventory}),
                           'sources': {name: {'identities': len(value), 'copies': sum(map(len, value.values()))} for name, value in expected.items()}})
            raw = fetch_bulk(query, args.out)
            matches = range_matches(raw, query, args.tool_dir / 'node', module, args.out)
            report.update({'scan_finished': utc(), 'raw_sha256': sha(args.out / 'raw.json'), 'query_sha256': sha(args.out / 'query.json')})
            report['status'], report['findings'] = normalize(raw, expected, policy, matches)
            write_json(args.out / 'admission.json', report)
        else:
            report = read_json(args.out / 'admission.json')
            require(report['provider'] == 'npm' and report['scope'] == policy['scope'] and report['policy'] == policy['id'], 'receipt_provider')
            freshness(report, digests, tool_hashes)
            require(report['raw_sha256'] == sha(args.out / 'raw.json') and report['query_sha256'] == sha(args.out / 'query.json'), 'response_hash_mismatch')
            require(read_json(args.out / 'query.json') == query, 'query_coverage_mismatch')
            raw = read_json(args.out / 'raw.json')
            validate_response(raw, query)
            matches = range_matches(raw, query, args.tool_dir / 'node', module, args.out)
            status, findings = normalize(raw, expected, policy, matches)
            require(status == report['status'] and findings == report['findings'], 'receipt_result_mismatch')
        print(json.dumps({'status': report['status'], 'provider': 'npm', 'scope': policy['scope'], 'report': str(args.out / 'admission.json')}))
        return 0 if report['status'] == 'PASS' else 1
    except (AdmissionError, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        report.update({'status': 'INDETERMINATE', 'error': str(exc)})
        write_json(args.out / ('admission.json' if args.action == 'scan' else 'check-failure.json'), report)
        print(json.dumps({'status': 'INDETERMINATE', 'provider': 'npm', 'error': str(exc)}))
        return 2


if __name__ == '__main__':
    result = main()
    if result == 0:
        raise SystemExit(0)
    if result == 1:
        raise SystemExit(1)
    raise SystemExit(2)
