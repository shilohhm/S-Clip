"""Allow ``python -m sclip`` to launch the app.

A package-level entry point keeps the standard Python invocation working
without depending on the console-script installer. ``python -m sclip`` does
the same thing as the installed ``sclip`` command.
"""

from __future__ import annotations

from sclip.app import main

if __name__ == "__main__":
    raise SystemExit(main())
