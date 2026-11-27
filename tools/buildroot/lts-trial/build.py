#!/usr/bin/env python3
"""Build both trial kernels in fresh, offline containers."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import tempfile


IMAGE = "sha256:9af56be350d47f08b71df02abac56118fdc3bfa886adc26c64eb612fb3d50e7a"
BSP = "cyntron-sa-bsp-work"
HERE = Path(__file__).resolve().parent


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, **kwargs)


def build_profile(profile, archive, directory):
    cid = docker("create", "--network", "none", "--cpus", "2", "--memory", "3g",
                 "--mount", f"type=volume,src={BSP},dst=/work,readonly",
                 IMAGE, "bash", "/inputs/compile.sh", profile,
                 stdout=subprocess.PIPE, text=True).stdout.strip()
    try:
        docker("cp", str(HERE), f"{cid}:/inputs")
        docker("cp", str(archive), f"{cid}:/linux.tar.xz")
        docker("start", "--attach", cid)
        if docker("inspect", "--format", "{{.State.ExitCode}}", cid,
                  stdout=subprocess.PIPE, text=True).stdout.strip() != "0":
            raise RuntimeError(f"{profile} build failed")
        docker("cp", f"{cid}:/out", str(directory / profile))
    finally:
        docker("rm", "-f", cid, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="official linux-6.1.193.tar.xz")
    parser.add_argument("destination", type=Path, help="new local results directory")
    args = parser.parse_args()
    archive = args.archive.resolve(strict=True)
    out = args.destination.expanduser().absolute()
    expected = next(line.split()[0] for line in (HERE / "source.sha256").read_text().splitlines()
                    if line.endswith("  /linux.tar.xz"))
    hasher = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    if hasher.hexdigest() != expected:
        parser.error("Linux archive checksum mismatch")
    if out.exists() or out.is_symlink():
        parser.error("destination already exists")
    docker("image", "inspect", IMAGE, stdout=subprocess.DEVNULL)
    docker("volume", "inspect", BSP, stdout=subprocess.DEVNULL)
    out.parent.mkdir(parents=True, exist_ok=True)
    lock = out.with_name(out.name + ".build-lock")
    lock.mkdir()
    try:
        with tempfile.TemporaryDirectory(prefix=".kernel-trial-", dir=out.parent) as work:
            stage = Path(work) / "kernels"
            stage.mkdir()
            for profile in ("smp", "rt"):
                build_profile(profile, archive, stage)
            if out.exists() or out.is_symlink():
                raise FileExistsError(out)
            stage.rename(out)
    finally:
        lock.rmdir()


if __name__ == "__main__":
    main()
