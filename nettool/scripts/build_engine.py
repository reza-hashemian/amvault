#!/usr/bin/env python3
"""Build the engine into one self-contained program for the installers.

    python scripts/build_engine.py

Writes `src-tauri/binaries/nettool-engine-<target>[.exe]`, the name Tauri
expects for a program it ships beside the app.  Run it with a Python that has
the engine's requirements and PyInstaller installed.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def target_triple() -> str:
    for line in subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("could not read the target from `rustc -vV`")


def main() -> int:
    name = f"nettool-engine-{target_triple()}"
    data = os.path.join(ROOT, "nettool_engine", "demo_devices.json")
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--onefile", "--noconfirm", "--clean",
            "--name", name,
            "--distpath", os.path.join(ROOT, "src-tauri", "binaries"),
            "--workpath", os.path.join(ROOT, "build", "pyinstaller"),
            "--specpath", os.path.join(ROOT, "build"),
            # os.pathsep-free form: PyInstaller 6 accepts SRC:DEST everywhere.
            "--add-data", f"{data}:nettool_engine",
            os.path.join(ROOT, "engine_main.py"),
        ],
        check=True, cwd=ROOT,
    )
    print(f"built src-tauri/binaries/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
