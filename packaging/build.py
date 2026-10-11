#!/usr/bin/env python3
"""Freeze amvault into a self-contained application folder.

    python packaging/build.py

Uses PyInstaller, with the Python that has amvault's requirements installed.
The result is `dist/amvault/` (Linux, Windows) or `dist/amvault.app` (macOS):
the app together with its own Python and Qt, so the machine it is installed on
needs neither.  The per-platform installers are made from that folder.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            # No console window behind the app on Windows; a real .app on macOS.
            "--windowed",
            "--name", "amvault",
            # PyInstaller converts this to .ico / .icns itself when Pillow is installed.
            "--icon", os.path.join(ROOT, "packaging", "icon.png"),
            "--osx-bundle-identifier", "io.github.reza-hashemian.amvault",
            # pykeepass finds some of its parsers at run time.
            "--collect-submodules", "pykeepass",
            "--distpath", os.path.join(ROOT, "dist"),
            "--workpath", os.path.join(ROOT, "build", "pyinstaller"),
            "--specpath", os.path.join(ROOT, "build"),
            os.path.join(ROOT, "amvault-gui.py"),
        ],
        check=True, cwd=ROOT,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
