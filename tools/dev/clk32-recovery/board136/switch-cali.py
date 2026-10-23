# Bench-only: switch SYS_32K to the calibrated CCU RC and report.
import mmap, os, time
fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x01C20000)
w = memoryview(m).cast('I')
print('before 0x310=%08x 0x314=%08x WDT CTRL=%08x MODE=%08x' % (w[0x310//4], w[0x314//4], w[0xC90//4], w[0xC94//4]))
w[0x314 // 4] = 3
w[0x310 // 4] = 0x16AA0000 | 0x1F
time.sleep(1.0)
w[0xC90 // 4] = (0xA57 << 1) | 1
time.sleep(0.05)
print('after  0x310=%08x 0x314=%08x WDT CTRL=%08x (restart bit clears: %s)' % (w[0x310//4], w[0x314//4], w[0xC90//4], w[0xC90//4] & 1 == 0))
