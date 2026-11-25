#!/usr/bin/env python3
"""Build deterministic Node-RED release bytes from the checked-in npm lock."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import urllib.request

NODE_VERSION = '22.23.5'
BUILD_NPM_VERSION = '10.9.9'
NODE_SHA256 = '1c3e15d38e4cafbbf74377ac070cd9d7e0418c673e1f6332f38f1d4762a699f9'
NODE_NAME = f'node-v{NODE_VERSION}-linux-armv7l.tar.xz'
NODE_URL = f'https://nodejs.org/dist/v{NODE_VERSION}/{NODE_NAME}'


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as f:
        while block := f.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


def inventory(root):
    entries, packages = [], []
    for path in sorted(root.rglob('*')):
        name = path.relative_to(root).as_posix()
        if name == '.sa02m-inventory.json':
            continue
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError(f'Escaping link: {name}')
            entries.append({'path': name, 'link': os.readlink(path)})
        elif path.is_file():
            with path.open('rb') as f:
                magic = f.read(4)
            if path.suffix == '.node' or magic in (b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf') or magic[:2] == b'MZ':
                raise ValueError(f'Native addon requires a target-ABI build: {name}')
            entries.append({'path': name, 'size': path.stat().st_size, 'sha256': sha(path), 'mode': path.stat().st_mode & 0o777})
            if path.name == 'package.json':
                data = json.loads(path.read_text())
                if isinstance(data.get('name'), str) and isinstance(data.get('version'), str):
                    packages.append({'path': name, 'name': data['name'], 'version': data['version'], 'engines': data.get('engines')})
    return entries, packages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--node-version', default=NODE_VERSION)
    parser.add_argument('--node-archive', type=Path)
    parser.add_argument('--allow-any-arch', action='store_true')
    parser.add_argument('--keep-build', action='store_true')
    args = parser.parse_args()
    if args.node_version.lstrip('v') != NODE_VERSION:
        raise ValueError('Node version must match the reviewed builder pin')
    if platform.machine() != 'armv7l' and not args.allow_any_arch:
        raise ValueError('Non-ARM build requires --allow-any-arch and separate ARM acceptance')
    for tool, expected in [('node', 'v' + NODE_VERSION), ('npm', BUILD_NPM_VERSION)]:
        if subprocess.check_output([tool, '--version'], text=True).strip() != expected:
            raise ValueError(f'Build tool mismatch: {tool} must be {expected}')
    repo = Path(__file__).resolve().parents[2]
    source = repo / 'vendor-src/nodered'
    manifest = json.loads((source / 'package.json').read_text())
    lock = json.loads((source / 'package-lock.json').read_text())
    version = manifest['dependencies']['node-red']
    if lock['packages']['node_modules/node-red']['version'] != version:
        raise ValueError('Node-RED source/lock mismatch')
    build, out = args.build_dir.resolve(), args.out.resolve()
    build.mkdir(parents=True, exist_ok=False)
    out.mkdir(parents=True, exist_ok=True)
    for name in ('package.json', 'package-lock.json'):
        shutil.copyfile(source / name, build / name)
    subprocess.run(['npm', 'ci', '--ignore-scripts', '--omit=optional', '--no-audit', '--no-fund'], cwd=build, check=True)
    modules, runtime = build / 'node_modules', build / 'node-red'
    (modules / 'node-red').rename(runtime)
    nested = runtime / 'node_modules'
    nested.mkdir(exist_ok=True)
    (modules / '.package-lock.json').unlink(missing_ok=True)
    for path in sorted(modules.iterdir()):
        target = nested / path.name
        if path.name.startswith('@') or path.name == '.bin':
            target.mkdir(exist_ok=True)
            for child in path.iterdir():
                if (target / child.name).exists() or (target / child.name).is_symlink():
                    raise ValueError(f'Hoist collision: {child}')
                child.rename(target / child.name)
        else:
            if target.exists() or target.is_symlink():
                raise ValueError(f'Hoist collision: {path}')
            path.rename(target)
    # Self-entry links point up twice after Node-RED becomes the archive root.
    for self_link in (nested / '.bin').iterdir():
        if self_link.is_symlink() and os.readlink(self_link).startswith('../node-red/'):
            new_target = '../../' + os.readlink(self_link)[len('../node-red/'):]
            self_link.unlink()
            self_link.symlink_to(new_target)
    for path in [runtime, *runtime.rglob('*')]:
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
    node_engines = json.loads((nested / 'npm/package.json').read_text())['engines']['node']
    provenance = {'source_lock_sha256': sha(source / 'package-lock.json'), 'source_manifest_sha256': sha(source / 'package.json'), 'node_engines': node_engines}
    write_json(runtime / '.sa02m-build.json', provenance)
    entries, packages = inventory(runtime)
    write_json(runtime / '.sa02m-inventory.json', entries)
    archive = out / f'node-red-{version}-{provenance["source_lock_sha256"][:12]}.tar.gz'
    with archive.open('wb') as stream, gzip.GzipFile(filename='', mode='wb', fileobj=stream, mtime=0) as gz, tarfile.open(fileobj=gz, mode='w', format=tarfile.PAX_FORMAT) as tf:
        for path in [runtime, *sorted(runtime.rglob('*'))]:
            info = tf.gettarinfo(str(path), arcname=str(path.relative_to(build)))
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ''
            info.pax_headers = {}
            if info.isfile():
                with path.open('rb') as f:
                    tf.addfile(info, f)
            else:
                tf.addfile(info)
    node_archive = out / NODE_NAME
    if args.node_archive:
        shutil.copyfile(args.node_archive, node_archive)
    else:
        with urllib.request.urlopen(NODE_URL, timeout=120) as src, node_archive.open('wb') as dst:
            size = 0
            while chunk := src.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise ValueError('Oversized Node download')
                dst.write(chunk)
    if sha(node_archive) != NODE_SHA256:
        raise ValueError('Node archive does not match reviewed SHA-256')
    shutil.copyfile(repo / 'etc/systemd/system/nodered.service', out / 'nodered.service')
    def entry(path, root, url):
        return {'filename': path.name, 'size': path.stat().st_size, 'sha256': sha(path), 'root': root, 'url': url}
    package = {'version': version, 'architecture': 'armv7l', 'node_version': NODE_VERSION, **provenance,
               'runtime': entry(archive, 'node-red', None),
               'node': entry(node_archive, f'node-v{NODE_VERSION}-linux-armv7l', NODE_URL),
               'unit': {'sha256': sha(out / 'nodered.service')},
               'inventory_sha256': sha(runtime / '.sa02m-inventory.json')}
    write_json(out / 'vendor-payloads.json', {'schema': 1, 'packages': {'node-red': package}})
    write_json(out / 'runtime-packages.json', packages)
    write_json(out / 'build-receipt.json', {'build_arch': platform.machine(), 'build_node': NODE_VERSION, 'build_npm': BUILD_NPM_VERSION, 'native_addons': [], 'package_copies': len(packages), 'archive_sha256': sha(archive), **provenance})
    print(json.dumps({'archive': str(archive), 'sha256': sha(archive), 'packages': len(packages)}))
    if not args.keep_build:
        shutil.rmtree(build)


if __name__ == '__main__':
    main()
