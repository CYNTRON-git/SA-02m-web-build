#!/usr/bin/env python3
"""Does the timer block's 32 kHz input tick? Uses the unused Timer2 (CTRL was 0x4, disabled) and
restores it. Also pings the WDT once and watches whether its restart bit self-clears."""
import mmap, os, struct, time
B = 0x01C20000
fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=B)
def rd(a): return struct.unpack("<I", m[a - B:a - B + 4])[0]
def wr(a, v): m[a - B:a - B + 4] = struct.pack("<I", v)
T2C, T2I, T2V = 0x01C20C30, 0x01C20C34, 0x01C20C38
WCTRL = 0x01C20C90
orig = rd(T2C)
print("T2 CTRL orig=%08x" % orig)
for name, src in (("OSC24M (control)", 1), ("LOSC 32k", 0)):
    wr(T2C, 0); wr(T2I, 0xFFFFFFFF)
    wr(T2C, (src << 2) | 0x2)            # reload, disabled
    t = time.time()
    while rd(T2C) & 2 and time.time() - t < 1.0: pass
    rl = "reload bit cleared" if not rd(T2C) & 2 else "reload bit STUCK (no clock)"
    wr(T2C, (src << 2) | 0x1)            # enable
    a = rd(T2V); time.sleep(1.0); b = rd(T2V)
    print("%-17s %s  CUR %08x -> %08x  ticks/s=%d" % (name, rl, a, b, (a - b) & 0xFFFFFFFF))
wr(T2C, orig)
print("T2 CTRL restored=%08x" % rd(T2C))
print("WDT CTRL before ping=%08x" % rd(WCTRL))
wr(WCTRL, (0xA57 << 1) | 1)
for d in (0, 0.001, 0.05, 0.5, 2.0):
    time.sleep(d); print("  +%.3fs WDT CTRL=%08x" % (d, rd(WCTRL)))
m.close(); os.close(fd)
