"""Replay released OTA mapping and real offline packing into a private root.

Imported by test_event_journal_delivery.py; no installer, incoming runner,
system manager or live absolute destination is executed.
"""

import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "5e367530"


def module(name, path, package=False):
    spec = importlib.util.spec_from_file_location(name, path,
        submodule_search_locations=[str(path.parent)] if package else None)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


def candidate_payload(folder):
    overlay = folder / "overlay"
    paths = list((ROOT / "opt/sa02m-devices").rglob("*.py"))
    paths += [ROOT / "etc/systemd" / (name + ".service") for name in
              ("sa02m-devices-api", "sa02m-devices-logger")]
    paths += [ROOT / "etc/systemd/system/sa02m-stand-api.service.d/30-event-journal.conf"]
    paths += [ROOT / "etc/systemd/system/sa02m-stand-api.service.d/20-devices-socket-group.conf"]
    for source in paths:
        destination = overlay / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return overlay


def github_manifest(overlay, folder):
    # Immutable historical producer input, including map and service list.
    # Provenance and exact extraction range live beside the fixture.
    raw = (ROOT / "scripts/dev/fixtures/event-journal-github-generator-5e367530.py").read_bytes()
    if hashlib.sha256(raw).hexdigest() != "29e4484a710901667eddfed70a0e74f74e8e89e602c9de93668237b345a01249":
        raise AssertionError("released GitHub fixture bytes changed")
    generator = raw.decode("utf-8")
    if not generator.strip().startswith("import") or "manifest = {" not in generator:
        raise AssertionError("released GitHub generator extraction failed")
    meta = folder / "meta"
    env = {**os.environ, "OVERLAY": str(overlay), "META": str(meta), "TARGET_VER": "1.0.7.36",
           "TARGET_COMMIT": "0" * 40, "SA02M_NGINX_SITE": str(folder / "no-site")}
    subprocess.run([sys.executable, "-c", generator], env=env, check=True, capture_output=True)
    return json.loads((meta / "manifest.json").read_text())


def offline_manifest(overlay, folder):
    packer = module("_journal_packer", ROOT / "scripts/pack-offline-update.py")
    patterns = packer.load_allowlist_patterns()
    paths = [p for p in packer.iter_overlay_files(overlay)
             if any(packer.path_matches(p, pattern) for pattern in patterns)]
    deploy = packer.build_deploy_list(paths, packer.load_deploy_map())
    payload, size = packer.make_payload_tar_gz(overlay, paths)
    manifest = packer.build_manifest(version="1.0.7.36", commit="0" * 40,
                                     key_id="delivery-test", payload_gz=payload,
                                     uncompressed_size=size, deploy=deploy)
    key = folder / "test-key.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "Ed25519", "-out", str(key)],
                   check=True, capture_output=True)
    signature = packer.sign_manifest(manifest, key, "openssl")
    package = folder / "delivery.sa02m"
    packer.write_outer_package(package, manifest=manifest, sig_b64=signature, payload_gz=payload)
    with tarfile.open(package) as outer:
        restored = json.load(outer.extractfile("manifest.json"))
        message = folder / "message"
        message.write_bytes(packer.SIG_DOMAIN + packer.canonical_json(restored))
        import base64
        signed = folder / "signature"
        signed.write_bytes(base64.b64decode(outer.extractfile("manifest.sig").read()))
        subprocess.run(["openssl", "pkeyutl", "-verify", "-inkey", str(key), "-rawin",
                        "-in", str(message), "-sigfile", str(signed)], check=True, capture_output=True)
        unpacked = folder / "unpacked"
        with tarfile.open(fileobj=io.BytesIO(outer.extractfile("payload.tar.gz").read()), mode="r:gz") as inner:
            inner.extractall(unpacked, filter="data")
    return restored, unpacked


def deploy(manifest, overlay, destination):
    for item in manifest["deploy"]:
        relative = Path(item["dst"].lstrip("/"))
        target = destination / relative
        if destination.resolve() not in target.resolve().parents:
            raise AssertionError("unsafe isolated deployment target")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(overlay / item["src"], target)
        target.chmod(int(item["mode"], 8))


def unit_environment(path, destination):
    values = {}
    for line in path.read_text().splitlines():
        if line.startswith("Environment="):
            for setting in shlex.split(line.partition("=")[2]):
                key, _, value = setting.partition("=")
                if key.startswith("STAND_DEVICES_EVENTS_") and value.startswith("/"):
                    value = str(destination / value.lstrip("/"))
                values[key] = value
    return values


def service_hooks(manifest, destination, *, skip_api=False):
    shim = destination / "bin"
    shim.mkdir(exist_ok=True)
    systemctl = shim / "systemctl"
    systemctl.write_text('''#!/usr/bin/python3
import os, sys
args = sys.argv[1:]
if args[0] != 'show':
    raise SystemExit('unexpected systemctl mutation in inactive-stand fixture')
unit = args[1]
keys = next(a for a in args if a.startswith('--property=')).split('=', 1)[1].split(',')
env = ' '.join(k + '=' + v for k, v in os.environ.items() if k.startswith('STAND_DEVICES_EVENTS_'))
values = dict(Id=unit, LoadState='loaded', ActiveState='active', UnitFileState='enabled', MainPID='0', Environment=env)
if unit == 'sa02m-stand-api.service':
    values.update(LoadState='not-found', ActiveState='inactive', UnitFileState='')
for key in keys:
    print(key + '=' + values.get(key, ''))
''')
    systemctl.chmod(0o755)
    results = []
    for service in manifest["services"]["restart"]:
        if service not in {"sa02m-devices-api", "sa02m-devices-logger"} or (skip_api and service.endswith("api")):
            continue
        unit = destination / "etc/systemd/system" / (service + ".service")
        hooks = [line.partition("=")[2] for line in unit.read_text().splitlines()
                 if line.startswith("ExecStartPre=")]
        if len(hooks) != 1 or not hooks[0].startswith("-/usr/bin/python3 -I "):
            raise AssertionError("delivered unit lacks its isolated, nonfatal prepare hook")
        command = shlex.split(hooks[0][1:])
        command[2] = str(destination / command[2].lstrip("/"))
        env = {**os.environ, **unit_environment(unit, destination), "PATH": str(shim) + ":/usr/bin:/bin"}
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=55)
        if result.returncode:
            raise AssertionError(result.stderr)
        results.append(service)
    if not results:
        raise AssertionError("released restart manifest exercised no startup command")
    return results


def installer_rootfs(folder, *, active_peer=False):
    source = candidate_payload(folder)
    scripts = source / "scripts"
    scripts.mkdir()
    destination = folder / "rootfs"
    for name in ("etc/default", "etc/systemd/system", "var/lib", "opt"):
        (destination / name).mkdir(parents=True, exist_ok=True)
    (destination / "etc/systemd/system/sa02m-stand-api.service").write_text("[Service]\nExecStart=/bin/true\n")
    installer = (ROOT / "scripts/11-devices.sh").read_text()
    # Retarget only literal live paths. $REPO_ROOT/opt and $ETC_DIR/... remain source paths.
    installer = re.sub(r"(?<![A-Za-z0-9_$}])/(?:opt|etc|var|usr/lib/systemd|lib/systemd)(?=/)",
                       lambda match: str(destination) + match[0], installer)
    (scripts / "11-devices.sh").write_text(installer)
    (scripts / "lib.sh").write_text('''check_root() { :; }
log() { printf '%s\\n' "$*"; }
sa02m_render_nginx_site() { return 1; }
sa02m_svc_capture() { printf 'capture %s\\n' "$*"; }
sa02m_svc_apply() { SA02M_SVC_LAST_RESULT=left-inactive; printf 'apply %s\\n' "$*"; }
sa02m_atomic_install() { install "$@"; }
''')
    shim = folder / "bin"
    shim.mkdir()
    (shim / "systemctl").write_text("#!/bin/sh\necho forbidden-manager-call >&2\nexit 79\n")
    (shim / "systemctl").chmod(0o755)
    env = {**os.environ, "SA02M_ROOTFS_BUILD": "1", "PATH": str(shim) + ":/usr/bin:/bin",
           "STAND_DEVICES_EVENTS_STATE_DIR": str(destination / "var/lib/sa02m-devices/event-journal"),
           "STAND_DEVICES_EVENTS_BOOTSTRAP_DIR": str(destination / "var/lib/sa02m-devices-journal-init"),
           "STAND_DEVICES_EVENTS_UPDATE_DIR": str(destination / "var/lib/sa02m-update")}
    old_process = None
    if active_peer:
        env.pop("SA02M_ROOTFS_BUILD")
        fixture = folder / "peer-fixture"
        fixture.mkdir()
        old_environment = {key: value for key, value in env.items() if not key.startswith("STAND_DEVICES_EVENTS_")}
        old_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                       env=old_environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (fixture / "pid").write_text(str(old_process.pid))
        env["JOURNAL_PEER_FIXTURE_DIR"] = str(fixture)
        (shim / "systemctl").write_text('''#!/usr/bin/python3
import os, signal, subprocess, sys
from pathlib import Path
root = Path(os.environ['JOURNAL_PEER_FIXTURE_DIR'])
args = sys.argv[1:]
if args == ['daemon-reload']:
    raise SystemExit(0)
if args == ['--no-block', 'try-restart', 'sa02m-stand-api.service']:
    os.kill(int((root / 'pid').read_text()), signal.SIGTERM)
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        start_new_session=True)
    (root / 'pid').write_text(str(process.pid))
    with (root / 'restarts').open('a') as stream:
        stream.write('try-restart sa02m-stand-api.service\\n')
    raise SystemExit(0)
if args[0] != 'show':
    raise SystemExit('unexpected controlled manager command')
unit = args[1]
keys = next(a for a in args if a.startswith('--property=')).split('=', 1)[1].split(',')
env = ' '.join(k + '=' + v for k, v in os.environ.items() if k.startswith('STAND_DEVICES_EVENTS_'))
values = dict(Id=unit, LoadState='loaded', ActiveState='active', UnitFileState='enabled',
              MainPID=(root / 'pid').read_text() if unit == 'sa02m-stand-api.service' else '0', Environment=env)
for key in keys:
    print(key + '=' + values.get(key, ''))
''')
    try:
        result = subprocess.run(["bash", str(scripts / "11-devices.sh")], env=env, capture_output=True,
                                text=True, timeout=55)
    finally:
        if old_process is not None:
            old_process.terminate()
            old_process.wait(timeout=5)
            new_pid = int((fixture / "pid").read_text())
            if new_pid != old_process.pid:
                os.kill(new_pid, signal.SIGTERM)
    if result.returncode or "forbidden-manager-call" in result.stderr or "preparation failed" in result.stdout:
        raise AssertionError(result.stdout + result.stderr)
    return destination, result.stdout


def order_gate_mutations(folder):
    paths = (".ai-dev/quality/checks/devices-api-upstream.sh", ".ai-dev/quality/checks/lib_check.sh",
             "etc/nginx/network_config.conf", "scripts/11-devices.sh", "etc/systemd/sa02m-devices-api.service",
             "opt/sa02m-devices/sa02m_devices/api.py", "opt/sa02m-devices/sa02m_devices/stand_socket_group.py",
             "etc/systemd/system/sa02m-stand-api.service.d/20-devices-socket-group.conf")
    for path in paths:
        destination = folder / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, destination)
    installer = folder / "scripts/11-devices.sh"
    original = installer.read_text()
    hook = "\nsa02m_stand_devices_socket_group\n"
    prepare = 'if ! /usr/bin/python3 -I /opt/sa02m-devices/sa02m_devices_journal_startup.py "${_journal_args[@]}"; then'
    if original.count(hook) != 1 or original.count(prepare) != 1:
        raise AssertionError("installer mutation target missing or ambiguous")
    cases = {"baseline": original, "comment-hook": original.replace(hook, "\n#sa02m_stand_devices_socket_group\n"),
             "late-hook": original.replace(hook, "\n") + hook,
             "comment-prepare": original.replace(prepare, "#" + prepare)}
    for name, source in cases.items():
        installer.write_text(source)
        result = subprocess.run(["bash", str(folder / paths[0])], capture_output=True, text=True, timeout=10)
        expected = 0 if name == "baseline" else 1
        if result.returncode != expected:
            raise AssertionError(name + ": " + result.stdout + result.stderr)


def verify_units(destination):
    units = destination / "etc/systemd/system"
    for target in ("sysinit", "basic", "shutdown", "network", "multi-user"):
        (units / (target + ".target")).write_text("[Unit]\nDescription=Isolated fixture target\n")
    for name in ("sa02m-modbus-mqtt", "sa02m-stand-api"):
        (units / (name + ".service")).write_text("[Unit]\nAfter=network.target\n[Service]\nExecStart=/usr/bin/python3\n")
    executable = destination / "usr/bin/python3"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch(mode=0o755)
    result = subprocess.run(["systemd-analyze", "verify", "--man=no", "--generators=no",
                             "--root=" + str(destination), "sa02m-devices-api.service",
                             "sa02m-devices-logger.service", "sa02m-stand-api.service"],
                            capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise AssertionError(result.stderr)
