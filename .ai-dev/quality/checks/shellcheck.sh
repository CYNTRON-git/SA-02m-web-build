#!/usr/bin/env bash
# Quality row `shellcheck` (see .ai-dev/quality/tools.json). Runs shellcheck at
# severity=error (real bugs only — style/info/warning are intentionally not gated)
# over every Bash CGI, shared lib, device script, and the installer. Lives in a
# script (not inline in the JSON `run`) so the command carries no cmd.exe
# metacharacters (`{ } [ ] | > 2>&1`) — execSync on Windows wraps `run` in
# cmd.exe, which would mangle those; a bare `bash <path>` invocation is safe on
# both git-bash/Windows and Linux/CI.
set -u

if ! command -v shellcheck >/dev/null 2>&1; then
  echo "shellcheck: skipped (not installed here; CI/Linux runs it)"
  exit 0
fi

exec shellcheck --severity=error --shell=bash \
  www/network_config/cgi-bin/*.cgi \
  www/network_config/cgi-bin/lib_*.sh \
  etc/*.sh \
  scripts/*.sh \
  install.sh
