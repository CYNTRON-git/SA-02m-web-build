"""Trusted filesystem, package and ABI checks shared by runtime staging and verification."""
import hashlib
from functools import cache
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import sysconfig


class Unavailable(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Unavailable(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def trusted(path, *, directory=False):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, f"invalid path {path}")
    for component in (*reversed(path.parents), path):
        st = component.lstat()
        check = stat.S_ISDIR if component != path or directory else stat.S_ISREG
        require(st.st_uid == 0 and not st.st_mode & 0o022 and check(st.st_mode),
                f"unsafe path {component}")
    return path


def read_json(path):
    trusted(path)
    require(path.stat().st_size <= 1024 * 1024, f"oversized JSON {path}")
    return json.loads(path.read_bytes())


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def checked(path, row):
    trusted(path)
    require(path.stat().st_size == row["bytes"] and sha(path) == row["sha256"],
            f"file mismatch {path}")


def run(argv, timeout=60):
    env = {"LANG":"C.UTF-8", "PATH":"/usr/sbin:/usr/bin:/sbin:/bin",
           "PIP_CONFIG_FILE":"/dev/null"}
    result = subprocess.run(argv, cwd="/", env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=timeout)
    require(result.returncode == 0,
            f"{Path(argv[0]).name} exit {result.returncode}: {(result.stderr or result.stdout)[-2000:]}")
    return result.stdout


def dpkg(package):
    fmt = "${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Status}"
    return run(["/usr/bin/dpkg-query", "-W", "-f="+fmt, package], 10).split("\t")


def validate_abi(target, query=dpkg):
    import platform
    info = platform.freedesktop_os_release()
    require(info.get("ID") == target["os"] and info.get("VERSION_CODENAME") == target["codename"], "OS mismatch")
    require(".".join(map(str, sys.version_info[:2])) == target["python"] and
            sysconfig.get_config_var("SOABI") == target["soabi"], "Python ABI mismatch")
    require(Path(sys.executable).resolve() == Path(target["interpreter"]["path"]), "interpreter mismatch")
    for key, arch in (("bootstrap", "all"), ("interpreter", target["arch"])):
        obj = target[key]
        path = Path(obj["path"])
        checked(path, obj)
        binary, version, actual_arch, status = query(obj["owner"])
        require(binary.split(":")[0] == obj["owner"] and (version, actual_arch, status)
                == (obj["version"], arch, "installed"), f"package mismatch {obj['owner']}")
        require(run(["/usr/bin/dpkg-query", "-S", str(path)], 10).strip()
                == f"{binary}: {path}", f"package does not own {path}")


SYSTEM_SITE = Path("/usr/lib/python3/dist-packages")


def validate_native(catalogue):
    """Select the exact installed package revision and verify its owned ELF bytes."""
    @cache
    def inventory(binary):
        package, version, arch, state = dpkg(binary)
        require(package==binary and arch=="armhf" and state=="installed", "native package changed "+binary)
        return version, set(run(["/usr/bin/dpkg-query","-L",binary],10).splitlines())
    selected={}
    for soname,row in catalogue.items():
        version,owned=inventory(row["binary"])
        pin=row["versions"].get(version)
        require(pin is not None,"unverified native package revision "+row["binary"]+"="+version)
        path=Path(row["path"])
        require(any(str(candidate) in owned and candidate.resolve()==path for candidate in map(Path,row["owned_paths"])),
                "unowned native library "+str(path))
        checked(path,pin)
        selected[soname]={"path":str(path),"binary":row["binary"],"version":version,**pin}
    return selected


def validate_bridge(bridge, system_site=SYSTEM_SITE, query=dpkg):
    owned = {}
    for name, pkg in bridge["packages"].items():
        binary = pkg["binary"]
        require(query(name) == [binary, pkg["version"], pkg["architecture"], "installed"],
                f"bridge package changed {name}")
        owned[name] = set(run(["/usr/bin/dpkg-query", "-L", binary], 10).splitlines())
    seen = set()
    for row in bridge["files"]:
        rel = Path(row["path"])
        require(not rel.is_absolute() and ".." not in rel.parts and str(rel) not in seen
                and not rel.name.endswith((".pth", ".pyc")) and rel.stem not in {"sitecustomize","usercustomize"},
                f"invalid bridge file {rel}")
        seen.add(str(rel))
        path = system_site / rel
        require(str(path) in owned[row["owner"]], f"unowned bridge file {path}")
        checked(path, row)


def tree_digest(root, links):
    """Seal every entry before executing installed code, including new startup files."""
    digest = hashlib.sha256()
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + files):
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            st = path.lstat()
            require(st.st_uid == 0, f"incorrect owner {path}")
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(path)
                require(links.get(rel) == target, f"unexpected link {path}")
                value = target
            else:
                require(not st.st_mode & 0o022, f"writable runtime {path}")
                require(stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode), f"nonregular entry {path}")
                value = "" if stat.S_ISDIR(st.st_mode) else sha(path)
                require(not name.endswith(".pth") and name not in
                        {"sitecustomize.py","usercustomize.py","sitecustomize","usercustomize"},
                        f"startup hook in runtime {path}")
            digest.update(json.dumps([rel, st.st_mode, value], separators=(",", ":")).encode() + b"\n")
    return digest.hexdigest()
