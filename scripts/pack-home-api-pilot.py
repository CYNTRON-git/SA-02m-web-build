#!/usr/bin/env python3
"""Create an offline pilot from committed bytes; never connects to a board."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
EXACT = (
    "opt/sa02m-alice/sa02m_alice/client/device_registry.py",
    "opt/sa02m-alice/sa02m_alice/client/sio_connection.py",
    "opt/sa02m-alice/sa02m_alice/client/sio_handlers.py",
    "opt/sa02m-rules/sa02m_rules/document_lock.py",
    "opt/sa02m-rules/sa02m_rules/home_command.py",
    "opt/sa02m-rules/sa02m_rules/home_document.py",
    "opt/sa02m-rules/sa02m_rules/upgrade.py",
    "opt/sa02m-rules/sa02m_rules/store.py",
    "opt/sa02m-rules/sa02m_rules/service.py",
    "etc/systemd/system/sa02m-home-api.service",
    "etc/systemd/system/sa02m-rules.service",
)
PREFIX = "opt/sa02m-alice/sa02m_alice/home/"
DOCS = ("docs/contracts/home-api-v1.md", "docs/contracts/home-api-v1-wire.json",
        "docs/contracts/home-api-v1-pilot.md")


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def commit(ref):
    value = git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise ValueError("invalid commit")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--base", required=True)
    parser.add_argument("--output", required=True, type=Path, help="new directory; existing paths are refused")
    args = parser.parse_args()
    ref, base = commit(args.ref), commit(args.base)
    subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", base, ref], check=True)
    names = git("ls-tree", "-r", "--name-only", ref).decode().splitlines()
    paths = sorted(set(EXACT) | {name for name in names if name.startswith(PREFIX) and name.endswith(".py")})
    if not any(name.startswith(PREFIX) for name in paths) or not set(paths + list(DOCS)) <= set(names):
        raise ValueError("selected commit does not contain the complete Home API candidate")
    files = {"payload/" + name: git("show", ref + ":" + name) for name in paths}
    files.update({"recipes/" + Path(name).name: git("show", ref + ":" + name) for name in DOCS})
    files["recipes/nginx.patch"] = git("diff", "--no-ext-diff", "--binary", base, ref, "--", "etc/nginx/network_config.conf")
    if not files["recipes/nginx.patch"]:
        raise ValueError("base/ref contain no nginx Home route delta; select the reviewed base")
    manifest = {"schema": 1, "kind": "home-api-v1-pilot", "commit": ref, "base": base,
                "deployment": "manual reviewed pilot procedure; no service actions in package",
                "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                          for name, data in sorted(files.items())},
                "runtime": [{"source": "payload/" + name, "destination": "/" + name,
                             "mode": "0644", "owner": "root", "group": "root"} for name in paths]}
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    files["SHA256SUMS"] = "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n"
                                   for name, data in sorted(files.items())).encode()
    args.output.mkdir(parents=False, exist_ok=False)
    marker = args.output / ".incomplete"
    marker.write_text("Package is not complete until this marker is removed.\n")
    for name, data in files.items():
        path = args.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    archive = args.output / ("home-api-" + ref[:12] + ".tar.gz")
    with tarfile.open(archive, "w:gz") as output:
        for name in sorted(files):
            output.add(args.output / name, arcname=name, recursive=False)
    (args.output / "archive.sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
    marker.unlink()
    print(json.dumps({"commit": ref, "archive": str(archive.resolve()), "runtime_files": len(paths)}))


if __name__ == "__main__":
    main()
