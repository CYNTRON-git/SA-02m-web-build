#!/usr/bin/env python3
# comment-mutation-proof-exempt: exercises the QA adapter process boundary, exit/skip forwarding, Linux environment and rejects native wheel staging.
"""The adapter must not convert failures or partial coverage into success."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('mac_quality', ROOT/'scripts/dev/mac-quality.py')
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)


class AdapterTests(unittest.TestCase):
    def test_rejects_outside_artifact_path_before_creating_directories(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base/'repo'
            root.mkdir()
            outside = base/'outside'
            outside.mkdir()
            (root/'alias').symlink_to(outside, target_is_directory=True)
            for path in (outside/'direct'/'run', root/'alias'/'linked'/'run'):
                with self.subTest(path=path), patch.object(qa, 'ROOT', root), \
                        patch.object(qa.sys, 'platform', 'darwin'), \
                        patch.object(qa.sys, 'argv', ['mac-quality.py', '--artifacts', str(path)]):
                    with self.assertRaisesRegex(RuntimeError, 'Artifacts must be inside'):
                        qa.main()
                    self.assertEqual(list(outside.iterdir()), [])

    def test_review_browser_hosts_in_canonical_registry(self):
        rows = json.loads((ROOT/'.ai-dev/quality/tools.json').read_text())['tools']
        hosts = {row['id']: qa.host_for(row) for row in rows if row['beat'] == 'review'}
        for row_id in ('ui-layout', 'sh-modal-layout-smoke', 'cloud-card-smoke', 'homekit-card-smoke',
                       'homeconnect-card-smoke'):
            with self.subTest(row=row_id):
                self.assertEqual(hosts[row_id], 'mac')

    def test_python_browser_executes_registry_command_with_venv_on_path(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            python = base/'python3'
            python.write_text('#!/bin/sh\nexit 31\n')
            python.chmod(0o755)
            row = {'id': 'status-snapshot-browser', 'run': 'python3 custom.py || exit 42'}
            config = {'rows': [row], 'fixture_python': '/fixture/python',
                      'browser_python': str(python), 'root': name}
            path = base/'config.json'
            path.write_text(json.dumps(config))
            self.assertEqual(qa.execute(str(path), row['id']), 42)

    def test_browser_rows_without_inline_dependency_guard_use_mac(self):
        self.assertEqual(qa.host_for({'id':'hardware-capabilities-browser', 'run':'node scripts/dev/check.mjs'}), 'mac')
        self.assertEqual(qa.host_for({'id':'py-unit', 'run':'python3 -m unittest discover'}), 'linux')

    def test_native_failure_and_skip_are_forwarded(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            skip = base/'skip';skip.write_text('')
            for status in (0, 13, 77):
                row = {'id': 'fixture', 'run': 'echo skipped-case >> "$QUALITY_SKIP_LOG"; exit '+str(status)+' # playwright'}
                config = {'rows': [row], 'fixture_python': '/fixture/python', 'root': name}
                path = base/'config.json';path.write_text(json.dumps(config))
                with patch.dict(os.environ, {'QUALITY_SKIP_LOG': str(skip)}):
                    self.assertEqual(qa.execute(str(path), 'fixture'), status)
                record = json.loads((base/'rows.jsonl').read_text().splitlines()[-1])
                self.assertEqual(record['exit'], status)
                self.assertIn('skipped-case', record['skipped_checks'])
                self.assertEqual(record['original_command'], row['run'])

    def test_summary_uses_only_canonical_verdicts_including_strict_skips(self):
        rows = [
            {'id': 'good', 'beat': 'build', 'run': 'true'},
            {'id': 'part', 'beat': 'build', 'run': 'echo skipped >> "$QUALITY_SKIP_LOG"'},
            {'id': 'missing', 'beat': 'build', 'run': 'exit 77'},
            {'id': 'bad', 'beat': 'build', 'run': 'exit 1'},
            {'id': 'noise', 'beat': 'build',
             'run': 'printf "PASS  all fixture checks complete\\nFAIL  good\\nPASS  unrelated\\n"'},
            {'id': 'unrelated', 'beat': 'review', 'run': 'false'},
        ]
        for strict in (False, True):
            with self.subTest(strict=strict), tempfile.TemporaryDirectory() as name:
                base = Path(name)
                registry = base/'registry.json'
                registry.write_text(json.dumps({'tools': rows}))
                with patch.dict(os.environ, {'QUALITY_STRICT': str(int(strict))}), redirect_stdout(io.StringIO()):
                    code, statuses = qa.run_registry('build', None, registry, base)
                self.assertEqual(code, 1)
                self.assertEqual(statuses, {'good': 'PASS', 'part': 'FAIL' if strict else 'PARTIAL',
                                            'missing': 'FAIL' if strict else 'SKIP',
                                            'bad': 'FAIL', 'noise': 'PASS'})

    def test_linux_launch_passes_fixture_python_and_same_skip_log(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            shim = base/'docker'
            shim.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$QA_ARGS"\nexit 19\n')
            shim.chmod(0o755)
            arguments = base/'args'
            row = {'id': 'fixture', 'run': 'python3 unmodified-test.py'}
            config = {'rows': [row], 'root': name, 'context': 'fixture-context', 'image': 'fixture-image',
                      'git_common': name+'/git', 'deps': name+'/deps', 'pth': name+'/deps.pth', 'python_site': '/site'}
            path = base/'config.json';path.write_text(json.dumps(config))
            with patch.dict(os.environ, {'PATH': name+os.pathsep+os.environ['PATH'], 'QA_ARGS': str(arguments), 'QUALITY_SKIP_LOG': str(base/'skip')}):
                self.assertEqual(qa.execute(str(path), 'fixture'), 19)
            argv = arguments.read_text().splitlines()
            self.assertIn('--init', argv[argv.index('run') + 1:argv.index(config['image'])])
            self.assertIn('--pull=never', argv[argv.index('run') + 1:argv.index(config['image'])])
            self.assertNotIn('--cap-add=SYS_PTRACE', argv, 'ordinary rows must retain default capabilities')
            self.assertIn('QUALITY_SKIP_LOG', argv)
            self.assertIn('PYTHON=python3', argv)
            self.assertIn(name+'/deps.pth:/site/sa02m-qa.pth:ro', argv)
            self.assertEqual(argv[-3:], ['bash', '-c', row['run']])

    def test_root_fd_fixture_gets_only_namespaced_ptrace_capability(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            shim = base/'docker'
            shim.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$QA_ARGS"\nexit 0\n')
            shim.chmod(0o755)
            arguments = base/'args'
            row = {'id': 'service-operations', 'run': 'python3 scripts/dev/test-service-operations.py'}
            config = {'rows': [row], 'root': name, 'context': 'fixture-context', 'image': 'fixture-image',
                      'git_common': name+'/git', 'deps': name+'/deps', 'pth': name+'/deps.pth', 'python_site': '/site'}
            path = base/'config.json'
            path.write_text(json.dumps(config))
            with patch.dict(os.environ, {'PATH': name+os.pathsep+os.environ['PATH'], 'QA_ARGS': str(arguments)}):
                self.assertEqual(qa.execute(str(path), row['id']), 0)
            argv = arguments.read_text().splitlines()
            options = argv[argv.index('run')+1:argv.index(config['image'])]
            self.assertEqual([arg for arg in options if arg.startswith('--cap-add')], ['--cap-add=SYS_PTRACE'])
            self.assertFalse(any(arg.startswith(('--privileged', '--pid', '--userns')) for arg in options), options)
            self.assertEqual(argv[-3:], ['bash', '-c', row['run']])


    def test_rs485_fd_row_has_only_private_namespace_capability(self):
        for row_id, expected in (('rs485-inuse-batch', ['--cap-add=SYS_PTRACE']),
                                 ('rs485-inuse-batch-unexpected', [])):
            with self.subTest(row=row_id), tempfile.TemporaryDirectory() as name:
                base = Path(name)
                shim = base/'docker'
                shim.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$QA_ARGS"\nexit 0\n')
                shim.chmod(0o755)
                arguments = base/'args'
                row = {'id': row_id, 'run': 'sudo -n python3 scripts/dev/test-rs485-inuse-batch.py'}
                config = {'rows': [row], 'root': name, 'context': 'fixture-context', 'image': 'fixture-image',
                          'git_common': name+'/git', 'deps': name+'/deps', 'pth': name+'/deps.pth', 'python_site': '/site'}
                path = base/'config.json'; path.write_text(json.dumps(config))
                with patch.dict(os.environ, {'PATH': name+os.pathsep+os.environ['PATH'], 'QA_ARGS': str(arguments)}):
                    self.assertEqual(qa.execute(str(path), row_id), 0)
                argv = arguments.read_text().splitlines()
                options = argv[argv.index('run')+1:argv.index(config['image'])]
                self.assertEqual([arg for arg in options if arg.startswith('--cap-add')], expected)
                self.assertFalse(any(arg.startswith(('--privileged', '--pid', '--userns')) for arg in options), options)
                self.assertEqual(argv[-3:], ['bash', '-c', row['run']])

    def test_non_pure_wheel_cannot_enter_linux_dependency_overlay(self):
        class Native:
            metadata = {'Name': 'native'}
            def read_text(self, _):
                return 'Root-Is-Purelib: false\n'
        with tempfile.TemporaryDirectory() as name, patch('importlib.metadata.distribution', return_value=Native()):
            with self.assertRaisesRegex(RuntimeError, 'non-pure'):
                qa.stage_dependencies(Path(name))


if __name__ == '__main__':
    unittest.main()
