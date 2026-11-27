#!/usr/bin/env python3
"""Export Git object proofs for Ubuntu tracker requests from a Noble admission capture."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
from release_admission.providers import canonical, require
from release_admission.ubuntu_tracker import CVE, DIRECTORIES, REPOSITORY, tree, verified_object


def export(repository, commit, requested, output, started):
    """A supplied object store is untrusted. Each selected object is checked against the pinned graph."""
    objects = output / "objects"
    objects.mkdir()

    def write(kind, digest):
        path = objects / digest
        if not path.exists():
            raw = subprocess.run(["git", "--no-replace-objects", "-C", str(repository), "cat-file", kind, digest],
                                 check=True, stdout=subprocess.PIPE, timeout=30).stdout
            path.write_bytes(verified_object(kind, digest, raw))
        return path.read_bytes()

    header = write("commit", commit).split(b"\n\n",1)[0]
    roots = re.findall(rb"^tree ([0-9a-f]{40})$",header,re.M)
    require(len(roots) == 1, "invalid Ubuntu tracker root")
    root = tree(write("tree", roots[0].decode()))
    exported = 0
    for directory in DIRECTORIES:
        mode, digest = root[directory.encode()]
        require(mode == b"40000", "invalid Ubuntu tracker directory")
        entries = tree(write("tree", digest))
        for cve in requested:
            entry = entries.get(cve.encode())
            if entry and entry[0] == b"100644":
                write("blob", entry[1])
                exported += 1
    snapshot = {"schema":1, "started":started, "repository":REPOSITORY, "commit":commit,
                "requested_cves":len(requested), "exported_paths":exported}
    (output / "tracker.json").write_bytes(canonical(snapshot) + b"\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ubuntu-capture", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--git-dir", type=Path, help="reuse an existing local Git object store")
    args = parser.parse_args()
    policy = json.loads((Path(__file__).resolve().parents[2]/"vendor-src/admission/policy.json").read_bytes())
    commit = policy["ubuntu_tracker_commit"]
    website = json.loads((args.ubuntu_capture/"ubuntu.json").read_bytes())
    requested = set()
    for response in website["responses"].values():
        match = re.fullmatch(r"https://ubuntu\.com/security/cves/(CVE-[0-9]{4}-[0-9]{4,})\.json",response["url"])
        if match:
            requested.add(match[1])
    for gap in website["result"]["gaps"]:
        if gap.endswith(": tracker unavailable") and CVE.fullmatch(gap.split(" ",1)[0]):
            requested.add(gap.split(" ",1)[0])
    require(requested, "no Ubuntu tracker requests")
    started = time.time()
    with tempfile.TemporaryDirectory(dir=args.out.parent) as scratch:
        working = Path(scratch)
        store = args.git_dir
        if store is None:
            store = working/"repository.git"
            subprocess.run(["git","init","--bare","--quiet",str(store)],check=True)
            subprocess.run(["git","-C",str(store),"-c","credential.helper=","-c","fetch.fsckObjects=true","fetch","--depth=1","--no-tags",
                            REPOSITORY,commit],check=True,timeout=3600)
        result = working/"capture"
        result.mkdir()
        export(store,commit,requested,result,started)
        require(not args.out.exists(), "Ubuntu tracker output already exists")
        result.rename(args.out)
    print((args.out/"tracker.json").read_text(),end="")


if __name__ == "__main__":
    main()
