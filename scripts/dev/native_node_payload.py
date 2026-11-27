"""Preserve the upstream ARM runtime and replace only its complete npm bundle."""
import copy
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def derive(upstream, upstream_hash, runtime, runtime_hash, root, npm_version, out):
    if sha(upstream)!=upstream_hash or sha(runtime)!=runtime_hash:
        raise ValueError("native Node build input hash mismatch")
    with tempfile.TemporaryDirectory(dir=out.parent) as staging:
        candidate=Path(staging)/out.name
        write_archive(upstream, runtime, root, npm_version, candidate)
        digest=sha(candidate)
        candidate.replace(out)
        return digest


def write_archive(upstream, runtime, root, npm_version, out):
    npm_dir=root+"/lib/node_modules/npm"
    incoming="node-red/node_modules/npm"
    seen=set()
    def emit(dest, src, member, name):
        if name in seen or name.startswith("/") or ".." in name.split("/"):
            raise ValueError("invalid or repeated native archive member "+name)
        seen.add(name)
        entry=copy.copy(member)
        entry.name=name
        entry.pax_headers={k:v for k,v in member.pax_headers.items() if k not in {"path","linkpath"}}
        if entry.islnk():
            raise ValueError("unexpected hard link "+name)
        stream=src.extractfile(member) if member.isfile() else None
        dest.addfile(entry,stream)
    with tarfile.open(out,"w:xz",preset=6,format=tarfile.PAX_FORMAT) as target:
        with tarfile.open(upstream,"r|xz") as src:
            for m in src:
                if m.name==npm_dir or m.name.startswith(npm_dir+"/"):
                    continue
                if m.name!=root and not m.name.startswith(root+"/"):
                    raise ValueError("unexpected upstream Node root")
                emit(target,src,m,m.name)
        with tarfile.open(runtime,"r:gz") as src:
            metadata=json.load(src.extractfile(incoming+"/package.json"))
            if (metadata["name"],metadata["version"])!=("npm",npm_version):
                raise ValueError("wrong replacement npm")
            for m in src:
                if m.name==incoming or m.name.startswith(incoming+"/"):
                    emit(target,src,m,npm_dir+m.name[len(incoming):])
    required={root+"/bin/"+n for n in ("node","npm","npx","corepack")}
    required.update({npm_dir+"/bin/"+n for n in ("npm-cli.js","npx-cli.js")})
    if not required<=seen:
        raise ValueError("derived runtime missing public command or CLI target")
