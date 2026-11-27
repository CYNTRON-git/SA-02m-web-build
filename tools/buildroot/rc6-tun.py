#!/usr/bin/env python3
"""Build opt-in TUN for the two pinned SA-02m release kernels and verify a staging target."""
import argparse
import gzip
import hashlib
import io
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import tarfile
import tempfile
from typing import NamedTuple

IMAGE = "sha256:9af56be350d47f08b71df02abac56118fdc3bfa886adc26c64eb612fb3d50e7a"
TUN_SOURCE = "e13822c8f1e778f5cee71c0e8f870b6c3af685a65f898420e22e6c2d59259e7e"
COMPILER = {
    "gcc": "81306660999ab70d8155ac68242981b69f07f8d5e78bf2712c80b529dbfb905a",
    "cc1": "a0ee24d065b1fc329cd75784480861e722baf8010e35b3b62b96f4dccdd30089",
    "ld": "07e7a292f104af0a626491ac2340f080b7bfacb0b4c615ad56623111a43d4419",
    "as": "bb13c69fbde1b2364fbf29e930aa8b25b1bb869894fe46b26972907dd7e4681b",
}


class Profile(NamedTuple):
    release: str
    build_id: str
    config: str
    symvers: str
    autoconf: str
    uts: str
    module: str
    archive: str
    vermagic: str

    @property
    def filename(self):
        return f"sa02m-tun-{self.release}.tar.gz"

    @property
    def member(self):
        return f"lib/modules/{self.release}/extra/tun.ko"


PROFILES = {
    "smp": Profile(
        "6.1.0-rc6", "dbf14e07d6cbba27fcfc6b2779b3b5ea23acfa6e",
        "80a0b024ecca655db78f7c65bec84f8033b3d1bd354cbecf0fd7c975c4de349b",
        "fa8f841f575f4fb9315bdd43a78551daf0ef073d2a152bfdd4d74e661610f150",
        "928c8be1268db079cf68e100437577192fc353695a209f95d6d06213b7c388ba",
        "7089ea30cb403b8770be5ae8e3045a3fc33ff820668e28080a3d11121c33ad15",
        "9aa97adba768ee04ba54f640442eaca8839b495566bf0847b3955d0206a5700c",
        "ee8e43b91ef733d72f403f9d4248e97058a22c1dd8a55ad9703cf884686ff3a0",
        "6.1.0-rc6 SMP mod_unload ARMv7 p2v8",
    ),
    "rt": Profile(
        "6.1.0-rc6-rt4", "82f7bb735df8af36511004673a45aa3bfcf40227",
        "1d6b6328775026a88e36bd496e373b9243bcb7ece95b69260f9e6e4a36d1fe4b",
        "995bcf2684b8fb7594dd092e569b7af91e937755cdaa21aafefafc624ecc0292",
        "b3dc2dd27bbdd372a2a6a18fc4d8fe55b27b51f86dce3cf5d8148a494aa2d306",
        "249e62b3ef25798b53f6890821687813a4a07ec36b0dab4b11ff2b02b3f70d18",
        "2bfdf923636ecb72e8bccc70608a1dbb9fa32a197fa6343e13c54f496ad239c0",
        "69579271807ed89c18b86baa7d73c708594e3f8c99928c1e45395acdff85b9b8",
        "6.1.0-rc6-rt4 SMP preempt_rt mod_unload ARMv7 p2v8",
    ),
}


def check_digest(data, expected, label):
    value = hashlib.sha256(data).hexdigest()
    if value != expected:
        raise ValueError(f"{label}: SHA-256 {value}; expected {expected}")


def make_archive(profile, module):
    check_digest(module, profile.module, "tun.ko")
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w|", format=tarfile.USTAR_FORMAT) as tar:
            member = tarfile.TarInfo(profile.member)
            member.size = len(module)
            member.mode = 0o644
            tar.addfile(member, io.BytesIO(module))
    data = buf.getvalue()
    check_digest(data, profile.archive, profile.filename)
    return data


def worker(name):
    profile = PROFILES[name]
    output = Path("/work/profiles") / name / "output"
    kernel = output / "build/linux-custom"
    cross = str(output / "host/bin/arm-none-linux-gnueabihf-")
    inputs = {
        kernel / ".config": profile.config,
        kernel / "Module.symvers": profile.symvers,
        kernel / "include/generated/autoconf.h": profile.autoconf,
        kernel / "include/generated/utsrelease.h": profile.uts,
        kernel / "drivers/net/tun.c": TUN_SOURCE,
        Path(cross + "gcc"): COMPILER["gcc"],
        Path(cross + "ld"): COMPILER["ld"],
        Path(cross + "as"): COMPILER["as"],
    }
    for path, digest in inputs.items():
        check_digest(path.read_bytes(), digest, path)
    cc1 = Path(subprocess.check_output([cross + "gcc", "-print-prog-name=cc1"], text=True).strip())
    check_digest(cc1.read_bytes(), COMPILER["cc1"], cc1)
    # __FILE__ is embedded by Kbuild. Use the same path as the approved reference binaries.
    stage = Path("/trial") / ("current-tun-" + name)
    stage.mkdir(parents=True)
    (stage / "tun.c").write_bytes((kernel / "drivers/net/tun.c").read_bytes())
    (stage / "Makefile").write_text("obj-m := tun.o\nccflags-y += -DCONFIG_TUN_MODULE=1\n")
    subprocess.run(["make", "-j1", "-C", str(kernel), "ARCH=arm", "CROSS_COMPILE=" + cross,
                    "M=" + str(stage), "modules"], check=True, stdout=sys.stderr)
    mod = stage / "tun.ko"
    for field, expected in (("name", "tun"), ("vermagic", profile.vermagic), ("depends", "")):
        actual = subprocess.check_output(["modinfo", "-F", field, str(mod)], text=True).strip()
        if actual != expected:
            raise ValueError(f"{mod}: unexpected {field}: {actual!r}")
    sys.stdout.buffer.write(make_archive(profile, mod.read_bytes()))


def build(destination, volume):
    # An absent/misspelled Docker volume must not be created implicitly.
    subprocess.run(["docker", "volume", "inspect", volume], stdout=subprocess.DEVNULL, check=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        stage = Path(temp) / "archives"
        stage.mkdir()
        for name, profile in PROFILES.items():
            cmd = ["docker", "run", "--rm", "-i", "--pull=never", "--platform", "linux/amd64",
                   "--network", "none", "--cpus", "1", "--memory", "512m", "--user", "0",
                   "--mount", f"type=volume,src={volume},dst=/work,readonly", IMAGE,
                   "python3", "-", "--worker", name]
            data = subprocess.run(cmd, input=Path(__file__).read_bytes(), stdout=subprocess.PIPE,
                                  check=True).stdout
            check_digest(data, profile.archive, profile.filename)
            (stage / profile.filename).write_bytes(data)
        stage.rename(destination)
    for p in PROFILES.values():
        print(p.archive, destination / p.filename)


def kernel_build_id(notes):
    values = []
    offset = 0
    while offset < len(notes):
        if offset + 12 > len(notes):
            raise ValueError("truncated kernel note")
        namesz, descsz, kind = struct.unpack_from("<III", notes, offset)
        offset += 12
        name = notes[offset:offset + namesz]
        offset += (namesz + 3) & ~3
        desc = notes[offset:offset + descsz]
        offset += (descsz + 3) & ~3
        if offset > len(notes):
            raise ValueError("truncated kernel note data")
        if name == b"GNU\0" and kind == 3:
            values.append(desc.hex())
    if len(values) != 1:
        raise ValueError("expected exactly one GNU kernel build ID")
    return values[0]


def verify_running(archive, release, machine, notes):
    match = [p for p in PROFILES.values() if p.release == release]
    if machine != "armv7l" or len(match) != 1 or kernel_build_id(notes) != match[0].build_id:
        raise ValueError("running architecture, kernel release or build ID is outside the two verified profiles")
    profile = match[0]
    check_digest(archive.read_bytes(), profile.archive, archive)
    print(f"Verified {profile.member} for {release}, build ID {profile.build_id}")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(sys.argv[2])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build both pinned archives into a new directory")
    b.add_argument("output", type=Path)
    b.add_argument("--volume", default="cyntron-sa-bsp-work", help="existing Buildroot BSP volume")
    v = sub.add_parser("verify-running", help="verify archive against this running kernel")
    v.add_argument("archive", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        build(args.output, args.volume)
    else:
        verify_running(args.archive, platform.release(), platform.machine(), Path("/sys/kernel/notes").read_bytes())


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        sys.exit(str(e))
