#!/usr/bin/env python3
"""Run the canonical quality registry using existing Mac and Linux tools."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def host_for(row):
    if (row['id'] in ('ui-layout', 'sh-modal-layout-smoke') or
            row['id'].endswith(('-browser', '-card-smoke')) or 'playwright' in row['run']):
        return 'mac'
    return 'linux'


def stage_dependencies(target):
    """Copy only installed pure Python wheels; never expose Mac extension modules."""
    from importlib.metadata import distribution
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    pending = ['paho-mqtt', 'pytest', 'openpyxl']
    seen, versions = set(), {}
    environment = {**default_environment(), 'sys_platform': 'linux', 'platform_system': 'Linux', 'extra': ''}
    while pending:
        name = pending.pop()
        dist = distribution(name)
        canonical = dist.metadata['Name'].lower().replace('_', '-')
        if canonical in seen:
            continue
        seen.add(canonical)
        if 'Root-Is-Purelib: true' not in (dist.read_text('WHEEL') or ''):
            raise RuntimeError('Refusing non-pure dependency: '+name)
        versions[canonical] = dist.version
        site = Path(dist.locate_file('')).resolve()
        for entry in dist.files or []:
            source = Path(dist.locate_file(entry)).resolve()
            if not source.is_relative_to(site) or not source.is_file() or source.suffix == '.pyc':
                continue
            if source.suffix in ('.so', '.dylib', '.pyd'):
                raise RuntimeError('Refusing native dependency file: '+str(source))
            dest = target/source.relative_to(site)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        for requirement in dist.requires or []:
            dep = Requirement(requirement)
            if dep.marker is None or dep.marker.evaluate(environment):
                pending.append(dep.name)
    (target/'versions.json').write_text(json.dumps(versions, indent=2))
    return versions


def execute(config_path, row_id):
    config = json.loads(Path(config_path).read_text())
    row = next(r for r in config['rows'] if r['id'] == row_id)
    environment = os.environ.copy()
    environment['PATH'] = '/opt/homebrew/opt/coreutils/libexec/gnubin:'+environment['PATH']
    if host_for(row) == 'mac':
        environment['PYTHON'] = config['fixture_python']
        if row_id == 'status-snapshot-browser':
            environment['PATH'] = str(Path(config['browser_python']).parent) + os.pathsep + environment['PATH']
        command = ['/bin/sh', '-c', row['run']]
        # Matching Playwright's installed browser, with no revision aliases or preloader.
        environment.pop('NODE_OPTIONS', None)
    else:
        command = ['docker', '--context', config['context'], 'run', '--rm', '--init', '--pull=never',
                   '-v', config['root']+':'+config['root'],
                   '-v', config['git_common']+':'+config['git_common']+':ro',
                   '-v', config['deps']+':/opt/sa02m-qa-deps:ro',
                   '-v', config['pth']+':'+config['python_site']+'/sa02m-qa.pth:ro',
                   '-e', 'QUALITY_SKIP_LOG', '-e', 'PYTHON=python3',
                   '-e', 'GIT_CONFIG_COUNT=1', '-e', 'GIT_CONFIG_KEY_0=safe.directory',
                   '-e', 'GIT_CONFIG_VALUE_0='+config['root'],
                   '-w', config['root'], config['image'], 'bash', '-c', row['run']]
    started = time.monotonic()
    result = subprocess.run(command, env=environment, cwd=config['root'])
    skip = Path(environment['QUALITY_SKIP_LOG']).read_text() if environment.get('QUALITY_SKIP_LOG') and Path(environment['QUALITY_SKIP_LOG']).exists() else ''
    record = {'id': row_id, 'host': host_for(row), 'original_command': row['run'],
              'exit': result.returncode, 'seconds': round(time.monotonic()-started, 3), 'skipped_checks': skip.splitlines()}
    with (Path(config_path).parent/'rows.jsonl').open('a') as out:
        out.write(json.dumps(record)+'\n')
    return result.returncode


def output(command, **kwargs):
    return subprocess.check_output(command, text=True, **kwargs).strip()


def run_registry(beat, touched, registry, artifacts):
    launcher = artifacts/'run.mjs'
    # The runner uses console for verdicts and stdout.write for row output.
    # Keep a runner-only transcript: a fixture may print its own PASS lines.
    launcher.write_text('import {run,computeTouchedFiles} from ' +
                        json.dumps((ROOT/'.ai-dev/quality/run.mjs').as_uri()) + ';\n' + """
import { Console } from 'node:console';
import { createWriteStream } from 'node:fs';
const [root, registry, beat, scope, transcript] = process.argv.slice(2);
const report = new Console(createWriteStream(transcript));
for (const level of ['log', 'error']) {
    const emit = console[level].bind(console);
    console[level] = (...args) => { report[level](...args); emit(...args); };
}
const base = JSON.parse(scope);
const meta = {};
const files = base === null ? null : computeTouchedFiles(root, base || null, meta);
console.log("SCOPE", JSON.stringify(meta));
process.exitCode = run(beat, root, registry, files);
""")
    transcript = artifacts/'runner.log'
    command = ['node', str(launcher), str(ROOT), str(registry), beat, json.dumps(touched), str(transcript)]
    environment = {**os.environ, 'TMPDIR': str(artifacts)}
    with (artifacts/'quality.log').open('w') as log, subprocess.Popen(
        command, cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    ) as process:
        for line in process.stdout:
            print(line, end='', flush=True)
            log.write(line)
            log.flush()
        code = process.wait()
    statuses = {}
    for line in transcript.read_text().splitlines():
        match = re.match(r'^(PASS|FAIL|SKIP|PARTIAL)  (\S+)', line)
        if match:
            statuses[match[2]] = match[1]
    return code, statuses


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('beat', nargs='?', choices=['build', 'review', 'ship'], default='build')
    parser.add_argument('--touched', nargs='?', const='')
    parser.add_argument('--build-root', type=Path, default=Path.home()/'cyntron-build')
    parser.add_argument('--context', default='colima-cyntron')
    parser.add_argument('--image', default='cyntron-build:sa-native-quality')
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--execute', nargs=2, metavar=('CONFIG', 'ROW'))
    parser.add_argument('--stage-deps', type=Path)
    args = parser.parse_args()
    if args.stage_deps:
        stage_dependencies(args.stage_deps)
        return 0
    if args.execute:
        return execute(*args.execute)
    if sys.platform != 'darwin':
        raise RuntimeError('This adapter needs macOS; use the canonical run.mjs on Linux')
    artifacts = (args.artifacts or ROOT/'.tmp'/('mac-quality-'+time.strftime('%Y%m%d-%H%M%S'))).resolve()
    if not artifacts.is_relative_to(ROOT):
        raise RuntimeError('Artifacts must be inside the repository so Linux can write the shared skip log')
    build = args.build_root.expanduser().resolve()
    browser_python = build/'venvs/nms-edge/bin/python'
    fixture_python = build/'venvs/firmware/bin/python'
    for executable in (browser_python, fixture_python):
        if not executable.is_file():
            raise RuntimeError('Missing configured interpreter: '+str(executable))
    artifacts.mkdir(parents=True, exist_ok=False)
    deps = artifacts/'dependencies';deps.mkdir()
    subprocess.run([str(browser_python), str(Path(__file__).resolve()), '--stage-deps', str(deps)], check=True)
    pth = artifacts/'sa02m-qa.pth';pth.write_text('/opt/sa02m-qa-deps\n')
    docker = ['docker', '--context', args.context]
    python_site = output(docker+['run', '--rm', '--pull=never', args.image, 'python3', '-c', 'import site;print(site.getsitepackages()[0])'])
    rows = json.loads((ROOT/'.ai-dev/quality/tools.json').read_text())['tools']
    config = {'root': str(ROOT), 'rows': rows, 'context': args.context, 'image': args.image,
              'git_common': output(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], cwd=ROOT),
              'deps': str(deps), 'pth': str(pth), 'python_site': python_site,
              'browser_python': str(browser_python), 'fixture_python': str(fixture_python)}
    config_path = artifacts/'config.json';config_path.write_text(json.dumps(config, indent=2))
    # Only transport changes. Scope matching, PASS/PARTIAL/SKIP and exit code stay
    # owned by the existing canonical runner; the source registry is not rewritten.
    adapted = [{**row, 'run': shlex.join([sys.executable, str(Path(__file__).resolve()), '--execute', str(config_path), row['id']])} for row in rows]
    registry = artifacts/'registry.json';registry.write_text(json.dumps({'tools': adapted}, indent=2))
    tools = {'node': output(['node', '--version']), 'image': output(docker+['image', 'inspect', '--format', '{{.Id}}', args.image]),
             'node_chromium': output(['node', '-e', "const p=require('./scripts/dev/node_modules/playwright');console.log(p.chromium.executablePath())"], cwd=ROOT),
             'python_chromium': output([str(browser_python), '-c', 'from playwright.sync_api import sync_playwright\nwith sync_playwright() as p: print(p.chromium.executable_path)']),
             'python_dependencies': json.loads((deps/'versions.json').read_text())}
    for key in ('node_chromium', 'python_chromium'):
        tools[key+'_version'] = output([tools[key], '--version'])
    (artifacts/'tools.json').write_text(json.dumps(tools, indent=2))
    print('QA artifacts: '+str(artifacts), flush=True)
    code, statuses = run_registry(args.beat, args.touched, registry, artifacts)
    (artifacts/'summary.json').write_text(json.dumps({'exit': code, 'statuses': statuses, 'tools': tools}, indent=2))
    return code


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print('mac-quality: '+str(error), file=sys.stderr)
        sys.exit(1)
