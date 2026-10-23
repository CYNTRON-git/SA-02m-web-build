# Time series of the SYS_32K rate under each 0x314 calibration value, per divider.
import mmap, os, sys, time
fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x01C20000)
w = memoryview(m).cast('I')
assert w[0xC94 // 4] & 1 == 0, 'WDT enabled - hold the runtime watchdog first'
def rate(win=0.5):
    c0 = w[0xC30 // 4]; w[0xC30 // 4] = 0; w[0xC34 // 4] = 0xFFFFFFFF; w[0xC30 // 4] = 2
    time.sleep(0.01); w[0xC30 // 4] = 3
    a = w[0xC38 // 4]; t = time.monotonic(); time.sleep(win); b = w[0xC38 // 4]
    dt = time.monotonic() - t; w[0xC30 // 4] = c0
    return ((a - b) & 0xFFFFFFFF) / dt
s0 = w[0x310 // 4]
for div, cali in [(31, 0), (31, 1), (31, 2), (31, 3), (0, 1), (0, 2), (0, 3)]:
    w[0x314 // 4] = 0; w[0x310 // 4] = 0x16AA0000 | div; time.sleep(0.2)
    w[0x314 // 4] = cali
    t0 = time.monotonic(); row = []
    for _ in range(8):
        row.append('%.0f' % rate()); 
    print('div=%2d cali=%d 0x314=%08x rates: %s' % (div, cali, w[0x314 // 4], ' '.join(row)))
w[0x314 // 4] = 0
w[0x310 // 4] = 0x16AA0000 | (s0 & 0x11F)
print('restored 0x310=%08x 0x314=%08x' % (w[0x310 // 4], w[0x314 // 4]))
