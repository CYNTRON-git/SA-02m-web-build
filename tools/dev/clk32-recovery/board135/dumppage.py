import mmap, os, struct, sys
base = int(sys.argv[1], 16)
fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ, offset=base)
for o in range(0, 0x1000, 4):
    # skip PIO and RTC data regs? read everything 32-bit
    v = struct.unpack("<I", m[o:o+4])[0]
    print("%08x %08x" % (base + o, v))
