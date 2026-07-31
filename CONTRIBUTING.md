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
still succeeds but shows the empty library state.

The animated demo has its own script, which needs FFmpeg:

```powershell
python scripts/render_demo_gif.py
```

Do not hand-edit either output, and do not feed the renderers data the
application could not itself produce - the point of both scripts is that the
README shows the app as it really is. In particular, do not swap the scripted
engine for a live capture: that would put whatever was on your screen into a
public repository.

## Packaging

The Windows installer is built in two steps - PyInstaller freezes the app, Inno
Setup wraps it - both driven by one script:

```powershell
winget install JRSoftware.InnoSetup
python scripts/build_windows_installer.py
```

Output lands in `build/installer`. `--freeze-only` and `--installer-only` run
the halves separately, and `--refresh-icon` regenerates `icon.ico` from the
source SVG.

Releases are cut by tagging; [`release.yml`](./.github/workflows/release.yml)
does the rest, and refuses to publish if the tag disagrees with
`src/sclip/version.py`.

Two things to check by hand after any packaging change, because neither is
covered by the test suite:

- **Launch the frozen build**, not just the source one. Missing assets and
  hidden imports only fail once frozen.
- **Uninstall while S-Clip is running.** It is a tray app, so that is the normal
  case rather than the edge case, and it is where the installer has already been
  wrong twice: once leaving orphaned files behind, once aborting the uninstall
  entirely.
