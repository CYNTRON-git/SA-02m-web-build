"""Record and replay bounded requests to named official advisory providers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import ssl
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class Transport:
    HOSTS = {"api.osv.dev", "api.launchpad.net", "ubuntu.com", "api.github.com", "raw.githubusercontent.com"}

    def __init__(self, folder, replay=None):
        self.folder = folder
        self.folder.mkdir(exist_ok=True)
        self.seen = {}
        self.saved = replay
        self.lock = threading.Lock()
        self.deadline = time.monotonic()+1800

    def fetch(self, url, request=None, *, git_blob=None):
        parsed = urlsplit(url)
        require(parsed.scheme=="https" and parsed.netloc in self.HOSTS,"unexpected provider")
        require(time.monotonic() < self.deadline,"scan timeout")
        key = hashlib.sha256(canonical([url, request])).hexdigest()
        path = self.folder/(key+".json")
        cached=self.saved if self.saved is not None else self.seen if key in self.seen else None
        evidence={}
        if cached is not None:
            if key in cached and "error" in cached[key]:
                row=cached[key]
                require(row["url"]==url and row["request"]==request,"invalid recorded provider failure")
                self.seen[key]=row
                raise OSError(row["error"])
            require(key in cached and cached[key]["url"]==url and cached[key]["request"]==request and
                    path.stat().st_size==cached[key]["bytes"]<=8*1024*1024 and
                    sha(path)==cached[key]["sha256"], "recorded response mismatch "+url)
            raw = path.read_bytes()
            if "failures" in cached[key]:
                evidence["failures"]=cached[key]["failures"]
        else:
            req = Request(url, data=None if request is None else canonical(request),
                          headers={"User-Agent":"SA02m-release-admission","Content-Type":"application/json","Accept":"application/json"})
            failures=[]
            for attempt in range(3):
                try:
                    with urlopen(req,timeout=min(25,max(1,self.deadline-time.monotonic()))) as response:
                        require(response.status==200 and response.geturl()==url,"provider HTTP identity "+url)
                        raw=response.read(8*1024*1024+1)
                    break
                except OSError as exc:
                    failures.append(str(exc))
                    if isinstance(exc,HTTPError):
                        temporary=exc.code in {408,429,500,502,503,504}
                    else:
                        temporary=not isinstance(getattr(exc,"reason",exc),ssl.SSLError)
                    delay=2**attempt
                    if not temporary or attempt==2 or time.monotonic()+delay+1>=self.deadline:
                        with self.lock:
                            self.seen[key]={"url":url,"request":request,"error":str(exc),"failures":failures}
                        raise
                    time.sleep(delay)
            if failures:
                evidence["failures"]=failures
            require(len(raw)<=8*1024*1024,"provider response limit")
            path.write_bytes(raw)
        response = json.loads(raw)
        with self.lock:
            require(len(self.seen)<12000,"request count limit")
            self.seen[key] = {"url":url,"request":request,"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),**evidence}
        if git_blob is not None:
            header = b"blob " + str(len(raw)).encode("ascii") + b"\0"
            require((len(raw), hashlib.sha1(header + raw).hexdigest()) == git_blob,
                    "Git blob identity mismatch " + url)
        return response


def osv_matches(keys, transport):
    """OSV performs ecosystem-specific affected range/backport matching."""
    keys = list(sorted(keys))
    require(0 < len(keys) < 5000,"OSV inventory size")
    result = {}
    for first in range(0,len(keys),100):
        batch = keys[first:first+100]
        queries = [{"package":{"ecosystem":eco,"name":name},"version":ver} for eco,name,ver in batch]
        pending = list(zip(batch,queries))
        for _ in range(20):
            raw = transport.fetch("https://api.osv.dev/v1/querybatch", {"queries":[q for k,q in pending]})
            rows = raw["results"]
            require(isinstance(rows,list) and len(rows)==len(pending),"partial OSV batch")
            next_page = []
            for (key,query), row in zip(pending,rows):
                require(isinstance(row,dict) and set(row)<= {"vulns","next_page_token"}, "invalid OSV result")
                hits = result.setdefault(key,{})
                for item in row.get("vulns",[]):
                    identifier, modified = item["id"], item["modified"]
                    require(re.fullmatch("[a-zA-Z0-9_.:-]{1,180}",identifier) and isinstance(modified,str),
                            "invalid OSV finding identity")
                    hits[identifier] = modified
                if row.get("next_page_token"):
                    require(row["next_page_token"]!=query.get("page_token"),"repeated OSV page")
                    next_page.append((key,dict(query,page_token=row["next_page_token"])))
            if not next_page:
                break
            pending = next_page
        else:
            raise ValueError("OSV page limit")
    return result


def osv_records(matches, transport, errors=None):
    revisions = {}
    for found in matches.values():
        for name, modified in found.items():
            require(revisions.setdefault(name,modified)==modified, "OSV changed during scan "+name)
    def fetch(pair):
        name, modified = pair
        try:
            result=transport.fetch("https://api.osv.dev/v1/vulns/"+name)
            # A batch timestamp has microsecond precision. An Ubuntu source
            # advisory can appear in the OSV index after withdrawal.
            require(result["id"]==name and datetime.fromisoformat(result["modified"])==datetime.fromisoformat(modified),
                    "OSV record no longer corresponds to batch "+name)
            if result.get("withdrawn"):
                require(0<datetime.fromisoformat(result["withdrawn"]).timestamp()<=time.time(),"future OSV withdrawal")
        except (OSError,ValueError) as exc:
            if errors is None:
                raise
            errors[name]=str(exc)
            return name,None
        return name,result
    with ThreadPoolExecutor(max_workers=6) as pool:
        return {k:v for k,v in pool.map(fetch,sorted(revisions.items())) if v is not None}


def official_sources(packages, transport):
    def published(pair):
        name, version = pair
        params={"ws.op":"getPublishedSources","exact_match":"true","source_name":name,"version":version,
                "distro_series":"https://api.launchpad.net/1.0/ubuntu/noble"}
        try:
            result=transport.fetch("https://api.launchpad.net/1.0/ubuntu/+archive/primary?"+urlencode(params))
        except OSError:
            return pair,None
        require(isinstance(result["entries"],list) and type(result["total_size"]) is int,"invalid Launchpad result")
        known=any(row["source_package_name"]==name and row["source_package_version"]==version
                  and row["distro_series_link"]==params["distro_series"] and row.get("date_published")
                  for row in result["entries"])
        if not known and result.get("next_collection_link"):
            raise ValueError("unresolved Launchpad pagination for "+name)
        return pair,known
    with ThreadPoolExecutor(max_workers=5) as pool:
        return dict(pool.map(published,sorted(packages)))


def level(record, cvss):
    """Take the most severe published classification, preserving every vector."""
    ranks = ["UNKNOWN","INFO","LOW","MODERATE","HIGH","CRITICAL"]
    values=[]
    def add(value):
        v = {"MEDIUM":"MODERATE","NEGLIGIBLE":"INFO","NONE":"INFO"}.get(value.upper(),value.upper())
        require(v in ranks,"unrecognized severity "+value)
        values.append(v)
    raw=record.get("database_specific",{}).get("severity")
    if raw:
        add(raw)
    for score in record.get("severity",[]):
        typ, value=score["type"],score["score"]
        if typ=="Ubuntu":
            add(value)
        elif typ in {"CVSS_V2","CVSS_V3","CVSS_V4"}:
            parser={"CVSS_V2":cvss.CVSS2,"CVSS_V3":cvss.CVSS3,"CVSS_V4":cvss.CVSS4}[typ]
            add(parser(value).severities()[0])
        else:
            raise ValueError("unsupported severity format "+typ)
    return max(values,key=ranks.index) if values else "UNKNOWN"
