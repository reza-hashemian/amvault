"""Entry point for the packaged engine.

In development the engine runs as `python -m nettool_engine`.  Installers
cannot assume a Python is present, so for them PyInstaller turns this file
into a self-contained `nettool-engine` program that sits beside the app.
"""

import sys

from nettool_engine.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
