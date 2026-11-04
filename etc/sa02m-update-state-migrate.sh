#!/bin/sh
# Installer-only root operation; deliberately absent from sudoers.
root=$(CDPATH= cd -- "$(dirname -- "$0")/../opt/sa02m-update" && pwd) || exit 1
exec /usr/bin/python3 -I -c 'import sys; sys.path.insert(0,sys.argv[1]); from lib.state_security import migrate; migrate()' "$root"
