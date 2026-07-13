// Claude realisation of the adapter's explicit-setup-command contract point.
// Assembles the platform-neutral command body (src/adapter/commands/dev-setup.body.md) +
// the Claude per-command frontmatter (src/adapter/claude/commands/dev-setup.fm) into a
// Claude slash-command file (.claude/commands/dev-setup.md → the `/dev-setup` command).
// Concatenation, NOT a generator: the neutral body is the single source (shared
// with the OpenCode adapter), the .fm adds only Claude's frontmatter. The body is
// a thin wrapper — it points at the orchestrator's `## Setup`, never restates the
// dialog (single home, PROTOCOL.md invariant 6). The command body becomes the
// prompt injected into the session; the orchestrator IS the session.
//
// Run from the repo root: node src/adapter/claude/install-commands.mjs [outDir]
//   outDir defaults to .claude/commands/ (pass a temp dir to dry-run).

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

// Assemble the setup command into outDir. Returns the path written so a test can
// read the result without re-deriving it.
export function install(outDir) {
  fs.mkdirSync(outDir, { recursive: true });
  const fm = fs.readFileSync(path.join(ROOT, "src", "adapter", "claude", "commands", "dev-setup.fm"), "utf8").trim();
  const body = fs.readFileSync(path.join(ROOT, "src", "adapter", "commands", "dev-setup.body.md"), "utf8").trimStart();
  const out = `---\n${fm}\n---\n\n${body}`;
  const outPath = path.join(outDir, "dev-setup.md");
  fs.writeFileSync(outPath, out);
  console.log(`wrote ${path.relative(ROOT, outPath)}  (/dev-setup command)`);
  return outPath;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const outDir = process.argv[2] ? path.resolve(process.argv[2]) : path.join(ROOT, ".claude", "commands");
  install(outDir);
}
