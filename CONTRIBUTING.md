# Contributing to S-Clip

Thanks for helping improve S-Clip. The project favours small, reviewable changes
with clear tests over broad rewrites.

## Development setup

S-Clip targets Windows 10/11 and Python 3.10 or newer.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Install FFmpeg 4.0 or newer and ensure `ffmpeg` is available on `PATH` before
running capture integration tests.

## Quality gates

Run these before opening a pull request:

```powershell
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python -m pytest -m "not slow"
```

Run `python -m pytest` as well when changing FFmpeg invocation, capture
lifecycle, or replay-buffer behavior.

## Architecture boundaries

- `sclip.contracts` contains framework-neutral data and protocols.
- `sclip.core` owns capture, devices, settings, and FFmpeg. It must not import Qt.
- `sclip.ui` depends on contracts rather than concrete core implementations.
- `sclip.app` is the composition root.

See [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) before changing threading
or dependency direction.

## Pull requests

Describe the user-visible effect, the tradeoffs you considered, and how you
verified the change. Include a screenshot for visual changes and add a focused
regression test for bug fixes.

To regenerate the README product images after a UI change:

```powershell
python scripts/render_readme_screenshot.py
```

This writes all four page screenshots into `docs/assets/`. It encodes its own
sample recordings with FFmpeg, so keep FFmpeg on `PATH`; without it the render
still succeeds but shows the empty library state. Do not hand-edit the images
or feed the renderer data the application could not itself produce — the point
of the script is that the README shows the app as it really is.
