#!/usr/bin/python3
"""Prepare or verify immutable offline environments. The caller owns activation."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
from time import sleep
import zipfile

HERE = Path(__file__).absolute().parent
sys.path.insert(0, str(HERE))
from integrity import (SYSTEM_SITE, checked, read_json, require, run, sha,
                       sync_dir, tree_digest, trusted, validate_abi, validate_bridge, validate_native)

BASE = Path("/opt/sa02m-python-runtimes")
GROUPS = ("gateway", "home", "mqtt", "opcua", "snmp")
GROUP_PROBE_TIMEOUT = 120
PIP = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('pip',run_name='__main__')"


def atomic_save(path, data):
    temp = path.parent / (".write-" + secrets.token_hex(12))
    with temp.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    sync_dir(path.parent)


class Catalogue:
    def __init__(self, directory):
        self.dir = trusted(directory, directory=True)
        self.manifest = read_json(self.dir / "runtime.json")
        require(self.manifest["schema"] == 1 and set(self.manifest["groups"]) == set(GROUPS),
                "unsupported runtime manifest")
        self.bridge = read_json(self.dir/"bridge.json")
        require(sha(self.dir/"bridge.json") == self.manifest["bridge_sha256"], "bridge manifest mismatch")
        self.target = self.manifest["target"]
        files = ["runtime.json", "bridge.json", "consumers.json","launch.py","integrity.py","probe.py","runtime.py","native_guard.py",
                 *(g+".lock" for g in GROUPS)]
        sources = {name:sha(trusted(self.dir/name)) for name in files}
        self.identity = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
        wheelset = self.manifest["wheelset"]
        require(re.fullmatch("[a-f0-9]{64}",wheelset), "invalid wheelset")
        self.wheels = trusted(self.dir/"wheelhouse"/wheelset, directory=True)

    def validate(self):
        validate_abi(self.target)
        validate_bridge(self.bridge)
        self.native = validate_native(self.manifest["groups"]["opcua"]["native"]["system"])
        packages = self.manifest["packages"]
        actual={p.name for p in self.wheels.iterdir()}
        expected={pin["file"] for pin in packages.values()}
        if expected-actual:
            missing=next(iter(sorted(expected-actual)))
            raise FileNotFoundError(2,"missing locked wheel",str(self.wheels/missing))
        require(actual==expected, "wheelhouse inventory mismatch")
        for pin in packages.values():
            filename = pin["file"]
            require(Path(filename).name == filename and filename.endswith(".whl"), "invalid wheel name")
            path = self.wheels/filename
            checked(path, pin)
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    member = Path(name)
                    require(not member.is_absolute() and ".." not in member.parts and not name.endswith(".pth")
                            and member.stem not in ("sitecustomize","usercustomize"), "invalid wheel member")
        for group, config in self.manifest["groups"].items():
            names = config["packages"]
            require(names == sorted(set(names)), "repeated or unsorted group package")
            expected = "".join(f"{n}=={packages[n]['version']} --hash=sha256:{packages[n]['sha256']}\n" for n in names)
            require((self.dir/(group+".lock")).read_bytes() == expected.encode(), f"lock mismatch {group}")


class Manager:
    def __init__(self, catalogue, base=BASE):
        self.cat = catalogue
        self.base = base
        self.releases = base/"releases"

    def layout(self, path, config):
        site = path/"lib"/("python"+self.cat.target["python"])/"site-packages"
        links = {}
        if (path/"lib64").is_symlink():
            links["lib64"] = "lib"
        interpreter = Path(self.cat.target["interpreter"]["path"])
        for name in ("python", "python3", interpreter.name):
            binary = path/"bin"/name
            require(binary.is_symlink() and binary.resolve() == interpreter, f"unexpected interpreter {binary}")
            target = os.readlink(binary)
            require(target in {"python3", interpreter.name, str(interpreter)}, f"unexpected interpreter link {binary}")
            links["bin/"+name] = target
        bridge = {}
        if config["bridge"]:
            for item in self.cat.bridge["files"]:
                rel = item["path"]
                source = str(SYSTEM_SITE/rel)
                links[(site/rel).relative_to(path).as_posix()] = source
                bridge[rel] = source
        return site, links, bridge

    def inspect(self, group, path, expected_digest=None):
        config = self.cat.manifest["groups"][group]
        _, links, bridge = self.layout(path, config)
        actual = tree_digest(trusted(path, directory=True), links)
        require(expected_digest is None or actual == expected_digest, "runtime bytes changed: "+group)
        expected = {n:self.cat.manifest["packages"][n]["version"] for n in config["packages"]}
        if bridge:
            expected["cryptography"] = self.cat.bridge["packages"]["python3-cryptography"]["version"].split("-")[0]
        output = run([str(path/"bin/python"), "-I", "-B", str(self.cat.dir/"probe.py"),
                      json.dumps([config, expected, bridge,self.cat.native if config.get("native") else {}])], GROUP_PROBE_TIMEOUT)
        return {"tree_sha256":actual, **json.loads(output)}

    def build(self, group, path):
        config = self.cat.manifest["groups"][group]
        run([self.cat.target["interpreter"]["path"], "-I", "-B", "-S", "-m", "venv",
             "--without-pip", "--symlinks", str(path)], 90)
        python = str(path/"bin/python")
        pip = [python, "-I", "-B", "-c", PIP, self.cat.target["bootstrap"]["path"],
               "--isolated", "--disable-pip-version-check", "--no-cache-dir"]
        run([*pip, "install", "--no-compile", "--only-binary=:all:", "--no-deps",
             "--no-index","--find-links", str(self.cat.wheels), "--require-hashes",
             "-r", str(self.cat.dir/(group+".lock"))], 180)
        # Validate wheel dependency markers before adding distro-native metadata.
        run([*pip, "check"])
        site, _, bridge = self.layout(path, config)
        for rel, target in bridge.items():
            dest = site/rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(target)
        return self.inspect(group, path)

    def verify(self, release_id, *, validate=True):
        require(re.fullmatch("[0-9a-f]{64}",release_id), "invalid release ID")
        if validate:
            self.cat.validate()
        path = self.releases/release_id
        receipt = read_json(path/"receipt.json")
        require(sha(trusted(path/"native-guard.json")) == receipt["guard_sha256"], "native guard changed")
        require(receipt["release_id"] == release_id and receipt["manifest_sha256"] == self.cat.identity
                and set(receipt["inventories"]) == set(GROUPS), "stale preparation receipt")
        for group in GROUPS:
            saved = receipt["inventories"][group]
            require(self.inspect(group, path/group, saved["tree_sha256"]) == saved, "runtime inventory mismatch")
        return {"ok":True, "release_id":release_id, "manifest_sha256":self.cat.identity,
                "base":str(path), "groups":list(GROUPS)}

    def prepare(self):
        require(os.geteuid() == 0, "staging requires root")
        for folder in (self.base, self.releases):
            folder.mkdir(mode=0o755, exist_ok=True)
            trusted(folder, directory=True)
        lock = os.open(self.base/"prepare.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(lock)
            require(stat.S_ISREG(st.st_mode) and st.st_uid == 0 and st.st_nlink == 1
                    and st.st_mode & 0o077 == 0, "unsafe preparation lock")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.cat.validate()
            index = self.base/("prepared-"+self.cat.identity+".json")
            if index.exists() or index.is_symlink():
                return self.verify(read_json(index)["release_id"], validate=False)
            # pip console scripts contain absolute shebangs. Construct at a unique
            # final path; a killed run cannot publish its unpublished directory.
            release_id = secrets.token_hex(32)
            path = self.releases/release_id
            path.mkdir(mode=0o700)
            try:
                receipts = {g:self.build(g, path/g) for g in GROUPS}
                from native_guard import stamp
                common = [Path(self.cat.target["interpreter"]["path"])]
                native = [SYSTEM_SITE/f["path"] for f in self.cat.bridge["files"]]
                opcua=[Path(target["path"]) for target in self.cat.native.values()]
                signatures = {str(p):{"stat":stamp(p),"sha256":sha(p)} for p in common+native+opcua}
                atomic_save(path/"native-guard.json", {"all":list(map(str,common)), "mqtt":list(map(str,native)),
                                                       "opcua":list(map(str,opcua)),
                                                       "signatures":signatures})
                atomic_save(path/"receipt.json", {"release_id":release_id, "manifest_sha256":self.cat.identity,
                                                   "guard_sha256":sha(path/"native-guard.json"), "inventories":receipts})
                os.chmod(path, 0o755)
                sync_dir(self.releases)
                result = self.verify(release_id, validate=False)
            except BaseException:
                # Only the private directory allocated by this locked invocation.
                shutil.rmtree(path)
                raise
            atomic_save(index, {"release_id":release_id})
            return result
        finally:
            os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "verify"])
    parser.add_argument("release_id", nargs="?")
    args = parser.parse_args()
    try:
        require(sys.flags.isolated and sys.flags.no_site, "manager requires isolated system Python without site")
        require((args.action == "verify") == bool(args.release_id), "invalid action arguments")
        os.umask(0o022)
        manager = Manager(Catalogue(HERE))
        if args.action == "prepare":
            result = manager.prepare()
        else:
            try:
                result = manager.verify(args.release_id)
            except subprocess.TimeoutExpired:
                # Cold boot can delay a package query or an import probe.
                # Rerun all catalogue, tree and native checks before accepting.
                sleep(15)
                result = Manager(Catalogue(HERE)).verify(args.release_id)
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        if args.action == "verify" and isinstance(exc, subprocess.TimeoutExpired):
            print(json.dumps({"ok":False, "code":"verification_timeout", "error":"runtime child timed out twice"}))
            return 1
        code = "verification_failed"
        if isinstance(exc,FileNotFoundError) and exc.filename and Path(exc.filename).is_relative_to(HERE):
            code = "payload_missing"
        elif isinstance(exc, BlockingIOError):
            code = "runtime_busy"
        print(json.dumps({"ok":False, "code":code, "error":str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
