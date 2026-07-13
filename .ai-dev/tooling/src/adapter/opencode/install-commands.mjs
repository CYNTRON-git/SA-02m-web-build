// OpenCode realisation of the adapter's explicit-setup-command contract point.
// Assembles the platform-neutral command body (src/adapter/commands/dev-setup.body.md) +
// the OpenCode per-command frontmatter (src/adapter/opencode/commands/dev-setup.fm) into
// an OpenCode command file in **.opencode/commands/** (PLURAL — opencode 1.17
// loads the plural command dir, matching its plural agents/ + plugins/ dirs; the
// singular .opencode/command/ is NOT loaded, the same dogfood finding as the other
// dirs, src/adapter/INSTALL.md). On OpenCode the markdown BODY is the prompt template
// (the `template` field is only for the opencode.json inline `command` form), and
// the frontmatter targets the `ai-dev` agent so the orchestrator runs setup.
// Concatenation, NOT a generator: the neutral body is the single source (shared
// with the Claude adapter) — a thin wrapper pointing at the orchestrator's
// `## Setup`, never restating the dialog (single home, PROTOCOL.md invariant 6).
//
// Run from the repo root: node src/adapter/opencode/install-commands.mjs [outDir]
//   outDir defaults to .opencode/commands/ (pass a temp dir to dry-run).

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

// Assemble the setup command into outDir. Returns the path written so a test can
// read the result without re-deriving it.
export function install(outDir) {
  fs.mkdirSync(outDir, { recursive: true });
  const fm = fs.readFileSync(path.join(ROOT, "src", "adapter", "opencode", "commands", "dev-setup.fm"), "utf8").trim();
  const body = fs.readFileSync(path.join(ROOT, "src", "adapter", "commands", "dev-setup.body.md"), "utf8").trimStart();
  const out = `---\n${fm}\n---\n\n${body}`;
  const outPath = path.join(outDir, "dev-setup.md");
  fs.writeFileSync(outPath, out);
  console.log(`wrote ${path.relative(ROOT, outPath)}  (/dev-setup command, agent ai-dev)`);
  return outPath;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const outDir = process.argv[2] ? path.resolve(process.argv[2]) : path.join(ROOT, ".opencode", "commands");
  install(outDir);
}
