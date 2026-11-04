import marshal, dis, sys, types
p = sys.argv[1]; mode = sys.argv[2]
with open(p, "rb") as f:
    f.read(16); code = marshal.load(f)
def walk(c, depth=0):
    yield c, depth
    for k in c.co_consts:
        if isinstance(k, types.CodeType):
            yield from walk(k, depth + 1)
if mode == "strings":
    for c, d in walk(code):
        doc = c.co_consts[0] if c.co_consts and isinstance(c.co_consts[0], str) else ""
        print("%s== %s (line %d) args=%s" % ("  " * d, c.co_qualname, c.co_firstlineno, c.co_varnames[:c.co_argcount]))
        for k in c.co_consts:
            if isinstance(k, (str, bytes, int, float)) and not isinstance(k, bool):
                s = repr(k)
                print("%s   %s" % ("  " * d, s[:400]))
        print("%s   names: %s" % ("  " * d, ", ".join(c.co_names)[:600]))
else:
    dis.dis(code)
