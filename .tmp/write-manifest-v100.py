#!/usr/bin/env python3
import json, datetime, os, sys

out_dir = sys.argv[1] if len(sys.argv) > 1 else "tools/imaging/out"
base = "SA-02m_v1.0.0"
img = os.path.join(out_dir, f"{base}.img.xz")
sha_file = img + ".sha256"
manifest = os.path.join(out_dir, f"{base}.manifest.json")

with open(sha_file, encoding="utf-8") as f:
    sha = f.read().split()[0]

doc = {
    "image_name": os.path.basename(img),
    "image_sha256": sha,
    "created_at": datetime.datetime.now(datetime.timezone.utc)
    .replace(microsecond=0)
    .isoformat()
    .replace("+00:00", "Z"),
    "pipeline": {
        "tool": "make-image.sh",
        "version": "1.3",
        "pishrink": True,
        "zerofill": True,
        "cleanup": True,
        "id_reset_in_stream": True,
        "final_xz_level": "9e",
    },
    "source_device": {
        "ip": "192.168.1.136",
        "hostname": "SA-02",
        "emmc_device": "/dev/mmcblk2",
        "emmc_size_gib": 7.28,
    },
    "release_version": "1.0.0",
    "serial_profile": "sa02m-1eth",
}
with open(manifest, "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(manifest)
