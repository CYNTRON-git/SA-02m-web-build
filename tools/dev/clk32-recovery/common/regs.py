#!/usr/bin/env python3
"""Read A40i (sun8i-r40) watchdog, timer and RTC/LOSC registers through /dev/mem (read-only)."""
import mmap, os, struct, sys, time

def rd(base, offs):
    page = base & ~0xFFF
    fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
    try:
        m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ, offset=page)
        out = {}
        for o in offs:
            a = base - page + o
            out[o] = struct.unpack("<I", m[a:a + 4])[0]
        m.close()
        return out
    finally:
        os.close(fd)

TIMER = 0x01C20C00
WDT = TIMER + 0x90      # sun4i-style WDT: CTRL +0x00, MODE +0x04
RTC = 0x01C20400

t = rd(TIMER, [0x00, 0x04, 0x10, 0x14, 0x18, 0x1C])
w = rd(WDT, [0x00, 0x04])
r = rd(RTC, [0x00, 0x04, 0x08, 0x0C, 0x10, 0x14])
print("TIMER IRQ_EN=%08x IRQ_STA=%08x T0_CTRL=%08x T0_INTV=%08x T0_CUR=%08x" % (t[0x00], t[0x04], t[0x10], t[0x14], t[0x18]))
mode = w[0x04]
print("WDT  CTRL=%08x MODE=%08x  -> enable=%d reset_en=%d interval_code=%d" % (w[0x00], mode, mode & 1, (mode >> 1) & 1, (mode >> 3) & 0xF))
print("RTC  LOSC_CTRL=%08x LOSC_AUTO_SWT_STA=%08x INTOSC_PRESCALER=%08x YMD=%08x HMS=%08x" % (r[0x00], r[0x04], r[0x08], r[0x10], r[0x14]))
print("     LOSC_SRC_SEL(bit0)=%s  ext-LOSC auto-switch status(STA bit0)=%d pending(bit1)=%d" % ("EXTERNAL 32k" if r[0x00] & 1 else "internal RC", r[0x04] & 1, (r[0x04] >> 1) & 1))
# Is the RTC second counter running?
s1 = rd(RTC, [0x14])[0x14]; time.sleep(2.2); s2 = rd(RTC, [0x14])[0x14]
print("     RTC HMS before=%08x after 2.2 s=%08x -> %s" % (s1, s2, "RUNNING" if s1 != s2 else "STOPPED (LOSC dead?)"))
