# -*- coding: utf-8 -*-
"""POST /firmware/upload multipart parsing must not depend on the `cgi` module.

WHY THIS EXISTS
`cgi` is deprecated since Python 3.11 and REMOVED in 3.13 (PEP 594). The board runs
3.12 today, so a distro upgrade would have broken firmware upload at import time —
the daemon would not even start, since service.py imported `cgi` at module level
(audit 2026-09-24 L9). The parsing now lives in `sa02m_flasher.multipart_upload`
(stdlib `email.parser`, present on every version).

WHAT IS RUN
1. The SHIPPED wiring: `_extract_multipart` is lifted out of service.py with `ast`
   (service.py itself cannot be imported off-Linux — `import grp`; see
   test_health_lease.py for the idiom) and every module-level import it needs is
   executed with `cgi` BLOCKED in sys.modules — the 3.13 condition, on any Python.
   RED on the pre-fix tree: resolving `cgi` raises ImportError.
2. The semantics service.py had with cgi.FieldStorage, pinned case by case: field
   name `file`, the filename taken verbatim (UTF-8, quoted-pair unescaping), the bytes
   exact (CR/LF/NUL/boundary-like runs inside the data, the final CRLF owned by the
   delimiter), exactly Content-Length bytes read, and the four error messages.
3. Where `cgi` still exists (Python <= 3.12 — CI and the board today), the OLD
   implementation is run as an oracle on the same bodies and must agree byte for byte.
"""
from __future__ import annotations

import __future__
import ast
import io
import sys
import types
import unittest
import warnings
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "sa02m_flasher"
_SERVICE_SRC = (_PKG / "service.py").read_text(encoding="utf-8")


class _BlockCgi:
    """`import cgi` raises ImportError inside the block — Python 3.13's condition."""

    def __enter__(self):
        self._saved = sys.modules.get("cgi", _BlockCgi)
        sys.modules["cgi"] = None  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        if self._saved is _BlockCgi:
            sys.modules.pop("cgi", None)
        else:
            sys.modules["cgi"] = self._saved
        return False


def _shipped_extract_multipart():
    """service.py's `_extract_multipart`, with the module-level imports it uses."""
    tree = ast.parse(_SERVICE_SRC)
    fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_extract_multipart"),
        None,
    )
    if fn is None:
        raise AssertionError("service.py no longer defines _extract_multipart() — POST /firmware/upload lost its parser")
    used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    ns: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                bound = (a.asname or a.name).split(".")[0]
                if bound in used:
                    exec(f"import {a.name}" + (f" as {a.asname}" if a.asname else ""), ns)  # noqa: S102
        elif isinstance(node, ast.ImportFrom):
            mod = ("sa02m_flasher" + ("." + node.module if node.module else "")) if node.level else node.module
            for a in node.names:
                bound = a.asname or a.name
                if bound in used:
                    exec(f"from {mod} import {a.name}" + (f" as {a.asname}" if a.asname else ""), ns)  # noqa: S102
    src = "\n".join(_SERVICE_SRC.splitlines()[fn.lineno - 1 : fn.end_lineno])
    code = compile(src, str(_PKG / "service.py"), "exec", flags=__future__.annotations.compiler_flag, dont_inherit=True)
    exec(code, ns)  # noqa: S102
    return ns["_extract_multipart"]


BOUNDARY = "----WebKitFormBoundaryX3kq9Zr2"
CTYPE = "multipart/form-data; boundary=" + BOUNDARY

# Bytes a firmware image really contains, and the ones a line-based parser trips on.
NASTY = (
    b"\x00\x01\x02\xff\r\n\r\n\n\r\r\n--" + BOUNDARY.encode() + b"x\r\n"
    + b"--" + BOUNDARY[:-1].encode() + b"\r\n" + bytes(range(256)) + b"\r\n\r"
)


def _body(parts, boundary=BOUNDARY, nl=b"\r\n"):
    """parts: [(headers_bytes, data_bytes)] -> a multipart body."""
    out = b""
    for hdr, data in parts:
        out += b"--" + boundary.encode() + nl + hdr + nl + nl + data + nl
    return out + b"--" + boundary.encode() + b"--" + nl


def _file_part(filename_bytes: bytes, data: bytes, name: bytes = b"file") -> tuple:
    return (
        b'Content-Disposition: form-data; name="' + name + b'"; filename="' + filename_bytes + b'"\r\n'
        + b"Content-Type: application/octet-stream",
        data,
    )


def _handler(body: bytes, ctype=CTYPE, length=None, trailing: bytes = b""):
    headers = {"Content-Type": ctype}
    if length is not False:
        headers["Content-Length"] = str(len(body) if length is None else length)
    return types.SimpleNamespace(headers=headers, rfile=io.BytesIO(body + trailing))


class TestNoCgiDependency(unittest.TestCase):
    def test_shipped_upload_parse_runs_with_cgi_removed(self) -> None:
        with _BlockCgi():
            try:
                extract = _shipped_extract_multipart()
            except ImportError as e:
                self.fail(f"service.py's upload parser needs `cgi`, which Python 3.13 removed: {e}")
            name, data = extract(_handler(_body([_file_part(b"MR-02m_1.0.0.1.fw", NASTY)])))
        self.assertEqual(name, "MR-02m_1.0.0.1.fw")
        self.assertEqual(data, NASTY)

    def test_service_py_does_not_import_cgi(self) -> None:
        tree = ast.parse(_SERVICE_SRC)
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertNotIn("cgi", imported, "service.py imports `cgi` — removed in Python 3.13")


class TestUploadSemantics(unittest.TestCase):
    """The behaviour service.py had with cgi.FieldStorage, via the new module."""

    def setUp(self) -> None:
        with _BlockCgi():
            from sa02m_flasher.multipart_upload import read_file_field
        self.read = read_file_field

    def _ok(self, body, **kw):
        return self.read(_handler(body, **kw).headers, _handler(body, **kw).rfile)

    def test_binary_bytes_exact(self) -> None:
        for data in (NASTY, b"", b"\r\n", b"\n", b"\r", b"x" * 70000 + b"\r\n" + b"y" * 3):
            with self.subTest(n=len(data)):
                name, got = self.read(*_hs(_body([_file_part(b"a.fw", data)])))
                self.assertEqual(got, data)
                self.assertEqual(name, "a.fw")

    def test_other_fields_before_and_after(self) -> None:
        body = _body([
            (b'Content-Disposition: form-data; name="comment"', b"hello"),
            _file_part(b"b.bin", b"DATA"),
            (b'Content-Disposition: form-data; name="x"', b"1"),
        ])
        self.assertEqual(self.read(*_hs(body)), ("b.bin", b"DATA"))

    def test_utf8_filename_verbatim(self) -> None:
        name, _ = self.read(*_hs(_body([_file_part("прошивка МР-02м.fw".encode("utf-8"), b"D")])))
        self.assertEqual(name, "прошивка МР-02м.fw")

    def test_quoted_pair_and_semicolon_in_filename(self) -> None:
        name, _ = self.read(*_hs(_body([_file_part(b'a\\"b;c.fw', b"D")])))
        self.assertEqual(name, 'a"b;c.fw')

    def test_windows_path_filename_kept_as_sent(self) -> None:
        # repo.add_upload() sanitises the name; the parser must not.
        name, _ = self.read(*_hs(_body([_file_part(b"C:\\fw\\a.fw", b"D")])))
        self.assertEqual(name, "C:\\fw\\a.fw")

    def test_reads_exactly_content_length(self) -> None:
        body = _body([_file_part(b"a.fw", b"DATA")])
        h = _handler(body, trailing=b"NEXT-REQUEST-BYTES")
        self.assertEqual(self.read(h.headers, h.rfile), ("a.fw", b"DATA"))
        self.assertEqual(h.rfile.read(), b"NEXT-REQUEST-BYTES")

    def test_lf_only_body(self) -> None:
        body = _body([(b'Content-Disposition: form-data; name="file"; filename="a.fw"', b"D\r\nE")], nl=b"\n")
        self.assertEqual(self.read(*_hs(body)), ("a.fw", b"D\r\nE"))

    def test_errors(self) -> None:
        cases = [
            ("not multipart", _handler(b"{}", ctype="application/json"), "Ожидается multipart/form-data"),
            ("no content-length", _handler(b"x", length=False), "Пустое тело запроса"),
            ("zero content-length", _handler(b"x", length=0), "Пустое тело запроса"),
            ("no file field", _handler(_body([(b'Content-Disposition: form-data; name="other"; filename="a.fw"', b"D")])),
             "Поле 'file' не найдено"),
            ("file field without filename", _handler(_body([(b'Content-Disposition: form-data; name="file"', b"D")])),
             "Отсутствует имя файла"),
            ("empty filename", _handler(_body([_file_part(b"", b"D")])), "Отсутствует имя файла"),
            ("no boundary", _handler(b"--x\r\n\r\n", ctype="multipart/form-data"), "boundary"),
        ]
        for label, h, msg in cases:
            with self.subTest(label):
                with self.assertRaises(ValueError) as cm:
                    self.read(h.headers, h.rfile)
                self.assertIn(msg, str(cm.exception))

    def test_bad_content_length_is_valueerror(self) -> None:
        h = _handler(b"x", length="abc")
        with self.assertRaises(ValueError):
            self.read(h.headers, h.rfile)

    def test_duplicate_file_field_refused(self) -> None:
        body = _body([_file_part(b"a.fw", b"A"), _file_part(b"b.fw", b"B")])
        with self.assertRaises(ValueError) as cm:
            self.read(*_hs(body))
        self.assertIn("несколько", str(cm.exception))


def _hs(body):
    h = _handler(body)
    return h.headers, h.rfile


def _cgi_available() -> bool:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import cgi  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_cgi_available(), "cgi removed on this Python (>= 3.13) — the oracle cannot run; CI/board Python 3.12 runs it")
class TestAgreesWithCgiOracle(unittest.TestCase):
    """The pre-1.0.6.58 parser, verbatim, as the oracle."""

    @staticmethod
    def _old(headers, rfile):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import cgi
        ctype = headers.get("Content-Type") or ""
        if not ctype.startswith("multipart/"):
            raise ValueError("Ожидается multipart/form-data")
        length = int(headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("Пустое тело запроса")
        fs = cgi.FieldStorage(
            fp=rfile,
            headers=headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype, "CONTENT_LENGTH": str(length)},
            keep_blank_values=True,
        )
        if "file" not in fs:
            raise ValueError("Поле 'file' не найдено")
        item = fs["file"]
        if not item.filename:
            raise ValueError("Отсутствует имя файла")
        data = item.file.read() if hasattr(item, "file") else item.value
        return item.filename, (data if isinstance(data, (bytes, bytearray)) else bytes(data or b""))

    def test_same_result_as_cgi(self) -> None:
        from email.message import Message

        from sa02m_flasher.multipart_upload import read_file_field

        bodies = {
            "nasty bytes": _body([_file_part(b"a.fw", NASTY)]),
            "empty data": _body([_file_part(b"a.fw", b"")]),
            "trailing CR": _body([_file_part(b"a.fw", b"abc\r")]),
            "big": _body([_file_part(b"big.bin", bytes(range(256)) * 400)]),
            "utf8 name": _body([_file_part("имя.fw".encode("utf-8"), b"D")]),
            "quoted pair": _body([_file_part(b'a\\"b;c.fw', b"D")]),
            "fields around": _body([
                (b'Content-Disposition: form-data; name="c"', b"v"),
                _file_part(b"b.bin", b"DATA"),
            ]),
            "lf only": _body([(b'Content-Disposition: form-data; name="file"; filename="a.fw"', b"D\r\nE")], nl=b"\n"),
            "no filename": _body([(b'Content-Disposition: form-data; name="file"', b"D")]),
            "empty filename": _body([_file_part(b"", b"D")]),
            # cgi never applied a Content-Transfer-Encoding: the bytes stay as sent.
            "cte ignored": _body([(
                b'Content-Disposition: form-data; name="file"; filename="a.fw"\r\n'
                b"Content-Transfer-Encoding: base64",
                b"QUJD",
            )]),
            "unquoted params": _body([(b"Content-Disposition: form-data; name=file; filename=a.fw", b"D")]),
            "no file field": _body([(b'Content-Disposition: form-data; name="x"; filename="a"', b"D")]),
        }
        for label, body in bodies.items():
            with self.subTest(label):
                msg = Message()  # cgi reads Content-Type from a Message-like mapping
                msg["Content-Type"] = CTYPE
                msg["Content-Length"] = str(len(body))
                try:
                    want = ("ok", self._old(msg, io.BytesIO(body)))
                except ValueError as e:
                    want = ("err", str(e))
                try:
                    got = ("ok", read_file_field({"Content-Type": CTYPE, "Content-Length": str(len(body))}, io.BytesIO(body)))
                except ValueError as e:
                    got = ("err", str(e))
                self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
