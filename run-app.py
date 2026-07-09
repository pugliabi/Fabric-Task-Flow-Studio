#!/usr/bin/env python3
"""Launch the Fabric Task Flows Studio web app.

    python run-app.py [--host 127.0.0.1] [--port 8000] [--no-reload]

Ensures fastapi + uvicorn are present (installs into the active environment if
not), then serves the chat UI at http://127.0.0.1:8000.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from threading import Timer

# Windows consoles default to cp1252, which can't encode box/arrow glyphs and
# silently corrupts Unicode in files the agent writes. Force UTF-8 everywhere.
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent
APP_DIR = REPO_ROOT / "app"

REQUIRED = ["fastapi", "uvicorn"]


def _ensure_deps() -> None:
    missing = [pkg for pkg in REQUIRED if importlib.util.find_spec(pkg) is None]
    if not missing:
        return
    print(f"Installing missing dependencies: {', '.join(missing)} …")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "fastapi", "uvicorn[standard]"]
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Fabric Task Flows Studio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-reload", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    _ensure_deps()

    # Make `app` importable as a top-level package for uvicorn.
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(APP_DIR))

    import uvicorn

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Fabric Task Flows Studio -> {url}\n")
    if not args.no_browser:
        Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
        reload_dirs=[str(APP_DIR)],
        app_dir=str(APP_DIR),
    )


if __name__ == "__main__":
    main()
