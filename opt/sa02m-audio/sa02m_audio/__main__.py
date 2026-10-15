"""`python3 -m sa02m_audio` — the daemon entry (sa02m-audio.service)."""

import sys

from .service import main

sys.exit(main(sys.argv[1:]))
