// Installer primitives — the source root, the platform allow-list, and the
// idempotent filesystem/process helpers every install step composes. Context-free
// building blocks (no install business logic): the step modules (install-core,
// install-claude, install-opencode, install-version) import from here.
//
// This is ADAPTER-LAYER code: platform-specific by nature, so concrete platform
// names (claude / opencode) are allowed here.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

// This module lives at <repo>/src/adapter/install-fs.mjs — SOURCE is the protocol repo
// root (two dirs up from src/adapter/), the tree we copy FROM. Every sibling install-*
// module sits in the same dir, so importing SOURCE from here gives one home (invariant 6)
// for that fact. When the vendored installer re-runs from .ai-dev/tooling/src/adapter/,
// SOURCE resolves to .ai-dev/tooling/ — correct for the upgrade re-run.
export const SOURCE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

export const PLATFORMS = ["claude", "opencode"];

// ── filesystem helpers ───────────────────────────────────────────────────────

// Recursively copy a directory tree, OVERWRITING files (so a re-run converges to
// the same bytes — idempotent). node:fs cpSync does this in one call; the explicit
// recursive form keeps the behaviour obvious and lets us skip node_modules / dot-git
// noise that must never be vendored. Identity guard: when the vendored installer
// re-runs from .ai-dev/tooling/ (the upgrade path), SOURCE and the vendor target
// coincide — copying a tree onto itself must be a no-op, never a truncation.
export function copyTree(src, dest) {
  if (path.resolve(src) === path.resolve(dest)) return;
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === ".git") continue;
    const from = path.join(src, entry.name);
    const to = path.join(dest, entry.name);
    if (entry.isDirectory()) copyTree(from, to);
    else if (entry.isFile()) fs.copyFileSync(from, to);
  }
}

// Copy a single file, overwriting (idempotent — same bytes on a re-run). Same
// identity guard as copyTree (the vendored self-re-run case).
export function copyFile(src, dest) {
  if (path.resolve(src) === path.resolve(dest)) return;
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.copyFileSync(src, dest);
}

// Copy a file ONLY when the destination is absent — never clobber a project's real
// file (the doc-template rule: lay down a template where the target has none).
export function copyIfAbsent(src, dest) {
  if (fs.existsSync(dest)) return false;
  copyFile(src, dest);
  return true;
}

// Append a line to a text file only if it is not already present — idempotent
// import wiring (CLAUDE.md / AGENTS.md). Creates the file if absent.
export function ensureLine(file, line) {
  const existing = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
  if (existing.split("\n").some((l) => l.trim() === line.trim())) return false;
  const sep = existing && !existing.endsWith("\n") ? "\n" : "";
  fs.writeFileSync(file, existing + sep + line + "\n");
  return true;
}

// Run an install script as a child process (node, argv array — no shell).
// `scriptBase` is the dir the script's `scriptRelPath` is resolved against: the
// vendored tooling copy in downstream mode, the source tree (<target>/src/...) in
// dogfood mode — the children self-locate their ROOT three dirs up, so the source
// copies point ROOT at the repo root (correct for self-host). `env` overrides
// (e.g. AI_DEV_CONFIG) layer onto the current environment.
export function runScript(scriptBase, scriptRelPath, args, env) {
  const script = path.join(scriptBase, scriptRelPath);
  execFileSync("node", [script, ...args], {
    stdio: "inherit",
    env: { ...process.env, ...env },
  });
}
