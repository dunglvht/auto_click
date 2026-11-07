#!/usr/bin/env python3
"""Start Firefox Tab Auto Clicker."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import websocket  # noqa: F401
except ImportError:
    sys.exit("Missing dependency: run  pip install -r requirements.txt  (needs websocket-client)")

from tab_autoclicker.gui import main  # noqa: E402

if __name__ == "__main__":
    main()
