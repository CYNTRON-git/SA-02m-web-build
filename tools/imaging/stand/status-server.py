#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SA-02m stand status UI + JSON API on :8765.

States: IDLE | FEL_SEEN | NETBOOT | FLASHING | REBOOT_WAIT | DONE | FAIL

Usage:
  python3 status-server.py [--port 8765] [--state-file ../stand-data/status.json]
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_STATE = {
    "state": "IDLE",
    "progress": 0,
    "message": "",
    "board_ip": "",
    "session_id": "",
    "updated": 0.0,
    "history": [],
}

_lock = threading.Lock()
_state: dict = dict(DEFAULT_STATE)
_state_path: Path | None = None


def _now() -> float:
    return time.time()


def load_state() -> None:
    global _state
    if _state_path and _state_path.is_file():
        try:
            _state = json.loads(_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _state = dict(DEFAULT_STATE)


def save_state() -> None:
    if not _state_path:
        return
    _state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state, indent=2), encoding="utf-8")
    tmp.replace(_state_path)


def update_state(**kwargs) -> dict:
    with _lock:
        for k, v in kwargs.items():
            if v is None:
                continue
            if k == "progress":
                try:
                    _state["progress"] = max(0, min(100, int(v)))
                except (TypeError, ValueError):
                    pass
            elif k in _state or k in ("state", "message", "board_ip", "session_id"):
                _state[k] = v
        _state["updated"] = _now()
        hist = _state.setdefault("history", [])
        hist.append(
            {
                "t": _state["updated"],
                "state": _state.get("state"),
                "progress": _state.get("progress"),
                "message": _state.get("message"),
            }
        )
        _state["history"] = hist[-40:]
        save_state()
        return dict(_state)


HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>SA-02m flash stand</title>
<style>
  :root { --bg:#0f1419; --fg:#e7ecf1; --muted:#8b9aab; --ok:#3dd68c; --bad:#ff6b6b;
          --warn:#ffc857; --card:#1a2332; --accent:#4da3ff; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: "Segoe UI", system-ui, sans-serif; background:var(--bg);
         color:var(--fg); min-height:100vh; display:flex; flex-direction:column; }
  header { padding:1.25rem 1.5rem; border-bottom:1px solid #2a3545; }
  header h1 { margin:0; font-size:1.25rem; font-weight:600; letter-spacing:.02em; }
  header p { margin:.35rem 0 0; color:var(--muted); font-size:.9rem; }
  main { flex:1; display:flex; align-items:center; justify-content:center; padding:2rem; }
  .panel { background:var(--card); border-radius:12px; padding:2.5rem 3rem; min-width:min(520px,100%);
           box-shadow:0 8px 32px rgba(0,0,0,.35); text-align:center; }
  .state { font-size:2.5rem; font-weight:700; letter-spacing:.04em; margin:.5rem 0; }
  .state.IDLE,.state.NETBOOT { color:var(--accent); }
  .state.FEL_SEEN,.state.FLASHING,.state.REBOOT_WAIT { color:var(--warn); }
  .state.DONE { color:var(--ok); }
  .state.FAIL { color:var(--bad); }
  .bar { height:10px; background:#2a3545; border-radius:6px; overflow:hidden; margin:1.25rem 0; }
  .bar > i { display:block; height:100%; width:0; background:linear-gradient(90deg,#4da3ff,#3dd68c);
             transition:width .4s ease; }
  .msg { color:var(--muted); min-height:1.4em; font-size:1rem; }
  .meta { margin-top:1.5rem; font-size:.85rem; color:var(--muted); font-family:ui-monospace,monospace; }
  footer { padding:1rem 1.5rem; color:var(--muted); font-size:.8rem; border-top:1px solid #2a3545; }
</style>
</head>
<body>
<header>
  <h1>SA-02m · flash stand</h1>
  <p>FEL → netinstall → eMMC · zero-touch</p>
</header>
<main>
  <div class="panel">
    <div class="state IDLE" id="st">IDLE</div>
    <div class="bar"><i id="bar"></i></div>
    <div class="msg" id="msg">Waiting for board in FEL…</div>
    <div class="meta" id="meta"></div>
  </div>
</main>
<footer>API: <code>/api/status</code> · poll 1s</footer>
<script>
async function tick(){
  try {
    const r = await fetch('/api/status');
    const j = await r.json();
    const st = document.getElementById('st');
    st.textContent = j.state || 'IDLE';
    st.className = 'state ' + (j.state || 'IDLE');
    document.getElementById('bar').style.width = (j.progress||0) + '%';
    document.getElementById('msg').textContent = j.message || '';
    const ip = j.board_ip ? ('board ' + j.board_ip + ' · ') : '';
    const t = j.updated ? new Date(j.updated*1000).toLocaleTimeString() : '';
    document.getElementById('meta').textContent = ip + (j.session_id||'') + (t?(' · '+t):'');
    document.body.style.background = j.state==='DONE' ? '#0d1f16' : (j.state==='FAIL' ? '#1f0d0d' : '');
  } catch(e) {}
}
tick(); setInterval(tick, 1000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _parse_update(self) -> dict:
        params: dict[str, str] = {}
        parsed = urllib.parse.urlparse(self.path)
        params.update({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            raw = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            if "application/json" in ctype:
                try:
                    params.update(json.loads(raw.decode("utf-8")))
                except json.JSONDecodeError:
                    pass
            else:
                params.update(
                    {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()}
                )
        return params

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            with _lock:
                body = json.dumps(_state).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path == "/api/health":
            self._send(200, b'{"ok":true}', "application/json")
            return
        # Allow GET updates from busybox wget without POST
        if path.startswith("/api/status") or path == "/api/update":
            p = self._parse_update()
            if p.get("state") or p.get("progress") is not None:
                st = update_state(
                    state=p.get("state"),
                    progress=p.get("progress"),
                    message=p.get("message"),
                    board_ip=p.get("board_ip"),
                    session_id=p.get("session_id"),
                )
                self._send(200, json.dumps(st).encode("utf-8"), "application/json")
                return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/api/status", "/api/update"):
            p = self._parse_update()
            st = update_state(
                state=p.get("state"),
                progress=p.get("progress"),
                message=p.get("message"),
                board_ip=p.get("board_ip"),
                session_id=p.get("session_id"),
            )
            self._send(200, json.dumps(st).encode("utf-8"), "application/json")
            return
        if path == "/api/reset":
            with _lock:
                global _state
                _state = dict(DEFAULT_STATE)
                _state["updated"] = _now()
                save_state()
                body = json.dumps(_state).encode("utf-8")
            self._send(200, body, "application/json")
            return
        self._send(404, b"not found", "text/plain")


def main() -> int:
    global _state_path
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--state-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "stand-data" / "status.json",
    )
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()
    _state_path = args.state_file
    load_state()
    if _state.get("state") in ("DONE", "FAIL"):
        # Fresh shift UI starts idle unless mid-flash
        pass
    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"status-server http://127.0.0.1:{args.port}/  state={_state_path}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stop", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
