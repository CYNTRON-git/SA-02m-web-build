// The cross-platform breadcrumb (backlog: downstream field report 2026-06-13) —
// shared by both platform wirings (install-claude / install-opencode).
// The INACTIVE platform always gets a minimal load-surface: a session opened on the
// unwired platform otherwise starts with zero protocol surface and cannot even know
// what it is missing (the platform-switch offer is prose that never loads there).
// The block is marker-delimited so a re-run REPLACES it (never duplicates, never
// clobbers the file's real content) and the active wiring can strip it.

import fs from "node:fs";
import path from "node:path";

const BREADCRUMB_OPEN = "<!-- ai-dev:breadcrumb -->";
const BREADCRUMB_CLOSE = "<!-- /ai-dev:breadcrumb -->";

// Remove a previously written breadcrumb block from a text, leaving the real
// content (normalised to one trailing newline) — the strip half of replace.
export function stripBreadcrumb(text) {
  const start = text.indexOf(BREADCRUMB_OPEN);
  if (start < 0) return text;
  const close = text.indexOf(BREADCRUMB_CLOSE);
  const end = close < 0 ? text.length : close + BREADCRUMB_CLOSE.length;
  const kept = text.slice(0, start) + text.slice(end).replace(/^\n+/, "");
  return kept.replace(/\n+$/, "\n").replace(/^\n$/, "");
}

// Strip the breadcrumb from a file in place — called by the active platform's
// wiring: when a platform becomes active, its full wiring REPLACES the breadcrumb.
export function stripBreadcrumbFile(file) {
  if (!fs.existsSync(file)) return;
  const existing = fs.readFileSync(file, "utf8");
  const kept = stripBreadcrumb(existing);
  if (kept !== existing) fs.writeFileSync(file, kept);
}

// Write (replace) the breadcrumb on the inactive platform's load surface. Skipped
// when that surface already carries the real protocol import (a prior platform
// switch left both wirings live) — a "not wired" claim there would be false.
export function writeInactiveBreadcrumb(target, activePlatform, dogfood) {
  // Dogfood mode: BOTH load surfaces (CLAUDE.md and AGENTS.md) are real,
  // hand-authored, committed files carrying their own protocol content — appending a
  // breadcrumb to the inactive one would churn a tracked file (break idempotency).
  // The self-host repo always carries both wirings; no breadcrumb is ever needed.
  if (dogfood) return;
  const inactive = activePlatform === "claude" ? "opencode" : "claude";
  const file = path.join(target, inactive === "claude" ? "CLAUDE.md" : "AGENTS.md");
  const existing = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
  const kept = stripBreadcrumb(existing);
  if (kept.split("\n").some((l) => l.trim() === "@.ai-dev/PROTOCOL.md")) {
    if (kept !== existing) fs.writeFileSync(file, kept);
    return;
  }
  const harness = inactive === "claude" ? "Claude Code" : "OpenCode";
  const block = [
    BREADCRUMB_OPEN,
    `This project runs the **ai-dev protocol**; its active platform is **${activePlatform}** — this ${harness} session has no protocol wiring yet.`,
    `Run \`node .ai-dev/tooling/src/adapter/install.mjs . --platform ${inactive}\` to wire ${harness}, then offer the Operator the platform switch (\`.ai-dev/tooling/src/agents/orchestrator.md\` \`## Setup\`, "Platform switch").`,
    BREADCRUMB_CLOSE,
  ].join("\n");
  const sep = kept ? (kept.endsWith("\n") ? "\n" : "\n\n") : "";
  fs.writeFileSync(file, kept + sep + block + "\n");
}
