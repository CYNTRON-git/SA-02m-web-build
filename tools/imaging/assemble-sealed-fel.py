#!/usr/bin/env python3
"""Build and read back a separate, file-backed SA-02m FEL USB medium on macOS."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
import zlib

ROOT = Path(__file__).resolve().parent
MIB = 1024**2


def run(args):
    return subprocess.check_output([str(a) for a in args])


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_boot(path):
    data = path.read_bytes()
    magic, header_crc, _, length, _, _, data_crc = struct.unpack(">7I", data[:28])
    payload = (ROOT.parents[1] / "etc/boot.cmd.sa02m.min").read_bytes()
    if (magic != 0x27051956 or data[30] != 6 or len(data) != 64 + length
        or zlib.crc32(data[:4] + bytes(4) + data[8:64]) != header_crc
        or zlib.crc32(data[64:]) != data_crc
        or data[64:] != struct.pack(">2I", len(payload), 0) + payload):
        raise ValueError("Payload FAT boot.scr does not match the release boot command and U-Boot CRCs")


def read_mbr(path):
    with path.open("rb") as f:
        block = f.read(512)
    if len(block) != 512 or block[-2:] != bytes.fromhex("55aa"):
        raise ValueError("Missing MBR")
    return [(block[446 + 16*i + 4], *struct.unpack_from("<II", block, 446 + 16*i + 8)) for i in range(4)]


@contextmanager
def mounted(image, directory, read_only):
    mode = "-readonly" if read_only else "-readwrite"
    response = plistlib.loads(run(["hdiutil","attach","-plist",mode,"-nobrowse","-mountpoint",directory,image]))
    entries = response["system-entities"]
    entry = next(x for x in entries if x.get("mount-point") == str(directory))
    try:
        if entry["volume-kind"] != "exfat" or not any(x.get("content-hint") == "FDisk_partition_scheme" for x in entries):
            raise ValueError("Expected MBR/exFAT mount")
        info = plistlib.loads(run(["diskutil","info","-plist",entry["dev-entry"]]))
        if info.get("BusProtocol") != "Disk Image" or info.get("Writable") != (not read_only):
            raise ValueError("Unexpected virtual disk or write mode")
        yield entry
    finally:
        run(["hdiutil","detach",directory])


def assemble(image, destination, boot_reference=None):
    sidecar = Path(str(image) + ".sha256")
    basename = image.name
    if not basename.endswith(".img.xz"):
        raise ValueError("Expected a managed .img.xz capture")
    manifest = image.with_name(basename.removesuffix(".img.xz") + ".manifest.json")
    doc = json.loads(manifest.read_bytes())
    expected = re.fullmatch(r"([A-Fa-f0-9]{64}) [ *]([^\r\n]+)\n?",sidecar.read_text())
    if (not expected or Path(expected[2]).name != basename or doc.get("image_name") != basename
        or expected[1].lower() != doc.get("image_sha256") or doc.get("runtime_capture") != "managed"
        or doc.get("release_version") != "1.0.8.3" or doc.get("serial_profile") != "sa02m-1eth"):
        raise ValueError("Capture identity, checksum record or profile mismatch")
    seal = doc.get("runtime_image_seal", {})
    if (set(seal) != {"schema","source","release_id","manifest_sha256","selector_sha256"}
        or type(seal["schema"]) is not int or seal["schema"] != 1
        or any(not isinstance(v,str) or not re.fullmatch("[0-9a-f]{64}",v) for k,v in seal.items() if k != "schema")):
        raise ValueError("Missing or invalid managed seal")
    compressed_sha = digest(image)
    if compressed_sha != expected[1].lower():
        raise ValueError("Compressed image SHA-256 mismatch")
    if destination.exists():
        raise FileExistsError(destination)
    # macOS rejects nested mounts on external noowners volumes. The private,
    # empty system-volume directory is only a mount point; image bytes stay on SSD.
    with (tempfile.TemporaryDirectory(dir=destination.parent, prefix=".usb-work-") as name,
          tempfile.TemporaryDirectory(dir="/private/tmp", prefix="sa02m-usb-mount-") as mount_name):
        work = Path(name)
        raw = work/"sdcard.img"
        with raw.open("xb") as handle:
            subprocess.run(["xz","--decompress","--stdout","--",image],stdout=handle,check=True)
        table = read_mbr(raw)
        p1, p2 = table[:2]
        if (p1[0] not in (11,12) or p2[0] != 131 or p1[1] < 1 or p1[2] < 1
            or p2[1] < p1[1]+p1[2] or p2[2] < 1 or (p2[1]+p2[2])*512 > raw.stat().st_size):
            raise ValueError("Expected an eMMC disk with FAT p1 and Linux p2 inside the captured file")
        boot = work/"boot.scr"
        run(["mcopy","-i",f"{raw}@@{p1[1]*512}","::boot.scr",boot])
        validate_boot(boot)
        boot_hashes = {}
        if boot_reference is not None:
            expected_boot = json.loads(boot_reference.read_bytes())
            if set(expected_boot) != {"zImage","zImage.smp","zImage.rt","sun8i-a40i-sk.dtb"}:
                raise ValueError("Incomplete hardware boot reference")
            for name, expected_sha in expected_boot.items():
                artifact = work/name
                run(["mcopy","-i",f"{raw}@@{p1[1]*512}","::"+name,artifact])
                boot_hashes[name] = digest(artifact)
                if boot_hashes[name] != expected_sha:
                    raise ValueError(f"eMMC FAT artifact differs from reviewed .136 snapshot: {name}")
        items = {"sdcard.img":raw, "boot.scr":boot, "autorun.sh":ROOT/"autorun-sealed.sh"}
        hashes = {n:digest(p) for n,p in items.items()}
        if hashes["sdcard.img"] != doc.get("uncompressed_sha256"):
            raise ValueError("Uncompressed disk does not match final free-block receipt")
        if digest(image) != compressed_sha:
            raise ValueError("Compressed payload changed during verification")
        seal_bytes = (json.dumps(seal, sort_keys=True, separators=(",", ":")) + "\n").encode()
        sealed_receipt = work/"SEALED-FEL"
        sealed_receipt.write_text(" ".join(("SA02M-SEALED-FEL-1", str(raw.stat().st_size),
            hashes["sdcard.img"], hashes["boot.scr"], hashlib.sha256(seal_bytes).hexdigest(),
            seal["selector_sha256"])) + "\n")
        items["SEALED-FEL"] = sealed_receipt
        hashes["SEALED-FEL"] = digest(sealed_receipt)
        (work/"SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n,h in hashes.items()))
        items["SHA256SUMS"] = work/"SHA256SUMS"
        expected_readback = {**hashes,"SHA256SUMS":digest(items["SHA256SUMS"])}
        result = work/"verified";result.mkdir()
        disk = result/"SA-02m-1eth-1.0.8.3-USB.img"
        dmg = work/"disk.dmg"
        # Use the disk-image service only on a newly created regular file.
        size_mib = max(128, (raw.stat().st_size + MIB - 1)//MIB + 64)
        run(["hdiutil","create","-size",f"{size_mib}m","-fs","ExFAT","-volname","SA02M1083","-layout","MBRSPUD","-type","UDIF","-o",dmg])
        mnt = Path(mount_name)
        with mounted(dmg,mnt,False):
            for n,p in items.items():
                with p.open("rb") as src, (mnt/n).open("xb") as out:
                    shutil.copyfileobj(src,out,length=4*MIB)
            for f in mnt.glob("._*"):
                f.unlink()
        dmg.rename(disk)
        usb_parts = read_mbr(disk)
        if usb_parts[0][0] != 7 or any(t != (0,0,0) for t in usb_parts[1:]) or (usb_parts[0][1]+usb_parts[0][2])*512 != disk.stat().st_size:
            raise ValueError("USB carrier is not a one-partition raw MBR disk")
        with mounted(disk,mnt,True) as entry:
            names = set(os.listdir(mnt))
            housekeeping = names - items.keys()
            if not housekeeping.issubset({".fseventsd",".Spotlight-V100",".Trashes"}) or not items.keys() <= names:
                raise ValueError(f"Unexpected read-back contents: {names}")
            readback = {n:digest(mnt/n) for n in items}
            if readback != expected_readback:
                raise ValueError("Read-back checksum mismatch")
            verify = subprocess.run(["/sbin/fsck_exfat","-n",entry["dev-entry"]],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,check=True)
        (result/"fsck-exfat.txt").write_bytes(verify.stdout)
        image_sha = digest(disk)
        (result/(disk.name+".sha256")).write_text(f"{image_sha}  {disk.name}\n")
        metadata = {"profile":doc["serial_profile"],"version":doc["release_version"],"source_xz":basename,
            "source_xz_sha256":compressed_sha,"source_manifest_sha256":digest(manifest),"managed_seal":seal,
            "source_bytes":raw.stat().st_size,"disk_file":disk.name,"disk_bytes":disk.stat().st_size,
            "disk_sha256":image_sha,"partition_table":"MBR","filesystem":"exFAT","filesystem_check":"fsck_exfat -n passed",
            "files_sha256_readback":readback,"emmc_fat_boot_sha256":boot_hashes,"recovery_entrypoint":"/mnt/autorun.sh","health_check":"offline file-backed image",
            "filesystem_metadata":sorted(housekeeping)}
        (result/"usb-manifest.json").write_text(json.dumps(metadata,indent=2)+"\n")
        result.rename(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image",type=Path)
    parser.add_argument("destination",type=Path)
    parser.add_argument("--boot-reference",type=Path,required=True)
    args=parser.parse_args()
    os.umask(0o077)
    assemble(args.image.resolve(strict=True),args.destination.resolve(),args.boot_reference)
