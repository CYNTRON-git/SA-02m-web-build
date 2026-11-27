"""Bind advisory requests to actual delivered copies and observed device inventories."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import tarfile
import time
import zipfile
from email.parser import BytesParser

from .providers import canonical, require, sha
from .homekit_absence import matches as absent_homekit


def normalize(name):
    return re.sub("[-_.]+","-",name).lower()


class Inventory:
    def __init__(self):
        self.paths=defaultdict(list)
        self.inputs={}
        self.gaps=defaultdict(list)
        self.devices={}
        self.ubuntu=set()
        self.node_versions=defaultdict(list)

    def file(self, label, path):
        require(path.is_file() and not path.is_symlink(),"file missing "+label)
        self.inputs[label]=sha(path)
        return path

    def json(self,label,path):
        return json.loads(self.file(label,path).read_bytes())

    def add(self, ecosystem, name, version, scope, location):
        require(isinstance(name,str) and name and isinstance(version,str) and version,"missing package identity")
        key=(ecosystem,normalize(name) if ecosystem=="PyPI" else name,version)
        self.paths[key].append({"scope":scope,"path":location})

    def rust_sbom(self,wheel,scope):
        sbom,=[p for p in wheel.namelist() if p.endswith("/cryptography-rust.cyclonedx.json")]
        cargo=json.loads(wheel.read(sbom))
        require(cargo["bomFormat"]=="CycloneDX" and cargo["components"],"missing Rust component inventory")
        local=[]
        for component in cargo["components"]:
            name,version,purl=(component[k] for k in ("name","version","purl"))
            if purl==f"pkg:cargo/{name}@{version}?download_url=file://{name}" and name.startswith("cryptography-"):
                local.append({"name":name,"version":version,"parent":"cryptography"})
            else:
                require(purl==f"pkg:cargo/{name}@{version}","unmapped Rust component "+purl)
                self.add("crates.io",name,version,scope,sbom+":"+purl)
        return local

    def homekit_wheel(self,path):
        self.file("HomeKit crypto ARM wheel",path)
        digest=sha(path)
        require(digest in self.hk_hashes["cryptography"],"HomeKit crypto wheel outside lock")
        with zipfile.ZipFile(path) as wheel:
            meta,=[n for n in wheel.namelist() if n.endswith(".dist-info/METADATA")]
            attrs=BytesParser().parsebytes(wheel.read(meta))
            require(attrs["Name"]=="cryptography" and attrs["Version"]==self.hk["cryptography"] and
                    path.name.endswith("-cp311-abi3-manylinux_2_31_armv7l.whl"),"HomeKit wheel identity")
            elf,=[n for n in wheel.namelist() if n.endswith(".so")]
            require(elf=="cryptography/hazmat/bindings/_rust.abi3.so","unmapped HomeKit ELF")
            sboms={n:hashlib.sha256(wheel.read(n)).hexdigest() for n in wheel.namelist()
                   if "/sboms/" in n and n.endswith(".json")}
            generic,=[n for n in sboms if n.endswith("/sbom.json")]
            component,=json.loads(wheel.read(generic))["components"]
            version=component["version"]
            require(component["name"]=="openssl" and component["purl"].startswith(f"pkg:generic/openssl@{version}?"),
                    "HomeKit wheel native SBOM")
            return {"version":version,"wheel":path.name,"wheel_sha256":digest,"elf":elf,
                    "elf_sha256":hashlib.sha256(wheel.read(elf)).hexdigest(),"sboms":sboms,
                    "pyca_workspace_crates":self.rust_sbom(wheel,"homekit")}

    def project(self,repo,artifact,node):
        data=repo/"opt/sa02m-python-manager"
        packages=self.json("python manifest",data/"runtime.json")
        require(re.fullmatch("[a-f0-9]{64}",packages["wheelset"]),"invalid runtime wheelset")
        wheel_ids=sorted((p["file"],p["sha256"]) for p in packages["packages"].values())
        require(hashlib.sha256(canonical(wheel_ids)).hexdigest()==packages["wheelset"],"wheelset directory not content-addressed")
        wheelhouse=data/"wheelhouse"/packages["wheelset"]
        self.wheelhouse=wheelhouse
        self.bridge=self.json("Python bridge",data/"bridge.json")
        self.consumers=self.json("Python consumers.json",data/"consumers.json")
        self.file("Python consumer launcher",repo/"usr/local/libexec/sa02m-python-consumer")
        for rel in ("consumers.json","runtime.py","probe.py","launch.py","native_guard.py","integrity.py"):
            self.file("Python "+rel,data/rel)
        expected_files=set()
        for group,config in packages["groups"].items():
            lock=self.file("python "+group+" lock",data/(group+".lock"))
            expected=[]
            for name in config["packages"]:
                row=packages["packages"][name]
                wheel=self.file("wheel "+name,wheelhouse/row["file"])
                require(self.inputs["wheel "+name]==row["sha256"] and wheel.stat().st_size==row["bytes"],"wheel mismatch "+name)
                with zipfile.ZipFile(wheel) as z:
                    meta,=[n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
                    attrs=BytesParser().parsebytes(z.read(meta))
                    require((normalize(attrs["Name"]),attrs["Version"])==(name,row["version"]),"wheel identity "+name)
                expected_files.add(wheel.name)
                expected.append(f"{name}=={row['version']} --hash=sha256:{row['sha256']}")
                self.add("PyPI",name,row["version"],"python",group+":"+wheel.name)
            require(lock.read_text().splitlines()==expected,"Python lock mismatch "+group)
        require({p.name for p in wheelhouse.iterdir()}==expected_files,"wheelhouse completeness")
        self.runtime=packages
        yaml_source=packages["packages"]["pyyaml"]["source"]
        recipe=self.file("PyYAML build recipe",repo/"vendor-src/python/build-pure-yaml.sh")
        require(sha(recipe)==yaml_source["recipe_sha256"],"unrecognized PyYAML build recipe")
        for t in yaml_source["tools"]:
            self.add("PyPI",t["name"],t["version"],"development","PyYAML build:"+t["file"])
        cffi=packages["packages"]["cffi"]["source"]
        for rel,digest in cffi["recipe_sha256"].items():
            path=self.file("CFFI build "+rel,repo/"vendor-src/python/cffi"/rel)
            require(sha(path)==digest,"CFFI builder input changed "+rel)
        for t in cffi["tools"]:
            self.add("PyPI",t["name"],t["version"],"development","CFFI build:"+t["file"])
        with zipfile.ZipFile(wheelhouse/packages["packages"]["cryptography"]["file"]) as wheel:
            for member in packages["groups"]["opcua"]["native"]["members"].values():
                if member["path"] in wheel.namelist():
                    require(hashlib.sha256(wheel.read(member["path"])).hexdigest()==member["sha256"],"wheel ELF mismatch")
            self.local_crates=self.rust_sbom(wheel,"opcua-native")
        semver=node.read_json(repo/"vendor-src/nodered/admission-policy.json")
        digests,expected,inventory=node.collect_inputs(repo,artifact,semver)
        self.inputs.update({"node-red "+k:v for k,v in digests.items()})
        for source,entries in expected.items():
            for (name,version),locations in entries.items():
                for location in locations:
                    self.add("npm",name,version,"node-red",location)
        dev=self.json("development npm lock",repo/"scripts/dev/package-lock.json")
        self.file("development npm manifest",repo/"scripts/dev/package.json")
        for path,row in dev["packages"].items():
            if path:
                require("node_modules/" in path and row.get("integrity"),"dev lock entry "+path)
                self.add("npm",row.get("name") or path.rsplit("node_modules/",1)[-1],row["version"],"development",path)
        hk=self.file("HomeKit lock",repo/"opt/sa02m-homekit/requirements.lock").read_text()
        self.hk={}
        self.hk_hashes={}
        for line in hk.replace("\\\n"," ").splitlines():
            if not line.strip() or line.startswith(("#","--")): continue
            match=re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s;]+)((?:\s+--hash=sha256:[0-9a-f]{64})+)\s*",line)
            require(match,"unsupported HomeKit lock entry")
            pkg,version=normalize(match[1]),match[2]
            require(pkg not in self.hk,"duplicate HomeKit pin")
            self.hk[pkg]=version
            self.hk_hashes[pkg]=set(re.findall(r"--hash=sha256:([a-f0-9]{64})",match[3]))
            self.add("PyPI",pkg,version,"homekit","requirements.lock")
        require(self.hk,"empty HomeKit lock")
        release=self.json("vendor payloads",repo/"etc/vendor-payloads.json")["packages"]["node-red"]
        native=release["node"]
        path=self.file("native node archive",artifact/native["filename"])
        require(self.inputs["native node archive"]==native["sha256"] and path.stat().st_size==native["size"],"native Node mismatch")
        # The installer can copy the complete upstream Node distribution.
        # npm/corepack and their bundled copies are a distinct physical tree.
        self.native_npm={}
        with tarfile.open(path,"r|xz") as archive:
            for index,member in enumerate(archive):
                require(index<100000 and (member.name==native["root"] or member.name.startswith(native["root"]+"/")),"invalid upstream Node layout")
                if member.isfile() and member.name==native["root"]+"/bin/node":
                    self.native_binary_sha=hashlib.file_digest(archive.extractfile(member),"sha256").hexdigest()
                if member.isfile() and member.name.endswith("/package.json"):
                    require(member.size<=2*1024*1024,"oversized Node package metadata")
                    obj=json.load(archive.extractfile(member))
                    if isinstance(obj.get("name"),str) and isinstance(obj.get("version"),str):
                        self.add("npm",obj["name"],obj["version"],"native-node",member.name)
                        self.native_npm.setdefault((obj["name"],obj["version"]),[]).append(member.name)
        require(self.native_npm,"empty upstream Node npm inventory")
        self.native_version=release["node_version"]
        return self.native_version

    def external_node(self, name, lineage, packages, versions):
        if not lineage:
            self.gaps["external-node"].append(name+": Node installation history unavailable")
            return
        tree=self.json("Node lineage "+name,lineage)
        binaries=[p for p in tree["candidates"] if p["path"].endswith("/bin/node") and p["exists"]]
        self.devices[name]["node_binaries"]=[p["path"] for p in binaries]
        if not binaries and not tree["packages"] and not tree["managed_root"]["exists"] and not any(tree["commands"].values()):
            return
        observed=self.json("Node versions "+name,versions)["paths"] if versions else {}
        for row in binaries:
            digest=row["sha256"]
            path=row["path"]
            identity=observed.get(path,{})
            if digest==self.native_binary_sha:
                version=self.native_version
            elif identity.get("sha256")==digest and re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+",identity.get("version","")):
                version=identity["version"].removeprefix("v")
            else:
                self.gaps["external-node"].append(f"{name}:{path}: executable version unverified")
                continue
            self.node_versions[version].append(name+":"+path)
        if not packages:
            self.gaps["external-node"].append(name+": installed npm package inventory unavailable")
            return
        census=self.json("installed npm trees "+name,packages)
        actual={}
        for folder in census["roots"]:
            if not folder["exists"]:
                continue
            for row in folder["packages"]:
                path=row["path"]
                require(path.startswith(folder["path"]+"/") and path.endswith("/package.json") and
                        re.fullmatch("[0-9a-f]{64}",row["sha256"]), "invalid physical Node package")
                if row.get("name") and row.get("version"):
                    self.add("npm",row["name"],row["version"],"external-node",name+":"+path)
                    actual[path]=(row["name"],row["version"],row["sha256"])
        for row in tree["packages"]:
            if row["exists"]:
                require(actual.get(row["path"])==(row["name"],row["version"],row["sha256"]),"installed Node CLI tree incomplete")

    def board(self,name,inventory,kernel,runtime,homekit_integrity=None,system_python=None,homekit_origins=None,homekit_native=None,homekit_absence=None):
        self.devices[name]={}
        data=self.json("board "+name,inventory)
        require(data["dpkg"]["rc"]==0 and data["kernel_release"],"dpkg/kernel unavailable "+name)
        count=0
        for line in data["dpkg"]["stdout"].splitlines():
            state,binary,version,arch,source,source_version=line.split("\t")
            if state!="installed": continue
            self.add("Ubuntu:24.04:LTS",source,source_version,"ubuntu",f"{name}:{binary}={version}/{arch}")
            self.ubuntu.add((source,source_version))
            count+=1
        require(count>0,"empty dpkg inventory")
        self.devices[name]["dpkg_count"]=count
        if system_python:
            census=self.json("system Python "+name,system_python)
            for i,p in enumerate(census["distributions"]):
                if p["origin"].startswith("/usr/local/"):
                    self.add("PyPI",p["name"],p["version"],"python-ambient",f"{name}:{p['origin']}:{p['name']}:{i}")
        else:
            self.gaps["python-ambient"].append(name+": global Python package census unavailable")
        if kernel:
            boot=self.json("boot artifacts "+name,kernel)
            images=[p for p in boot["files"] if p["file"]=="zImage"]
            require(len(images)==1 and re.fullmatch("[a-f0-9]{64}",images[0]["sha256"]) and
                    images[0]["bytes"]>0 and boot["kernel_release"]==data["kernel_release"],"kernel census changed")
            self.devices[name]["kernel"]={"release":data["kernel_release"],"disk_candidate":images[0]}
        self.gaps["vendor-kernel"].append(f"{name} {data['kernel_release']}: vendor advisory mapping unavailable")
        h=data["homekit"]
        self.devices[name]["homekit_installed"]=h["exists"]
        if homekit_absence:
            receipt = self.json("HomeKit absence " + name, homekit_absence)
            self.devices[name]["homekit_absence_verified"] = absent_homekit(receipt, data, name, time.time())
        if h["exists"]:
            artifact=getattr(self,"hk_native",None)
            if not artifact or not homekit_native:
                self.gaps["homekit"].append(name+": installed HomeKit native ELF and SBOM identities unavailable")
            else:
                observed=self.json("HomeKit native origins "+name,homekit_native)
                base="/opt/sa02m-homekit-venv/lib/python3.12/site-packages/"
                actual={r["path"].removeprefix(base):r["sha256"] for r in observed["sboms"]}
                require(observed["uid"]==33 and observed["version"]==self.hk["cryptography"] and
                        observed["openssl"].split()[:2]==["OpenSSL",artifact["version"]] and
                        observed["elf"]["path"]==base+artifact["elf"] and
                        observed["elf"]["sha256"]==artifact["elf_sha256"] and actual==artifact["sboms"],
                        "installed HomeKit native bytes differ from locked wheel")
                self.devices[name]["homekit_native"]={"elf_sha256":artifact["elf_sha256"],"openssl":artifact["version"]}
            proof=self.json("HomeKit integrity "+name,homekit_integrity) if homekit_integrity else {}
            pip_proof=proof.get("pip",{})
            records=proof.get("records",[])
            documented={r["distribution"] for r in records if r.get("checked",0)>0 and not r.get("bad")
                        and re.fullmatch("[a-f0-9]{64}",r.get("record_sha256",""))}
            if not proof.get("ok") or not all(Path(r["metadata"]["path"]).parent.name in documented for r in h["distributions"]):
                self.gaps["homekit"].append(name+": installed RECORD integrity unavailable")
            found=set()
            for row in h["distributions"]:
                pkg,version=normalize(row["Name"]),row["Version"]
                location=row["metadata"]["path"]
                require(row["metadata"]["sha256"] and pkg not in found,"invalid HomeKit distribution")
                found.add(pkg)
                if pkg=="pip":
                    # Version 24.0 alone cannot distinguish Ubuntu-patched bytes from PyPI.
                    if not (proof.get("ok") and proof.get("pip_wheel_sha256")==self.runtime["target"]["bootstrap"]["sha256"]
                            and pip_proof.get("matched",0)>0 and pip_proof.get("extra")==[] and pip_proof.get("mismatches")==[]):
                        self.gaps["homekit"].append(f"{name} {location}: distro bootstrap origin unverified")
                else:
                    self.add("PyPI",pkg,version,"homekit",name+":"+location)
            inherited=self.json("HomeKit inherited origins "+name,homekit_origins) if homekit_origins else {}
            if inherited:
                modules={m["name"]:m for m in inherited["modules"]}
                approved={"/usr/lib/python3/dist-packages/"+row["path"]:row["sha256"] for row in self.bridge["files"]}
                cffi=modules.get("_cffi_backend",{})
                crypto=modules.get("cryptography",{})
                if not (approved.get(cffi.get("resolved"))==cffi.get("sha256") and cffi.get("sha256") and
                        crypto.get("resolved","").startswith(inherited["prefix"]+"/lib/python3.12/site-packages/cryptography/")):
                    self.gaps["homekit"].append(name+": HomeKit native crypto/cffi origins unverified")
                paho=modules.get("paho.mqtt.client",{})
                package=self.runtime["packages"]["paho-mqtt"]
                with zipfile.ZipFile(self.wheelhouse/package["file"]) as wheel:
                    digest=hashlib.sha256(wheel.read("paho/mqtt/client.py")).hexdigest()
                if paho.get("sha256")==digest and paho.get("resolved","").endswith("/paho/mqtt/client.py"):
                    self.add("PyPI","paho-mqtt",package["version"],"homekit",name+":"+paho["resolved"])
                else:
                    self.gaps["homekit"].append(name+": inherited Paho client unverified")
            else:
                self.gaps["homekit"].append(name+": actual dependent import origins missing")
            for n in sorted(set(self.hk)-found):
                if n=="h11" and inherited and self.verify_homekit_h11(inherited):
                    self.add("PyPI",n,self.hk[n],"homekit",name+":"+inherited["h11_distribution"]["metadata_path"])
                else:
                    self.gaps["homekit"].append(name+": inherited HomeKit distribution requires proof "+n)
            for row in h["distributions"]:
                n=normalize(row["Name"])
                if n in self.hk:
                    require(self.hk[n]==row["Version"],"HomeKit lock/install mismatch "+n)
        if not runtime:
            self.gaps["python"].append(name+": native prepared inventory missing")
            return
        # This is the native manager's actual verified receipt, not pip freeze
        # from an unrelated ambient interpreter.
        receipt=self.json("native Python receipt "+name,runtime)
        catalogue={"runtime.json":self.inputs["python manifest"],"bridge.json":self.inputs["Python bridge"]}
        for filename in ("runtime.py","probe.py","launch.py","integrity.py","native_guard.py","consumers.json"):
            catalogue[filename]=self.inputs["Python "+filename]
        for group in self.runtime["groups"]:
            catalogue[group+".lock"]=self.inputs["python "+group+" lock"]
        identity=hashlib.sha256(json.dumps(catalogue,sort_keys=True).encode()).hexdigest()
        if (receipt["manifest_sha256"]!=identity or not re.fullmatch("[a-f0-9]{64}",receipt["release_id"])
                or not re.fullmatch("[a-f0-9]{64}",receipt["guard_sha256"])
                or set(receipt["inventories"])!=set(self.runtime["groups"])):
            self.gaps["python"].append(name+": native receipt from a different runtime")
            return
        for group,config in self.runtime["groups"].items():
            result=receipt["inventories"][group]
            actual=dict(result["distributions"])
            if config["bridge"]:
                require(actual.pop("cryptography")==self.runtime_bridge_version(),"wrong distro crypto version")
            expected={p:self.runtime["packages"][p]["version"] for p in config["packages"]}
            require(actual==expected and re.fullmatch("[0-9a-f]{64}",result["tree_sha256"]),"runtime lock/install mismatch "+group)
            self.check_origins(receipt["release_id"],group,config,result["origins"])
            if config.get("native"):
                self.check_native(config["native"],result.get("native",{}))
                self.devices[name]["opcua_native"]={"release_id":receipt["release_id"],"manifest_sha256":identity,
                    "receipt_sha256":self.inputs["native Python receipt "+name],
                    "tree_sha256":result["tree_sha256"],**result["native"]}
            for n,v in actual.items():
                self.add("PyPI",n,v,"python",f"{name}:{group}:{result['tree_sha256']}")

    def check_native(self,expected,actual):
        require(actual.get("openssl")==expected["openssl"] and actual.get("members")==expected["members"],
                "private native wheel identity mismatch")
        observed=actual.get("system",{})
        require(set(observed)==set(expected["system"]),"incomplete native library inventory")
        for name,pin in expected["system"].items():
            row=observed[name]
            version=row["version"]
            require(version in pin["versions"] and row=={"path":pin["path"],"binary":pin["binary"],
                    "version":version,**pin["versions"][version]},"native library identity mismatch "+name)

    def runtime_bridge_version(self):
        # The distro bridge bytes and version are owned by the runtime catalogue.
        return self.bridge["packages"]["python3-cryptography"]["version"].split("-")[0]

    def check_origins(self, release, group, config, origins):
        expected=set(config["imports"])
        approved={}
        if config["bridge"]:
            native={"cryptography":"cryptography/__init__.py","cryptography.hazmat.bindings._rust":"cryptography/hazmat/bindings/_rust.abi3.so"}
            cffi,=[f["path"] for f in self.bridge["files"] if f["path"].startswith("_cffi_backend.") and f["path"].endswith(".so")]
            native["_cffi_backend"]=cffi
            approved={k:"/usr/lib/python3/dist-packages/"+v for k,v in native.items()}
            expected.update(approved)
        require(set(origins)==expected,"runtime import inventory mismatch "+group)
        location=f"/releases/{release}/{group}/lib/python{self.runtime['target']['python']}/site-packages/"
        for module,source in origins.items():
            path=PurePosixPath(source)
            require(path.is_absolute() and str(path)==source and ".." not in path.parts and
                    (source==approved[module] if module in approved else location in source),
                    "unexpected native import origin "+module)

    def verify_homekit_h11(self, origins):
        package=self.runtime["packages"]["h11"]
        row=origins["h11_distribution"]
        base=PurePosixPath(row["metadata_path"]).parent
        if (row["version"]!=self.hk["h11"] or package["version"]!=row["version"] or row.get("bad")
                or package["sha256"] not in self.hk_hashes["h11"]):
            return False
        mods={m["name"]:m for m in origins["modules"]}
        if mods["h11"]["resolved"]!=str(base/"h11"/"__init__.py"):
            return False
        observed={PurePosixPath(item["path"]).relative_to(base).as_posix():item["sha256"]
                  for item in row["files"] if item["record_match"]}
        with zipfile.ZipFile(self.wheelhouse/package["file"]) as wheel:
            expected={p:hashlib.sha256(wheel.read(p)).hexdigest() for p in wheel.namelist()
                      if not p.endswith(("/","/RECORD"))}
        extras=set(observed)-set(expected)
        return (all(observed.get(p)==digest for p,digest in expected.items()) and
                extras<= {PurePosixPath(row["metadata_path"]).name+"/INSTALLER"} and
                mods["h11"]["sha256"]==expected["h11/__init__.py"])
