#!/usr/bin/env python3
"""macOS entry point for the shared lightweight EPU Mapper launcher."""

import os
import sys

# The runtime lives inside a signed application bundle. Prevent this process
# and every review-server child process from writing __pycache__ into it.
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

from windows_gui_launcher import main


if __name__ == "__main__":
    main()
