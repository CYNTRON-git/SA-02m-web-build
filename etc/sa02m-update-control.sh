#!/bin/sh
# Fixed root entrypoint; sudo env_reset supplies a clean environment.
exec /usr/bin/python3 -I -c 'import sys; sys.path.insert(0,"/opt/sa02m-update"); from lib.control import main; sys.exit(main())' "$@"
