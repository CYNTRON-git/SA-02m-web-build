#!/usr/bin/env bash
# Quality row `shellcheck` (see .ai-dev/quality/tools.json). Runs shellcheck at
# severity=error (real bugs only — style/info/warning are intentionally not gated)
# over every Bash CGI, shared lib, device script, and the installer. Lives in a
# script (not inline in the JSON `run`) so the command carries no cmd.exe
# metacharacters (`{ } [ ] | > 2>&1`) — execSync on Windows wraps `run` in
# cmd.exe, which would mangle those; a bare `bash <path>` invocation is safe on
# both git-bash/Windows and Linux/CI.
# COMMENT-BLINDNESS AUDIT (1.0.6.24): N/A — this row runs shellcheck over the
# Bash surface. It asserts no needle of its own, so there is no pin a comment
# could satisfy.
# comment-mutation-proof-exempt: runner, no source-line needle — shellcheck over the whole Bash surface asserts no pin a comment could satisfy.
set -u

if ! command -v shellcheck >/dev/null 2>&1; then
  echo "shellcheck: skipped (not installed here; CI/Linux runs it)"
  exit 0
fi

# Scope = every Bash surface in the repo: CGI endpoints and libs, device
# scripts, the installer, the build/imaging tools, the dev harnesses, the
# on-device sbin helpers, and the quality checks themselves. Rationale for the
# wide net: the manifest donor-field shift shipped into a release artifact
# through the one surface (tools/) no row linted — an unlinted Bash file is a
# proven escape path, not a hypothetical one.
exec shellcheck --severity=error --shell=bash \
  www/network_config/cgi-bin/*.cgi \
  www/network_config/cgi-bin/lib_*.sh \
  etc/*.sh \
  scripts/*.sh \
  scripts/dev/*.sh \
  tools/*/*.sh \
  usr/local/sbin/*.sh \
  .ai-dev/quality/checks/*.sh \
  install.sh
