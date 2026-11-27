"""Execute inside the exact private interpreter and report actual package origins."""
import importlib
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import site
import sys
import sysconfig


def probe(config, expected, bridge, system):
    root = Path(sys.prefix)
    packages = Path(sysconfig.get_path("purelib"))
    if not sys.flags.isolated or root == Path(sys.base_prefix) or site.ENABLE_USER_SITE or not packages.is_relative_to(root):
        raise ValueError("interpreter isolation mismatch")
    for path in map(Path, sys.path):
        if ("dist-packages" in path.parts or "site-packages" in path.parts) and path != packages:
            raise ValueError(f"ambient site {path}")
    def normalize(name):
        return re.sub("[-_.]+", "-", name).lower()
    distributions = [(normalize(d.metadata["Name"]), d.version) for d in metadata.distributions()]
    if len(distributions) != len(set(n for n,v in distributions)) or dict(distributions) != expected:
        raise ValueError(f"installed inventory mismatch: {distributions}")
    modules = list(config["imports"])
    if config["bridge"]:
        modules.extend(["cryptography", "_cffi_backend", "cryptography.hazmat.bindings._rust"])
    if config["bridge"] or config.get("native"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        cipher = AESGCM(bytes(16))
        vector = bytes.fromhex("0388dace60b6a392f328c2b971b2fe78ab6e47d42cec13bdf53a67b21257bddf")
        if cipher.encrypt(bytes(12), bytes(16), None) != vector or cipher.decrypt(bytes(12), vector, None) != bytes(16):
            raise ValueError("AES-GCM known answer mismatch")
    origins = {}
    for name in modules:
        path = Path(importlib.import_module(name).__file__)
        rel = path.relative_to(packages).as_posix()
        if path.resolve() != Path(bridge.get(rel, str(path))):
            raise ValueError(f"wrong import origin {name}: {path}")
        origins[name] = str(path.resolve())
    result={"origins":origins, "distributions":dict(distributions)}
    if config.get("native"):
        from cryptography.hazmat.backends.openssl.backend import backend
        version=backend.openssl_version_text().split()
        native=config["native"]
        if version[:2]!=["OpenSSL",native["openssl"]]:
            raise ValueError("unexpected bundled OpenSSL version")
        for row in native["members"].values():
            with (packages/row["path"]).open("rb") as stream:
                if hashlib.file_digest(stream,"sha256").hexdigest()!=row["sha256"]:
                    raise ValueError("unexpected native wheel member")
        mapped={line.split(maxsplit=5)[-1] for line in Path("/proc/self/maps").read_text().splitlines()}
        if not all(row["path"] in mapped for row in system.values()):
            raise ValueError("missing verified native mappings: "+str([k for k,v in system.items() if v["path"] not in mapped]))
        result["native"]={"openssl":native["openssl"],"members":native["members"],"system":system}
    return result


if __name__ == "__main__":
    print(json.dumps(probe(*json.loads(sys.argv[1])), sort_keys=True))
