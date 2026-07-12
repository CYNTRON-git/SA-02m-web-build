// ssh-content-edit — the remote-edit DENY floor distinguishes a remote WRITE from a
// remote READ that merely uses a redirect (invariant 4 / protocol-reference.md `## Enforcement`).
//
// The defect this pins: the `>` redirect branch of the `sshContentEdit` predicate used
// to fire on ANY redirect target, so a read-only diagnostic (`ssh host 'cmd 2>/dev/null'`,
// `ssh host 'cmd > /dev/null'`) was WRONGLY denied — a non-toggleable deny with no
// granular escape. The fix reuses isStreamTarget (the single home for "what is a stream")
// to exclude stream targets, while a redirect to a REAL file still denies (floor unchanged).
//
// Each case asserts the ALLOW/DENY edge through the REAL deny entry, evaluate() — the
// same function both platform shims call. A temp root is supplied (no config needed: a
// deny-class rule fires regardless of safeguards). Cross-platform parity for the new
// allow is asserted in parity.test.mjs.
//
// Run: node src/adapter/ssh-content-edit.test.mjs

import { evaluate, loadConfig } from "./engine.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const config = loadConfig(HERE);
const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-ssh-"));

let pass = 0, fail = 0;
function check(name, got, want) {
  if (got === want) { pass++; }
  else { fail++; console.log(`  ✗ ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); }
}
function verdict(command) {
  return evaluate({ act: "bash", root, command }, config);
}

// ── ALLOW: a remote READ that only uses a stream redirect (the fixed false-positive) ──
console.log("STREAM REDIRECT IS READ-ONLY (allows):");
for (const [name, cmd] of [
  ["stderr-to-devnull",   'ssh host "tail -f /var/log/syslog 2>/dev/null"'],
  ["stdout-to-devnull",   'ssh host "cat /etc/hostname > /dev/null"'],
  ["stderr-to-stdout",    'ssh host "dmesg 2>&1 | tail"'],
  ["both-stream-and-pipe",'ssh host "journalctl -u nginx 2>&1 | grep error"'],
  ["stdout-stream-dev",   'ssh host "systemctl status nginx > /dev/stdout"'],
]) {
  const v = verdict(cmd);
  check(`allow:${name}`, v.verdict, "allow");
}

// ── DENY: a genuine remote EDIT — the floor must NOT weaken ────────────────────────────
console.log("GENUINE REMOTE EDIT (denies):");
for (const [name, cmd] of [
  ["redirect-realfile",   'ssh host "echo x > /etc/config"'],
  ["append-realfile",     'ssh host "cat foo >> /etc/bar"'],
  ["redirect-no-space",   'ssh host "echo x >/etc/config"'],
  ["fd-prefixed-realfile",'ssh host "echo x 1>/etc/config"'],
  ["quoted-realfile",     `ssh host 'echo x > /etc/config'`],
  ["sed-in-place",        'ssh host "sed -i s/a/b/ /etc/f"'],
  ["tee-realfile",        'ssh host "echo x | tee /etc/config"'],
  ["vi-edit",             'ssh host "vi /etc/config"'],
  ["vim-edit",            'ssh host "vim /etc/config"'],
  ["nano-edit",           'ssh host "nano /etc/config"'],
  // Fail-closed: a stream-LOOKING but non-exact target is a write, not a stream.
  ["devnull-traversal",   'ssh host "echo x > /dev/null/../etc/passwd"'],
  ["devnull-lookalike",   'ssh host "echo x > /dev/null2"'],
]) {
  const v = verdict(cmd);
  check(`deny:${name}`, v.verdict, "deny");
  check(`deny:${name}:ruleId`, v.ruleId, "ssh-content-edit");
}

// ── Not an ssh command: the predicate must not fire on a LOCAL redirect ────────────────
console.log("NO SSH (predicate inert; ssh-content-edit never fires):");
for (const [name, cmd] of [
  ["local-redirect-realfile", 'echo x > /etc/config'],     // local write — a different boundary, not this rule
  ["local-stream",            'cat foo 2>/dev/null'],
]) {
  const v = verdict(cmd);
  check(`nossh:${name}:not-ssh-content-edit`, v.ruleId === "ssh-content-edit", false);
}

fs.rmSync(root, { recursive: true, force: true });
console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
