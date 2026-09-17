"""Restricted Python for type=code scenarios. No import, no FS."""
from __future__ import annotations

import ast
import math
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

from sa02m_rules.engine import sun_times
from sa02m_rules.store import enqueue_notify

_BANNED = {
    "import", "Import", "ImportFrom", "Exec", "Eval", "ClassDef",
    "Global", "Nonlocal", "Lambda", "AsyncFunctionDef", "Await", "Yield",
}
_BANNED_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "globals", "locals",
    "getattr", "setattr", "delattr", "vars", "dir", "input", "memoryview",
    "breakpoint", "exit", "quit", "help", "copyright", "credits", "license",
}


class Hub:
    def __init__(self, state: Dict[str, Dict[str, Any]], pub: Callable, writes: list):
        self._state = state
        self._pub = pub
        self._writes = writes

    def get(self, device: str, cap: str = "on_off") -> Any:
        return (self._state.get(str(device)) or {}).get(str(cap))

    def set(self, device: str, cap: str, value: Any) -> None:
        if self._writes[0] >= 8:
            raise RuntimeError("write cap")
        self._pub(str(device), str(cap), value)
        self._writes[0] += 1

    def subscribe(self, device: str, cap: str = "on_off") -> Any:
        return self.get(device, cap)


class Cron:
    def __init__(self, now: float, lat: float, lon: float):
        self.now = now
        self.lat = lat
        self.lon = lon

    def schedule(self, at: str) -> bool:
        lt = time.localtime(self.now)
        return ("%02d:%02d" % (lt.tm_hour, lt.tm_min)) == str(at)

    def sunrise(self, offset: int = 0) -> float:
        rise, _ = sun_times(self.now, self.lat, self.lon)
        return rise + offset * 60.0

    def sunset(self, offset: int = 0) -> float:
        _, sett = sun_times(self.now, self.lat, self.lon)
        return sett + offset * 60.0


class Notify:
    def __init__(self, doc: Dict[str, Any], path: str):
        self._doc = doc
        self._path = path

    def text(self, message: str) -> None:
        enqueue_notify(self._doc, str(message)[:240], self._path)


class Http:
    def get(self, url: str) -> str:
        return self._do("GET", url, None)

    def post(self, url: str, body: str = "") -> str:
        return self._do("POST", url, body)

    def _do(self, method: str, url: str, body: Optional[str]) -> str:
        if not (str(url).startswith("http://") or str(url).startswith("https://")):
            raise RuntimeError("bad url")
        req = urllib.request.Request(str(url)[:500], method=method)
        if method == "POST" and body is not None:
            req.data = str(body)[:2000].encode("utf-8")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return "http %s" % resp.status


class Vars:
    def __init__(self, bucket: Dict[str, Any]):
        self._b = bucket

    def get(self, key: str, default: Any = None) -> Any:
        return self._b.get(str(key), default)

    def set(self, key: str, value: Any) -> None:
        if len(self._b) >= 32 and str(key) not in self._b:
            return
        self._b[str(key)[:32]] = value


def _walk_ok(tree: ast.AST) -> str:
    for node in ast.walk(tree):
        name = type(node).__name__
        if name in _BANNED or name.startswith("Async"):
            return "banned %s" % name
        if isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            return "banned %s" % node.id
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return "banned attr"
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            return "banned name"
    return ""


def run_code(s: Dict[str, Any], library: str, state: Dict[str, Dict[str, Any]],
             pub: Callable, now: float, lat: float, lon: float,
             doc: Dict[str, Any], path: str, vars_bucket: Dict[str, Any]) -> Dict[str, Any]:
    src = ((library or "") + "\n" + (s.get("code") or "")).strip()
    err = ""
    if not src:
        err = "empty"
    else:
        try:
            tree = ast.parse(src, mode="exec")
        except SyntaxError as exc:
            err = "syntax %s" % exc
            tree = None
        if tree is not None:
            err = _walk_ok(tree)
        if not err and tree is not None:
            writes = [0]
            env = {
                "Hub": Hub(state, pub, writes),
                "Cron": Cron(now, lat, lon),
                "Notify": Notify(doc, path),
                "Http": Http(),
                "Vars": Vars(vars_bucket),
                "math": math,
                "True": True, "False": False, "None": None,
                "abs": abs, "min": min, "max": max, "int": int, "float": float,
                "str": str, "bool": bool, "len": len, "range": range,
            }
            try:
                compiled = compile(tree, "<scenario>", "exec")
                exec(compiled, {"__builtins__": {}}, env)  # noqa: S102 — sandbox
            except Exception as exc:
                err = str(exc)[:200]
    rec = {"ts": now, "id": s.get("id"), "name": s.get("name"), "error": err, "ok": not err}
    s["last_run"] = now
    s["last_error"] = err
    return rec
