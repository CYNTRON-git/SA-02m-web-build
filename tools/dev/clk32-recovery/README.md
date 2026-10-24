# clk32-recovery — bench evidence, not tooling

Files recovered from the bench boards on 2026-10-23 after the workstation loss,
plus the `sa02m-losc-fallback` source rebuilt from its bytecode on 2026-10-24.
What each file is, where it came from, which ones write `/dev/mem` and how
faithful the rebuild is: `.ai-dev/notes/clk32-crystal-recovery-2026-10-22.md`.
Nothing here is installed or shipped; never run the `/dev/mem` writers on a
fleet device.

Check the rebuild against the recovered bytecode (needs a CPython 3.12; which
releases also reproduce every position: the script's header):

    py -3.12 -I tools/dev/clk32-recovery/losc-fallback/compare_dis.py
