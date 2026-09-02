"""Content-Disposition header-injection guard for the devices export (audit L1).

`_send_bytes` builds the download filename from request-supplied metric_id/group
(device_history_db.export_*), which `_q1` only edge-strips. Before 1.0.6.24 the
filename was `filename.encode("ascii","ignore")` — which PRESERVES CR, LF and the
double quote — and was embedded raw in `Content-Disposition`, so a `%0d%0a` in the
value split the header and a `"` broke out of the quoted filename. This pins that a
CR/LF/quote-bearing filename can neither inject a header nor break the quoting.

Proven RED (1.0.6.24): against the pre-fix `_send_bytes` (no sanitisation), the
CR/LF assertion fails — the captured Content-Disposition carries the injected
`\r\nSet-Cookie: ...` verbatim.
"""

from __future__ import annotations

from sa02m_devices import api


class _CapturingHandler:
    """Records send_header calls the way BaseHTTPRequestHandler would receive them."""

    def __init__(self) -> None:
        self.headers: list[tuple[str, str]] = []
        self.status: int | None = None
        self.body: bytes = b""

    def send_response(self, code: int) -> None:
        self.status = code

    def send_header(self, key: str, value: str) -> None:
        self.headers.append((key, value))

    def end_headers(self) -> None:
        pass

    class _Wfile:
        def __init__(self, outer: "_CapturingHandler") -> None:
            self._outer = outer

        def write(self, data: bytes) -> None:
            self._outer.body += data

    @property
    def wfile(self):  # noqa: ANN202
        return _CapturingHandler._Wfile(self)


def _disposition(h: _CapturingHandler) -> str:
    for k, v in h.headers:
        if k == "Content-Disposition":
            return v
    return ""


def test_export_filename_crlf_cannot_inject_a_header():
    h = _CapturingHandler()
    evil = "data\r\nSet-Cookie: pwn=1\r\nX-Injected: yes"
    api._send_bytes(h, b"x", content_type="application/octet-stream", filename=evil)
    disp = _disposition(h)
    # The security property is that no CR/LF survives to split the header — once
    # they are gone, the leftover text is an inert (if ugly) filename, not an
    # injected field. send_header on the http.server side does not validate.
    assert "\r" not in disp and "\n" not in disp, f"CR/LF survived into the header: {disp!r}"


def test_export_filename_quote_cannot_break_out():
    h = _CapturingHandler()
    evil = 'a"; x="1'
    api._send_bytes(h, b"x", content_type="application/octet-stream", filename=evil)
    disp = _disposition(h)
    # The quoted fil="..." part must not carry a bare double quote that closes it.
    quoted = disp.split('filename="', 1)[1].split('"', 1)[0] if 'filename="' in disp else ""
    assert '"' not in quoted, f"a quote broke out of the quoted filename: {disp!r}"


def test_export_filename_control_chars_stripped_but_name_kept():
    h = _CapturingHandler()
    api._send_bytes(h, b"x", content_type="application/octet-stream", filename="mr_export_7\x00\t.xlsx")
    disp = _disposition(h)
    assert "mr_export_7" in disp and ".xlsx" in disp, f"the legitimate name was lost: {disp!r}"
    assert "\x00" not in disp and "\t" not in disp, f"a control char survived: {disp!r}"


def test_export_all_ascii_stripped_falls_back_to_default():
    h = _CapturingHandler()
    # A filename that is entirely non-ascii + control leaves an empty ascii_name.
    api._send_bytes(h, b"x", content_type="application/octet-stream", filename="\r\n\r\n")
    disp = _disposition(h)
    assert 'filename="export.bin"' in disp, f"empty ascii name did not fall back: {disp!r}"
