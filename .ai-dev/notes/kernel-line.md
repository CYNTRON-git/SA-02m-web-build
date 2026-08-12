# Which kernel the SA-02m fleet actually runs

Durable project knowledge. Recorded 2026-08-06 because a repo-wide cleanup rests
on it and it had lived only in a gitignored session pointer.

## The fact

**The whole fleet runs the 6.1 line** (Operator, 2026-08-06) — not the 5.10 line
the deleted port targeted.

The exact SMP/RT pair the web panel switches between lives in
**`docs/contracts/kernel-conditional-services.md` §6** and is not repeated here:
that copy is the gated one (`kernel-policy-contract` pin 8 reads it), so a copy
in this note could drift with nothing to catch it.

Verified independently on the bench board the same day: `uname -r`, both module
directories present, and `/etc/sa02m_kernel.conf` naming exactly the §6 pair.

**Producer:** `tools/buildroot/prepare-rt-docker-kernel.sh` (Starterkit VM +
Buildroot). Deploy: `tools/buildroot/sa02m-kernel-deploy.sh install-smp|install-rt`,
then `sa02m-kernel-select.sh init`. This is the only kernel build path in the
repo.

## What this fact settled

The 2026-07 port to the `wirenboard/linux` fork — `kernel-port/`,
`tools/kernel-wb/`, and the `build-sa02m-kernel` workflow — built kernel
**5.10.35**, a line **no device runs anywhere**. Nothing consumed its output,
which is also why its workflow sat red for a month unnoticed. On the Operator's
decision it was **deleted** (2026-08-06) rather than labelled historical;
history stays in git.

`tools/buildroot/README.md` had this inverted: it called itself the legacy path
being superseded by the WB port. It was the incumbent the whole time.

## Where the machine-checkable half lives

The kernel pair is a contract, not just a note:
`docs/contracts/kernel-conditional-services.md` §6, gated by
`.ai-dev/quality/checks/kernel-policy-contract.sh` (pin 8) — the defaults in
`etc/sa02m-kernel-select.sh` must name this pair, and the detector's whitelist
must match both. Change the fleet's kernel line ⇒ change the contract, and the
gate will point at every place that has to move with it.

## Still-stale text this note does NOT cover

Several comments elsewhere still assert the 5.10 line as a premise
(`scripts/01-system.sh`, `scripts/02-network.sh`, `scripts/lib.sh`,
`tools/debian-rootfs/`, `docs/contracts/ethernet-iface-naming.md`). They are
enumerated in the backlog entry on stale 5.10.35 assumptions, which stays OPEN.
