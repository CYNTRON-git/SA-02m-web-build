// OpenCode platform wiring: assemble the three agents + the setup command, generate
// the boundary-deny plugin, merge opencode.json keys (without dropping a project's
// own), and import the constitution via AGENTS.md.

import fs from "node:fs";
import path from "node:path";
import { runScript, ensureLine } from "./install-fs.mjs";
import { stripBreadcrumbFile } from "./install-breadcrumb.mjs";

// 5b. Wire OpenCode: assemble the three agents + the setup command + generate the
// plugin (downstream layout, since the adapter is now vendored under .ai-dev/tooling/),
// merge opencode.json keys, and import the constitution via AGENTS.md.
export function wireOpenCode(target, dogfood) {
  const cfg = { AI_DEV_CONFIG: path.join(target, ".ai-dev", "config.json") };
  const scriptBase = dogfood ? path.join(target, "src") : path.join(target, ".ai-dev", "tooling", "src");
  runScript(scriptBase, path.join("adapter", "opencode", "install-agents.mjs"), [path.join(target, ".opencode", "agents")], cfg);
  runScript(scriptBase, path.join("adapter", "opencode", "install-commands.mjs"), [path.join(target, ".opencode", "commands")], cfg);
  // The plugin generator resolves the adapter layout from the TARGET root (passed via
  // --root). Downstream: the target has .ai-dev/tooling/src/adapter ⇒ tooling-submodule
  // import path. Dogfood: the target has src/adapter/opencode/plugin-entry.mjs ⇒
  // detectLayout resolves the DEV layout ⇒ the deployed plugin imports ../../src/adapter
  // (the committed self-host form) — no extra flag needed, the existing signal handles it.
  runScript(
    scriptBase,
    path.join("adapter", "opencode", "install-plugin.mjs"),
    [path.join(target, ".opencode", "plugins", "ai-dev.mjs"), "--root", target],
    cfg,
  );

  // opencode.json — merge the protocol's keys without dropping a project's own.
  // Key order matters for byte-idempotency in dogfood mode: the committed self-host
  // file leads with $schema and orders permission question→edit→bash→webfetch, so
  // dogfood emits exactly that canonical shape (the committed opencode.json is the
  // single home of that form; the installer converges to it rather than the reverse).
  const ocPath = path.join(target, ".opencode", "opencode.json");
  const existing = fs.existsSync(ocPath) ? JSON.parse(fs.readFileSync(ocPath, "utf8")) : {};
  // The protocol plugin is the SOLE project-boundary guard; OpenCode's native
  // permission dial is set to full-speed inside the project. Division of labor:
  // plugin = the mechanical boundary (deny outside-root reads/writes/finds);
  // native permission = speed dial for tool calls inside the boundary.
  // Honest residual: bash boundary is best-effort (the engine parses obvious path
  // tokens; an opaque escape like `python3 -c` is not mechanically caught). edit/read/write
  // tool checks are exact; webfetch=allow because research needs it (exfil via
  // HTTP is a separate persona rule, not a filesystem-boundary concern).
  const instructionsEntry = dogfood ? "PROTOCOL.md" : ".ai-dev/PROTOCOL.md";
  // The boundary-deny plugin MUST be registered in the `plugin` key — OpenCode
  // 1.17.8 dropped project-folder plugin auto-discovery, so a plugin sitting at
  // .opencode/plugins/ai-dev.mjs loads ONLY when listed here. Without this key the
  // whole [mechanical] floor is silently absent on every downstream (the bug this
  // restores). The spec is `.opencode/`-relative — opencode resolves a relative
  // plugin path against the dir of opencode.json (i.e. .opencode/), NOT the project
  // root — so it is `./plugins/ai-dev.mjs` (the plugin always deploys to
  // .opencode/plugins/ai-dev.mjs in both dogfood and downstream layouts). De-duped
  // via Set so a project's own `plugin` entries are preserved, never clobbered, and
  // a re-install never doubles ours.
  const PLUGIN_ENTRY = "./plugins/ai-dev.mjs";
  let oc;
  if (dogfood) {
    oc = {
      ...(existing.$schema ? { $schema: existing.$schema } : { $schema: "https://opencode.ai/config.json" }),
      default_agent: "ai-dev",
      instructions: Array.from(new Set([...(existing.instructions || []), instructionsEntry])),
      permission: { ...(existing.permission || {}), question: "allow", edit: "allow", bash: "allow", webfetch: "allow" },
      agent: { ...(existing.agent || {}), build: { disable: true }, plan: { disable: true } },
      plugin: Array.from(new Set([...(existing.plugin || []), PLUGIN_ENTRY])),
    };
  } else {
    oc = { ...existing };
    oc.default_agent = "ai-dev";
    oc.instructions = Array.from(new Set([...(oc.instructions || []), instructionsEntry]));
    oc.permission = { ...(oc.permission || {}), edit: "allow", bash: "allow", webfetch: "allow", question: "allow" };
    oc.agent = { ...(oc.agent || {}), build: { disable: true }, plan: { disable: true } };
    oc.plugin = Array.from(new Set([...(oc.plugin || []), PLUGIN_ENTRY]));
  }
  fs.mkdirSync(path.dirname(ocPath), { recursive: true });
  fs.writeFileSync(ocPath, JSON.stringify(oc, null, 2) + "\n");

  // AGENTS.md is OpenCode's always-on surface. Downstream: import the constitution
  // via an @-line (replacing any breadcrumb). Dogfood: the committed AGENTS.md is a
  // rich hand-authored file that loads PROTOCOL.md via opencode.json `instructions`,
  // NOT an @-import — so dogfood only strips a stale breadcrumb and never appends an
  // @-line that isn't in the committed form.
  const agentsMd = path.join(target, "AGENTS.md");
  stripBreadcrumbFile(agentsMd);
  if (!dogfood) ensureLine(agentsMd, "@.ai-dev/PROTOCOL.md");
}
