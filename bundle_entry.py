#!/usr/bin/env python3
"""
LSI RAID Monitor — PyInstaller entry point.

This script is used by PyInstaller to produce a single executable file.
When run on another machine, it starts the Flask web UI and stores data
next to the executable.
"""

import os
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    # Running inside a PyInstaller bundle
    exe_dir = Path(sys.executable).parent
else:
    # Running from source during development
    exe_dir = Path(__file__).resolve().parent

# Persist data/charts next to the executable, not in PyInstaller's temp dir
os.environ.setdefault("LSI_DATA_DIR", str(exe_dir / "data"))
os.environ.setdefault("STORCLI_PATH", str(exe_dir / "storcli64"))

# Ensure the bundled project root is on sys.path so we can import lsi_report
sys.path.insert(0, str(exe_dir))

# Create runtime directories
Path(os.environ["LSI_DATA_DIR"]).mkdir(parents=True, exist_ok=True)

from web.app import app  # noqa: E402


if __name__ == "__main__":
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "5200"))
    print(f"LSI RAID Monitor starting at http://{host}:{port}")
    print(f"Data directory: {os.environ.get('LSI_DATA_DIR')}")
    print(f"storcli64 path: {os.environ.get('STORCLI_PATH')}")
    app.run(host=host, port=port, debug=False)
