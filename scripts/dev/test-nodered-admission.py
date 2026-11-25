#!/usr/bin/env python3
# comment-mutation-proof-exempt: executes real coverage, process limits and severity policy with negative fixtures; assertions are behavioral and independent of source comments.
"""Isolated admission policy, coverage and fail-closed regressions."""
import copy
import datetime as dt
import importlib.util
import io
from unittest import mock
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('admission', Path(__file__).with_name('nodered-admission.py'))
admission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admission)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.policy = admission.read_json(admission.POLICY)
        self.expected = {'package-lock.json': {('example', '1.0.0'): ['lock:example']},
                         'runtime-packages.json': {('example', '1.0.0'): ['archive:example', 'archive:npm/node_modules/example']}}

    def raw(self, level='moderate'):
        return {'example': [{'id': 123, 'severity': level, 'vulnerable_versions': '<2.0.0',
                             'url': 'https://github.com/advisories/GHSA-test-test-test'}]}

    def matches(self, value=True):
        return {('example', '1.0.0', '123'): value}

    def test_successful_empty_response_and_all_severities(self):
        self.assertEqual(admission.normalize({}, self.expected, self.policy, {}), ('PASS', []))
        for severity, status in [('critical', 'FAIL'), ('high', 'FAIL'), ('moderate', 'PASS'), ('low', 'PASS'),
                                 ('info', 'PASS'), ('unknown', 'INDETERMINATE'), (None, 'INDETERMINATE')]:
            with self.subTest(severity=severity):
                actual, rows = admission.normalize(self.raw(severity), self.expected, self.policy, self.matches())
                self.assertEqual(actual, status)
                self.assertEqual(len(rows), 1)

    def test_nested_copies_aliases_and_lower_owner_are_retained(self):
        status, rows = admission.normalize(self.raw(), self.expected, self.policy, self.matches())
        self.assertEqual(status, 'PASS')
        self.assertEqual(len(rows[0]['paths']), 3)
        self.assertIn('npm:123', rows[0]['aliases'])
        self.assertIn('GHSA-test-test-test', rows[0]['aliases'])
        self.assertEqual(rows[0]['owner'], self.policy['lower_findings_owner'])

    def test_query_covers_all_sources_versions_and_nested_copies(self):
        expected = copy.deepcopy(self.expected)
        expected['runtime-packages.json'][('nested', '2.0.0')] = ['archive:nested']
        expected['package-lock.json'][('example', '2.0.0')] = ['lock:example2']
        self.assertEqual(admission.build_query(expected), {'example': ['1.0.0', '2.0.0'], 'nested': ['2.0.0']})
        with self.assertRaisesRegex(admission.AdmissionError, 'query_empty'):
            admission.build_query({})

    def test_unknown_package_and_partial_schema_refuse(self):
        query = admission.build_query(self.expected)
        bad = [[], {'unrequested': []}, {'example': {}}, {'example': [{}]},
               {'example': [{'id': 1, 'vulnerable_versions': '', 'url': 'https://example.test'}]},
               {'example': [{'id': 1, 'vulnerable_versions': '*', 'url': 'http://example.test'}]}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(admission.AdmissionError):
                admission.validate_response(value, query)

    def test_duplicate_advisory_and_missing_match_refuse(self):
        raw = self.raw()
        raw['example'].append(copy.deepcopy(raw['example'][0]))
        with self.assertRaisesRegex(admission.AdmissionError, 'advisory_identity'):
            admission.normalize(raw, self.expected, self.policy, self.matches())
        for matches in ({}, {('unexpected', '1', '123'): True}):
            with self.subTest(matches=matches), self.assertRaisesRegex(admission.AdmissionError, 'match_coverage'):
                admission.normalize(self.raw(), self.expected, self.policy, matches)

    def test_provider_range_mismatch_refuses(self):
        with self.assertRaisesRegex(admission.AdmissionError, 'provider_range_mismatch'):
            admission.normalize(self.raw(), self.expected, self.policy, self.matches(False))

    def test_fixed_version_is_not_reported_as_vulnerable(self):
        expected = copy.deepcopy(self.expected)
        expected['runtime-packages.json'][('example', '2.0.0')] = ['archive:fixed']
        matches = self.matches()
        matches[('example', '2.0.0', '123')] = False
        status, rows = admission.normalize(self.raw('high'), expected, self.policy, matches)
        self.assertEqual(status, 'FAIL')
        self.assertEqual([x['version'] for x in rows], ['1.0.0'])

    def test_http_errors_partial_json_and_size_limit_refuse(self):
        query = admission.build_query(self.expected)
        endpoint = 'https://registry.npmjs.org/-/npm/v1/security/advisories/bulk'
        class Response(io.BytesIO):
            def __init__(self, body, status=200, url=endpoint):
                super().__init__(body)
                self.status, self.url = status, url
            def geturl(self):
                return self.url
        for body, status, url in [(b'{}', 503, endpoint), (b'{}', 200, 'https://other.example'),
                                  (b'{', 200, endpoint), (b'x' * (admission.LIMIT + 1), 200, endpoint)]:
            with self.subTest(status=status, url=url), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises((admission.AdmissionError, ValueError)):
                    admission.fetch_bulk(query, Path(tmp), lambda *_a, **_k: Response(body, status, url))
        with tempfile.TemporaryDirectory() as tmp:
            def outage(*_a, **_k):
                raise OSError('offline')
            with self.assertRaises(OSError):
                admission.fetch_bulk(query, Path(tmp), outage)
            self.assertEqual(admission.read_json(Path(tmp) / 'query.json'), query)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(admission.fetch_bulk(query, Path(tmp), lambda *_a, **_k: Response(b'{}')), {})

    def test_malformed_json_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'raw.json'
            p.write_text('{"results": [')
            with self.assertRaises(json.JSONDecodeError):
                admission.read_json(p)

    def test_no_implicit_or_expired_exceptions(self):
        admission.validate_policy(self.policy)
        for entry in [{'package': '*'}, {'severity': 'HIGH'}, {'id': 'GHSA-test', 'expiry': '2000-01-01'}]:
            policy = copy.deepcopy(self.policy)
            policy['exceptions'] = [entry]
            with self.assertRaisesRegex(admission.AdmissionError, 'exceptions_not_approved'):
                admission.validate_policy(policy)

    def test_threshold_change_requires_policy_change(self):
        self.policy['block'] = ['CRITICAL']
        with self.assertRaisesRegex(admission.AdmissionError, 'policy_threshold'):
            admission.validate_policy(self.policy)

    def test_freshness_input_and_tool_identity(self):
        now = dt.datetime.now(dt.timezone.utc)
        report = {'scan_started': (now - dt.timedelta(seconds=1)).isoformat(), 'scan_finished': now.isoformat(),
                  'inputs': {'archive': 'sha'}, 'tools': {'scanner': 'sha'}}
        admission.freshness(report, report['inputs'], report['tools'], now)
        for mode in ('stale', 'future', 'reversed', 'input', 'tool'):
            candidate = copy.deepcopy(report)
            if mode == 'stale':
                candidate['scan_started'] = (now - dt.timedelta(hours=25)).isoformat()
            elif mode == 'future':
                candidate['scan_finished'] = (now + dt.timedelta(seconds=1)).isoformat()
            elif mode == 'reversed':
                candidate['scan_started'] = (now + dt.timedelta(seconds=1)).isoformat()
            else:
                candidate['inputs' if mode == 'input' else 'tools'] = {}
            with self.subTest(mode=mode), self.assertRaises(admission.AdmissionError):
                admission.freshness(candidate, report['inputs'], report['tools'], now)

    def test_process_timeout_and_output_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err = Path(tmp) / 'out', Path(tmp) / 'err'
            with self.assertRaisesRegex(admission.AdmissionError, 'scanner_timeout'):
                admission.run_bounded([sys.executable, '-c', 'import time; time.sleep(5)'], out, err, 0.05)
            with self.assertRaisesRegex(admission.AdmissionError, 'scanner_output_limit'):
                admission.run_bounded([sys.executable, '-c', 'print("x" * 17000000)'], out, err)

    def test_unverified_tool_is_never_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp)
            (tools / 'node').write_text('unverified')
            with self.assertRaisesRegex(admission.AdmissionError, 'tool_unverified'):
                admission.tools_verified(self.policy, tools)

    def fixture_inputs(self, base):
        repo, artifact = base / 'repo', base / 'payload'
        source = repo / 'vendor-src/nodered'
        source.mkdir(parents=True)
        (repo / 'scripts/dev').mkdir(parents=True)
        (repo / 'etc').mkdir()
        artifact.mkdir()
        policy = copy.deepcopy(self.policy)
        policy['coverage']['runtime_copies'] = policy['coverage']['lock_copies'] = 1
        admission.write_json(source / 'admission-policy.json', policy)
        (repo / 'scripts/dev/nodered-admission.py').write_text('fixture')
        package = {'name': 'example', 'version': '1.0.0'}
        admission.write_json(source / 'package.json', package)
        admission.write_json(source / 'package-lock.json', {'packages': {'node_modules/example': {'version': '1.0.0'}}})
        archive = artifact / 'runtime.tar.gz'
        with tarfile.open(archive, 'w:gz') as tf:
            data = json.dumps(package).encode()
            entry = tarfile.TarInfo('node-red/package.json')
            entry.size = len(data)
            tf.addfile(entry, io.BytesIO(data))
        admission.write_json(artifact / 'runtime-packages.json', [dict(package, path='package.json', engines=None)])
        admission.write_json(repo / 'etc/vendor-payloads.json', {'packages': {'node-red': {
            'source_manifest_sha256': admission.sha(source / 'package.json'),
            'source_lock_sha256': admission.sha(source / 'package-lock.json'),
            'runtime': {'filename': archive.name, 'size': archive.stat().st_size, 'sha256': admission.sha(archive)}}}})
        return repo, artifact, policy

    def test_inventory_is_derived_from_archive_and_source_hashes(self):
        for mode in ('good', 'source', 'archive', 'inventory', 'empty', 'missing'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                repo, artifact, policy = self.fixture_inputs(Path(tmp))
                if mode == 'source':
                    (repo / 'vendor-src/nodered/package-lock.json').write_text('{}')
                elif mode == 'archive':
                    (artifact / 'runtime.tar.gz').write_bytes(b'changed')
                elif mode == 'inventory':
                    admission.write_json(artifact / 'runtime-packages.json', [{'name': 'fake', 'version': '1', 'path': 'package.json', 'engines': None}])
                elif mode == 'empty':
                    admission.write_json(artifact / 'runtime-packages.json', [])
                elif mode == 'missing':
                    (artifact / 'runtime-packages.json').unlink()
                if mode == 'good':
                    digests, expected, inventory = admission.collect_inputs(repo, artifact, policy)
                    self.assertEqual(len(digests), 7)
                    self.assertEqual(len(inventory), 1)
                    self.assertEqual(set(expected), {'package-lock.json', 'runtime-packages.json'})
                else:
                    with self.assertRaises((admission.AdmissionError, OSError)):
                        admission.collect_inputs(repo, artifact, policy)


if __name__ == '__main__':
    unittest.main()
