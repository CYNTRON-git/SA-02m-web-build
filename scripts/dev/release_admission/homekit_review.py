"""Apply the independently registered HomeKit review to its exact optional ARM artifact."""
import hashlib
import json
import re

from .inventory import normalize
from .openssl import cve_url
from .providers import canonical, require


SOURCE_ROOTS = ("opt/sa02m-homekit", "opt/sa02m-alice", "opt/sa02m-rules", "opt/sa02m-modbus-mqtt", "opt/sa02m-spodes")
ALICE_MARKER = "opt/sa02m-alice/__init__.py"
ALICE_MARKER_SHA = "2b097e7de3d197820c7bea512dd1aeaa482dc028f287579ae3fd5028cfff9745"


def verified_file(inv, label, path, digest):
    path = inv.file(label, path)
    require(inv.inputs[label] == digest, "HomeKit review input changed: " + label)
    return path


def verify_graph(inv, repo, folder, review, wheelhouse):
    def manifest(name, key):
        return json.loads(verified_file(inv, "HomeKit " + name, folder / name, review[key]).read_bytes())

    product = manifest("product-files.json", "product_files_sha256")
    packages = manifest("package-files.json", "package_files_sha256")
    manifest("candidate.json", "builder_inputs_sha256")
    discovered = set()
    for root in SOURCE_ROOTS:
        for path in (repo / root).rglob("*"):
            if {"tests", "__pycache__"}.intersection(path.relative_to(repo / root).parts):
                continue
            require(not path.is_symlink() and path.suffix not in (".so", ".pyd", ".pth", ".pyc"), "unreviewed HomeKit import path")
            if path.suffix == ".py":
                discovered.add(path.relative_to(repo).as_posix())
    require(discovered == {p for p in product if p.endswith(".py")} | {ALICE_MARKER}, "HomeKit product module membership changed")
    verified_file(inv, "HomeKit source " + ALICE_MARKER, repo / ALICE_MARKER, ALICE_MARKER_SHA)
    for unit_root in ("etc/systemd/system", "usr/lib/systemd/system", "lib/systemd/system"):
        for suffix in ("sa02m-homekit.service.d", "sa02m-.service.d", "service.d"):
            override = repo / unit_root / suffix
            require(not override.exists() and not override.is_symlink(), "unreviewed HomeKit service drop-in")
    for name, entry in product.items():
        path = verified_file(inv, "HomeKit source " + name, repo / name, entry["sha256"])
        require(path.stat().st_size == entry["bytes"], "HomeKit source size changed")
    for name, digest in review["extra_source_files"].items():
        verified_file(inv, "HomeKit source " + name, repo / name, digest)

    wheels = {n: row for n, row in packages["wheels"].items() if n.endswith("-any.whl") or "armv7l" in n}
    require(wheelhouse.is_dir() and not wheelhouse.is_symlink() and {p.name for p in wheelhouse.glob("*.whl")} == set(wheels),
            "release HomeKit ARM wheelhouse differs from review")
    require({normalize(n.split("-")[0]) for n in wheels} == set(inv.hk), "HomeKit ARM lock coverage changed")
    for name, row in wheels.items():
        path = verified_file(inv, "HomeKit ARM wheel " + name, wheelhouse / name, row["sha256"])
        require(path.stat().st_size == row["bytes"] and row["sha256"] in inv.hk_hashes[normalize(name.split("-")[0])],
                "HomeKit ARM wheel differs from lock")


def reviewed_findings(result, review, net, review_digest):
    component = result["component"]
    require(component == review["artifact"] and result["catalogue_records"] == review["upstream_records"]
            and result["compared_semver_ranges"] == review["upstream_compared_semver_ranges"], "HomeKit artifact or catalogue changed")
    other = result["uncategorized_ranges"]
    legacy = review["legacy_ranges"]
    def encode(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    require(len(other) == legacy["count"] and len({r["id"] for r in other}) == legacy["distinct_cves"]
            and hashlib.sha256(encode(sorted(other, key=encode))).hexdigest() == legacy["rows_sha256"]
            and result["gaps"] == [f"{len(other)} upstream custom or non-semver OpenSSL ranges require provider assessment"],
            "HomeKit upstream coverage differs from review")
    reviewed = {r["id"]: r for r in review["records"]}
    findings = result["findings"]
    require(len(reviewed) == len(review["records"]) == len(findings) and set(reviewed) == {f["identifiers"][0] for f in findings},
            "HomeKit advisory membership changed")
    checked = []
    location = "HomeKit lock:" + component["wheel"] + "!" + component["elf"]
    for finding in findings:
        identifier, = finding["identifiers"]
        row = reviewed[identifier]
        url = cve_url(identifier)
        key = hashlib.sha256(canonical([url, None])).hexdigest()
        require(row["status"] == "not_affected" and row["record_url"] == url and net.seen[key]["sha256"] == row["record_sha256"]
                and finding["cna_sha256"] == row["cna_sha256"] and finding["range"] == row["range"] and finding["levels"] == row["levels"]
                and finding["locations"] == [location] and finding["package"] == "OpenSSL" and finding["version"] == component["version"],
                "HomeKit reviewed CVE changed: " + identifier)
        checked.append(dict(finding, applicability=row["status"], justification=row["justification"], prerequisite=row["prerequisite"],
                            review_sha256=review_digest))
    return checked


def apply(result, inv, repo, policy, wheelhouse, net):
    require(set(inv.devices) == {"136"} and inv.devices["136"].get("homekit_absence_verified") is True,
            "HomeKit reviewed absent donor profile unverified")
    folder = repo / "vendor-src/admission/homekit"
    digest = policy["homekit_review_sha256"]
    text = verified_file(inv, "independent HomeKit review", folder / "review.md", digest).read_text()
    block, = re.findall(r"^```json\r?\n(.*?)\r?\n```", text, re.M | re.S)
    review = json.loads(block)
    require(review["upstream_archive_sha256"] == policy["openssl_cna_archive_sha256"], "HomeKit reviewed CNA archive changed")
    verify_graph(inv, repo, folder, review, wheelhouse)
    checked = reviewed_findings(result, review, net, digest)
    return dict(result, findings=checked, gaps=[], reviewed_legacy_ranges=review["legacy_ranges"],
                invalidate_if=review["invalidate_if"], applicability_review_sha256=digest)
