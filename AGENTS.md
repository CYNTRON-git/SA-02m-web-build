@.ai-dev/PROTOCOL.md

Read `.ai-dev/PROTOCOL.md` before working. Project rules are in
`docs/agent-rules/`; the active plan is linked from `.ai-dev/state/current.md`.

## Installed agent environments

The default `.ai-dev/launch` path remains Claude, matching
`.ai-dev/config.json`. Use `--claude`, `--opencode`, or `--codex` as its first
argument to select a launcher explicitly; remaining arguments pass through.
Run the launcher from the project root. OpenCode and Codex use their own CLI
settings, without Claude's model router or environment variables.

Codex roles are in `.codex/agents/` and shared skills are in `.agents/skills/`.
This repository does not install a Codex enforcement hook. Claude hook names
and `$CLAUDE_PROJECT_DIR` are not a Codex adapter. In Codex, follow the protocol
as instructions and use the actual harness permissions; do not claim that its
`[mechanical]` rules are enforced by these repository files.
Claude's settings and OpenCode's plugin remain separate adapters; their presence
alone does not prove that a running session loaded or enforced them.
