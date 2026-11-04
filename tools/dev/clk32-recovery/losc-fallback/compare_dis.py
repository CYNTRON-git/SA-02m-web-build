"""Compare the reconstructed sa02m-losc-fallback.py with the recovered bytecode.

Run with a CPython 3.12 in isolated mode, from anywhere:

    py -3.12 -I tools/dev/clk32-recovery/losc-fallback/compare_dis.py [SOURCE]

SOURCE defaults to sa02m-losc-fallback.py next to this script; pass another
path to check an edited copy. The recovered .pyc is untrusted data: it is only
marshal-loaded, never imported or executed. The reconstruction is only
compiled, never executed. Exit status 0 = every code object equivalent with
the same line numbers, 1 = a difference, 2 = wrong interpreter.

Checked per code object, recursively: bytecode, constants, names, variables,
cell/free vars, argument counts, flags, stack size, exception table and line
numbers (all must match), then the remaining instruction positions - end line
and columns (reported, not gated). Finally the marshal payload of the rebuild
is compared byte for byte with the recovered file's (reported).

Some 3.12 patch releases place the end-line and column positions of for-loop,
comprehension and with-statement instructions differently (a compiler change,
not a source difference): under them the run is still EQUIVALENT, reporting
"end-line/column positions differ" and a payload that is not byte-identical.
Which releases were measured, and what is known of the board's: the "source
rebuilt" section of .ai-dev/notes/clk32-crystal-recovery-2026-10-22.md.
To get another release:
`uv python install 3.12.3` (UV_PYTHON_INSTALL_DIR to keep it out of the way).
"""
import dis
import importlib.util
import io
import marshal
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PYC = os.path.join(HERE, "sa02m-losc-fallback.cpython-312.pyc.bin")
SRC = os.path.join(HERE, "sa02m-losc-fallback.py")
ORIGINAL_PATH = "/tmp/sa02m-losc-fallback.py"  # co_filename of the recovered file

SEMANTIC = ("co_code", "co_names", "co_varnames", "co_cellvars", "co_freevars",
            "co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_nlocals",
            "co_flags", "co_stacksize", "co_exceptiontable", "co_name", "co_qualname",
            "co_filename")


def walk(c):
    yield c
    for k in c.co_consts:
        if isinstance(k, types.CodeType):
            yield from walk(k)


def const_key(k):
    if isinstance(k, types.CodeType):
        return ("code", k.co_qualname)
    if isinstance(k, tuple):
        return ("tuple", tuple(const_key(x) for x in k))
    if isinstance(k, frozenset):
        return ("frozenset", tuple(sorted(repr(const_key(x)) for x in k)))
    return (type(k).__name__, repr(k))


def compare(a, b, out):
    """a = recovered, b = rebuilt; appends (qualname, kind, detail) rows."""
    name = a.co_qualname
    for attr in SEMANTIC:
        va, vb = getattr(a, attr), getattr(b, attr)
        if va == vb:
            continue
        if attr == "co_code":
            ia, ib = list(dis.get_instructions(a)), list(dis.get_instructions(b))
            i = next((n for n, (x, y) in enumerate(zip(ia, ib))
                      if (x.opname, x.argrepr) != (y.opname, y.argrepr)), min(len(ia), len(ib)))
            show = lambda seq: ("%s %s" % (seq[i].opname, seq[i].argrepr)
                                if i < len(seq) else "<end>")
            out.append((name, "DIFF", "co_code: first difference at instruction %d: "
                        "recovered %s, rebuilt %s" % (i, show(ia), show(ib))))
        else:
            out.append((name, "DIFF", "%s: recovered=%r rebuilt=%r" % (attr, va, vb)))
    ka = [const_key(k) for k in a.co_consts]
    kb = [const_key(k) for k in b.co_consts]
    if ka != kb:
        idx = [n for n in range(max(len(ka), len(kb)))
               if (ka[n] if n < len(ka) else None) != (kb[n] if n < len(kb) else None)]
        out.append((name, "DIFF", "co_consts differ at %s, e.g. [%d] recovered=%.80r rebuilt=%.80r"
                    % (idx, idx[0], a.co_consts[idx[0]] if idx[0] < len(ka) else None,
                       b.co_consts[idx[0]] if idx[0] < len(kb) else None)))
    la, lb = list(a.co_lines()), list(b.co_lines())
    if a.co_firstlineno != b.co_firstlineno or la != lb:
        first = next(((x, y) for x, y in zip(la, lb) if x != y), (None, None))
        out.append((name, "LINE", "line numbers differ: first line %d vs %d, first "
                    "(start, end, line) entry recovered %s rebuilt %s"
                    % (a.co_firstlineno, b.co_firstlineno, first[0], first[1])))
    pa, pb = list(a.co_positions()), list(b.co_positions())
    if pa != pb:
        bad = sum(1 for x, y in zip(pa, pb) if x != y) + abs(len(pa) - len(pb))
        out.append((name, "POS", "%d instruction positions differ" % bad))
    sub_a = [k for k in a.co_consts if isinstance(k, types.CodeType)]
    sub_b = [k for k in b.co_consts if isinstance(k, types.CodeType)]
    if len(sub_a) != len(sub_b):
        out.append((name, "DIFF", "nested code objects: %d vs %d" % (len(sub_a), len(sub_b))))
    for x, y in zip(sub_a, sub_b):
        compare(x, y, out)


def dis_text(code, with_lines):
    """dis of every code object, recursively, with or without line numbers."""
    buf = io.StringIO()
    for c in walk(code):
        buf.write("== %s\n" % c.co_qualname)
        for ins in dis.get_instructions(c):
            line = ins.positions.lineno if with_lines else "-"
            # A code-object argrepr carries a memory address; name it instead.
            arg = ins.argval.co_qualname if isinstance(ins.argval, types.CodeType) else ins.argrepr
            buf.write("%s %d %s %r\n" % (line, ins.offset, ins.opname, arg))
        buf.write("exc %s\n" % c.co_exceptiontable.hex())
    return buf.getvalue()


def main():
    if sys.version_info[:2] != (3, 12):
        print("needs CPython 3.12 (the bytecode format is version-bound); this is %s"
              % sys.version.split()[0])
        return 2
    with open(PYC, "rb") as f:
        raw = f.read()
    header, payload = raw[:16], raw[16:]
    if header[:4] != importlib.util.MAGIC_NUMBER:
        print("magic number %s does not match this interpreter's %s"
              % (header[:4].hex(), importlib.util.MAGIC_NUMBER.hex()))
        return 2
    with open(sys.argv[1] if len(sys.argv) > 1 else SRC, "rb") as f:
        source = f.read()
    # marshal's back-reference flag bits follow reference counts, so dump the
    # way importlib does - the fresh module code held by one local name, before
    # any walk below adds references to the nested code objects.
    fresh = compile(source, ORIGINAL_PATH, "exec", dont_inherit=True)
    same_payload = marshal.dumps(fresh) == payload
    del fresh
    recovered = marshal.loads(payload)
    rebuilt = compile(source, ORIGINAL_PATH, "exec", dont_inherit=True)

    out = []
    compare(recovered, rebuilt, out)
    by_name = {}
    for name, kind, detail in out:
        by_name.setdefault(name, []).append((kind, detail))
    semantic_ok = True
    print("interpreter %s" % sys.version.split()[0])
    for c in walk(recovered):
        rows = by_name.get(c.co_qualname, [])
        kinds = {k for k, _ in rows}
        if "DIFF" in kinds:
            verdict = "DIFFERENT"
            semantic_ok = False
        elif "LINE" in kinds:
            verdict = "DIFFERENT (line numbers only)"
            semantic_ok = False
        elif kinds:
            verdict = "equivalent (end-line/column positions differ)"
        else:
            verdict = "identical"
        print("  %-46s %s" % (c.co_qualname, verdict))
        for kind, detail in rows:
            if kind in ("DIFF", "LINE"):
                print("      " + detail)
    same_dis = dis_text(recovered, False) == dis_text(rebuilt, False)
    same_dis_lines = dis_text(recovered, True) == dis_text(rebuilt, True)
    print()
    print("code objects: %d recovered, %d rebuilt"
          % (len(list(walk(recovered))), len(list(walk(rebuilt)))))
    print("recursive dis identical, line numbers ignored: %s" % same_dis)
    print("recursive dis identical, line numbers included: %s" % same_dis_lines)
    print("marshal payload byte-identical (all positions included): %s" % same_payload)
    print("source size: original %d bytes (pyc header), reconstruction %d bytes"
          % (int.from_bytes(header[12:16], "little"), len(source)))
    ok = semantic_ok and same_dis and same_dis_lines
    print("RESULT: %s" % ("EQUIVALENT" if ok else "NOT EQUIVALENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
