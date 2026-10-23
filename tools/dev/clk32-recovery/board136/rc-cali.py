# Measure the CCU RC (via Timer2 on SYS_32K) under each calibration setting.
# The WDT is disabled by the caller (systemd runtime watchdog held), so a fast
# rate cannot reset the board. Restores 0x310/0x314 to Linux's values at exit.
import mmap, os, time
fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x01C20000)
w = memoryview(m).cast('I')
assert w[0xC94 // 4] & 1 == 0, 'WDT enabled - hold the runtime watchdog first'
def rate():
    c0 = w[0xC30 // 4]; w[0xC30 // 4] = 0; w[0xC34 // 4] = 0xFFFFFFFF; w[0xC30 // 4] = 2
    time.sleep(0.01); w[0xC30 // 4] = 3
    a = w[0xC38 // 4]; t = time.monotonic(); time.sleep(0.5); b = w[0xC38 // 4]
    dt = time.monotonic() - t; w[0xC30 // 4] = c0
    return ((a - b) & 0xFFFFFFFF) / dt
s0, c0 = w[0x310 // 4], w[0x314 // 4]
print('start 0x310=%08x 0x314=%08x' % (s0, c0))
for div in (31, 16, 1, 0):
    w[0x310 // 4] = 0x16AA0000 | div; time.sleep(0.05)
    print('div=%2d cali=%08x rate=%.0f Hz' % (div, w[0x314 // 4], rate()))
w[0x310 // 4] = 0x16AA0000 | 31
for cali in (1, 2, 3):
    w[0x314 // 4] = cali; time.sleep(0.3)
    print('div=31 cali write %d -> %08x rate=%.0f Hz' % (cali, w[0x314 // 4], rate()))
w[0x314 // 4] = c0
w[0x310 // 4] = 0x16AA0000 | (s0 & 0x11F)
print('restored 0x310=%08x 0x314=%08x' % (w[0x310 // 4], w[0x314 // 4]))
