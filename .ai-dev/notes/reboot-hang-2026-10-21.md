# Software reboot hangs in the kernel's device_shutdown (bench 136, 2026-10-21)

Durable project knowledge: what is established, what is ruled out, how to see
the shutdown path on this board, and the watchdog facts that decide whether a
hung reboot ever recovers. Evidence files live in
`.ai-dev/audit/remediation-20261020/` (gitignored): `reboot-diag2-136.log`,
`reboot-trace2-136.log`, `test-d-136.log`, `netconsole-136.log`,
`state-before-trace3-136.log`, `usb-state-136.log`.

## Established

- `systemctl reboot` hangs after **systemd-shutdown has finished** («Rebooting.»
  logged) — inside the kernel's `device_shutdown()` or the restart handler.
  systemd stops every unit cleanly; the journal's last line «Journal stopped» is
  normal, not the failure.
- It hangs on **both** fleet kernels (SMP `6.1.0-rc6` builds of Jul 30 and
  Oct 15) — the kernel build is not the cause. It did not hang on the same
  software (1.0.7.5 candidate, old kernel) installed over the long-lived
  1.0.7.1 board earlier the same day (3 clean reboots), nor on the Operator's
  fresh golden+OTA 1.0.7.3 base before our full `install.sh` ran on it.
- Soft-disconnecting the USB gadget before the reboot does not help
  (`/sys/class/udc/musb-hdrc.2.auto/soft_connect`), so the PC on the OTG port
  is not the trigger.
- The last line the trace can show over the network is
  `mmcblk mmc2:0001: shutdown` — `initcall_debug` prints the line *before* the
  callback, and Ethernet (`dwmac-sun8i`) is shut down a millisecond earlier,
  so everything after is invisible without a UART.

## Why the hardware watchdog does not save it (and the fix)

- systemd arms `sunxi-wdt` only inside `systemd-shutdown` (after all units are
  stopped) and only when `RebootWatchdogSec` > 0; the project policy set it to
  `0` (BUGLOG 2026-08-05 — the «slow shutdown gets cut by the watchdog» chain
  was explicitly left a hypothesis; the confirmed defect was only EINVAL for
  `2min` above the 16 s driver cap). With `0`, PID 1 closes the device with
  the magic close and the watchdog is **disarmed** for the whole reboot.
- Even when armed (`RebootWatchdogSec=16s` was tried), the kernel's watchdog
  core stops the watchdog in `kernel_restart_prepare()` through its reboot
  notifier (`watchdog_stop_on_reboot`, requested by `sunxi_wdt_probe`) —
  *before* `device_shutdown()`. So a hang there is never reset.
- The core exposes `watchdog.stop_on_reboot=0` as a kernel command-line
  parameter (`drivers/watchdog/watchdog_core.c`, module param of the built-in
  core, read-only at runtime): with it the watchdog keeps running across the
  restart and resets the board ≤16 s after the last ping. The restart handler
  re-arms it at 0.5 s anyway on a normal reboot, so a normal reboot is not
  affected. The boot script is the only place to set it (`boot.scr` →
  `setenv bootargs …`; the DT and modprobe.d cannot).
- Bench 136 carries the trial `boot.scr` (`watchdog.stop_on_reboot=0
  console=tty0 ignore_loglevel initcall_debug`, 331 bytes; original 261-byte
  script backed up in `/root/kernel-backup-20261021/fat/boot.scr.orig`) and a
  persistent `/etc/systemd/system.conf.d/zz-bench-rebootwdt.conf`
  (`RebootWatchdogSec=16s`). Both are bench state until the repo payload and
  the policy file change through the normal loop and are observed on hardware.

## How to see the kernel's shutdown on this board

- There is no debug UART: `COM1 = ttyS0 = UART0`; the kernel console is `tty0`
  (HDMI) and both fleet kernels have no `sysrq`, no `netconsole`, no
  `/proc/config.gz`.
- The board exposes a **USB serial gadget** (`g_serial`, OTG in
  `b_peripheral`, `/dev/ttyGS0`); a PC on the OTG cable sees it as a COM port
  (VID 0525 / PID A4A7, «PI USB to Serial»). The port also powers the board
  from the PC on the bench.
- Working trace recipe (no reboot of the PC side): on the board set
  `printk 8`, `initcall_debug=1`, `systemd-analyze log-target kmsg
  log-level info`; run a `/dev/kmsg` tail as a process whose `argv[0]` starts
  with `@` and move it to `init.scope` (`/sys/fs/cgroup/init.scope/cgroup.procs`)
  so the kill sweep spares it; have it write to `/dev/ttyGS0` (and UDP while
  the network lives). Clear `ExecStop` of `networking.service` /
  `ifup@eth0.service` with runtime drop-ins if the UDP half matters. The
  gadget outlives Ethernet and eMMC in `device_shutdown` (musb is registered
  early, so it is shut down late).

## Not yet known

- Which device's shutdown callback (or the restart handler) hangs. Next step:
  the `ttyGS0` trace above, then unbinding suspects before the reboot
  (USB hosts, 8250 ports, I2C adapters) with the watchdog backstop making each
  attempt self-recovering.
- Why only this base: the fresh golden+OTA base shows `mv64xxx: I2C bus
  locked` on i2c-1/i2c-2 every ~15 s (9 in 2 min; 0 on 135 in 3 h) — an
  expansion-board SDA stuck low, the storm the `sa02m-i2c2-unbind` udev rule
  exists for. It is the strongest hardware-state difference seen so far.
