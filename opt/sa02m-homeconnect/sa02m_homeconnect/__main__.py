"""`python3 -m sa02m_homeconnect` — the daemon entry (sa02m-homeconnect.service)."""

import sys

from .main import main

sys.exit(main(sys.argv[1:]))
