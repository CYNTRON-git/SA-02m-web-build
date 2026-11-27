"""Verify the official Ubuntu CVE Tracker Git objects used for unavailable web records."""
from functools import lru_cache
import hashlib
import re
import subprocess
import time

from .providers import require

REPOSITORY = "https://git.launchpad.net/~ubuntu-security/ubuntu-cve-tracker/+git/ubuntu-cve-tracker"
DIRECTORIES = ("active", "retired", "ignored")
CVE = re.compile(r"CVE-[0-9]{4}-[0-9]{4,}")
OID = re.compile(r"[0-9a-f]{40}")
PRIORITIES = {"negligible":"INFO", "low":"LOW", "medium":"MODERATE", "high":"HIGH", "critical":"CRITICAL"}


def cvss_metric(module, vector):
    try:
        if vector.startswith(("CVSS:3.0/","CVSS:3.1/")):
            return module.CVSS3(vector)
        require(vector.startswith("CVSS:4.0/"), "unsupported Ubuntu tracker CVSS version")
        return module.CVSS4(vector)
    except module.exceptions.CVSSError as exc:
        raise ValueError("invalid Ubuntu tracker CVSS vector") from exc


def verified_object(kind, digest, body):
    header = (kind + " " + str(len(body))).encode() + b"\0"
    require(hashlib.sha1(header + body).hexdigest() == digest, "Ubuntu tracker Git " + kind + " identity mismatch")
    return body


def tree(body):
    entries = {}
    offset = 0
    while offset < len(body):
        end = body.index(b"\0", offset)
        mode, name = body[offset:end].split(b" ", 1)
        digest = body[end+1:end+21]
        require(name and name not in entries and len(digest) == 20, "invalid Ubuntu tracker tree")
        entries[name] = (mode, digest.hex())
        offset = end + 21
    return entries


class Tracker:
    def __init__(self, folder, snapshot, pinned, ttl, cvss):
        self.folder = folder
        self.commit = pinned
        self.cvss = cvss
        require(OID.fullmatch(pinned) and snapshot["schema"] == 1 and snapshot["repository"] == REPOSITORY
                and snapshot["commit"] == pinned and 0 <= time.time() - snapshot["started"] <= ttl,
                "invalid or expired Ubuntu tracker revision")
        header = self.object("commit", pinned).split(b"\n\n", 1)[0]
        roots = re.findall(rb"^tree ([0-9a-f]{40})$", header, re.M)
        stamps = re.findall(rb"^committer [^\n]* ([0-9]+) [+-][0-9]{4}$", header, re.M)
        require(len(roots) == len(stamps) == 1 and 0 <= snapshot["started"]-int(stamps[0])
                and 0 <= time.time()-int(stamps[0]) <= ttl, "unknown or expired Ubuntu tracker commit date")
        root = tree(self.object("tree", roots[0].decode()))
        self.directories = {}
        for name in DIRECTORIES:
            mode, digest = root[name.encode()]
            require(mode == b"40000", "invalid Ubuntu tracker directory")
            self.directories[name] = tree(self.object("tree", digest))

    def object(self, kind, digest):
        require(OID.fullmatch(digest), "invalid Ubuntu tracker object name")
        path = self.folder / "objects" / digest
        limit = 16*1024*1024 if kind == "tree" else 1024*1024
        require(path.is_file(), "missing Ubuntu tracker Git "+kind+" "+digest)
        require(path.stat().st_size <= limit, "oversize Ubuntu tracker Git object")
        return verified_object(kind, digest, path.read_bytes())

    @lru_cache(maxsize=2048)
    def record(self, cve):
        require(CVE.fullmatch(cve), "invalid Ubuntu tracker CVE identity")
        matches = [(name, entries[cve.encode()]) for name, entries in self.directories.items() if cve.encode() in entries]
        require(len(matches) == 1, "missing or ambiguous Ubuntu tracker path")
        name, (mode, digest) = matches[0]
        require(mode == b"100644", "unexpected Ubuntu tracker CVE file mode")
        raw = self.object("blob", digest).decode("utf-8")
        require(re.findall(r"^Candidate: ([^\n]+)$", raw, re.M) == [cve], "Ubuntu tracker Candidate mismatch")
        return raw, {"repository":REPOSITORY, "commit":self.commit, "path":name+"/"+cve, "git_blob":digest}

    def row(self, cve, package):
        raw, proof = self.record(cve)
        priorities = re.findall(r"^Priority: ([^\n]+)$", raw, re.M)
        overrides = re.findall(r"^Priority_" + re.escape(package) + r": ([^\n]+)$", raw, re.M)
        require(len(priorities) == 1 and priorities[0] in PRIORITIES and overrides in ([],priorities),
                "unknown or conflicting Ubuntu tracker priority")
        lines = re.findall(r"^noble_" + re.escape(package) + r": ([^\n]+)$", raw, re.M)
        require(len(lines) == 1, "missing or ambiguous Ubuntu tracker Noble source")
        status = re.fullmatch(r"(needed|deferred|ignored|needs-triage|released)(?: \(([^\n()]*)\))?", lines[0])
        require(status is not None, "unsupported Ubuntu tracker Noble status")
        description = status[2] or ""
        if status[1] == "released":
            require(re.fullmatch(r"(?:[0-9]+:)?[0-9][0-9A-Za-z.+~:-]*", description),
                    "ambiguous Ubuntu tracker released version")
        vectors = re.findall(r"^CVSS:[ \t]*\n((?:[ \t][^\n]*\n?)+)", raw, re.M)
        require(len(vectors) == 1, "missing or ambiguous Ubuntu tracker CVSS")
        levels = set()
        generations = set()
        for line in vectors[0].splitlines():
            score = re.fullmatch(r"\s+[A-Za-z0-9_.-]+: (CVSS:(?:3\.[01]|4\.0)/\S+) \[([0-9]+(?:\.[0-9]+)?) (NONE|LOW|MEDIUM|HIGH|CRITICAL)\]", line)
            require(score is not None, "unsupported Ubuntu tracker CVSS metric")
            parsed = cvss_metric(self.cvss,score[1])
            require(parsed.scores()[0] == float(score[2]) and parsed.severities()[0].upper() == score[3],
                    "inconsistent Ubuntu tracker CVSS metric")
            levels.add(score[3])
            generations.add(score[1].removeprefix("CVSS:").partition(".")[0])
        require(levels, "empty Ubuntu tracker CVSS metrics")
        return {"status":status[1], "description":description, "priority":PRIORITIES[priorities[0]],
                "upstream_levels":sorted(levels), "tracker_git":dict(proof,cvss_generations=sorted(generations))}


def decision_shape(row):
    # The web tracker can expose a different set of upstream metrics for CVSS 3 and 4.
    return row["status"],row["priority"],tuple(row["upstream_levels"]),tuple(row["tracker_git"]["cvss_generations"])


def decision_fields(row):
    return tuple(row[k] for k in ("status", "description", "priority", "upstream_levels"))


def compatible_direct_record(record, row, cvss):
    """Confirm Ubuntu priority and the CVSS decision threshold against this exact OSV CVE."""
    priorities = {item.get("score") for item in record.get("severity",[]) if item.get("type") == "Ubuntu"}
    if len(priorities) != 1 or PRIORITIES.get(next(iter(priorities))) != row["priority"]:
        return False
    vectors = [item for item in record.get("severity",[]) if item.get("type") != "Ubuntu"]
    versions = {"CVSS:3.0":"CVSS_V3","CVSS:3.1":"CVSS_V3","CVSS:4.0":"CVSS_V4"}
    if not vectors or any(versions.get(item["score"].partition("/")[0]) != item.get("type") for item in vectors):
        return False
    upstream = {cvss_metric(cvss,item["score"]).severities()[0].upper() for item in vectors}
    return upstream == set(row["upstream_levels"])


def confirmed_release(package, installed, fixed, record):
    fixes = {event["fixed"] for item in record.get("affected",[])
             if (item["package"]["name"],item["package"]["ecosystem"]) == (package,"Ubuntu:24.04:LTS")
             for group in item.get("ranges",[]) if group.get("type") == "ECOSYSTEM"
             for event in group["events"] if "fixed" in event}
    require(fixed in fixes, "Ubuntu tracker fix differs from direct OSV Noble range")
    earlier = subprocess.run(["/usr/bin/dpkg","--compare-versions",installed,"lt",fixed],
                             timeout=5,capture_output=True)
    require(earlier.returncode == 0, "Ubuntu tracker fix does not exceed the installed Debian version")


def parity(tracker, websites, names, read_web_row):
    """Admit a decision shape only when comparable live web rows agree with the Git parser."""
    matching = set()
    conflicting = set()
    audit = {"commit":tracker.commit, "matched":0, "differences":[], "uncomparable":0, "used":[]}
    for cve, website in sorted(websites.items()):
        if "unavailable" in website:
            continue
        for package in sorted({p.get("name") for p in website.get("packages",[])} & names):
            try:
                web = read_web_row(package,cve,website)
                if web is None:
                    continue
                git = tracker.row(cve,package)
            except (ValueError,OSError,KeyError,TypeError,UnicodeError):
                audit["uncomparable"] += 1
                continue
            shape = decision_shape(git)
            if decision_fields(web) == decision_fields(git):
                matching.add(shape)
                audit["matched"] += 1
            else:
                conflicting.add(shape)
                audit["differences"].append({"cve":cve,"package":package,"fields":[k for k in
                    ("status","description","priority","upstream_levels") if web[k] != git[k]]})
    return matching - conflicting, audit
