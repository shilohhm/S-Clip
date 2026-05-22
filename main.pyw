"""Windowed launcher for S-Clip.

Adds ``src`` to ``sys.path`` so the app can be run from a checkout without
``pip install -e .``. When installed normally, use ``python -m sclip``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))

from sclip.app import main

if __name__ == "__main__":
    raise SystemExit(main())
