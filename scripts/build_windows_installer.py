"""Build the S-Clip Windows installer.

Run from the repository root:

    python scripts/build_windows_installer.py

Two steps. PyInstaller freezes the application into ``build/dist/S-Clip``, then
Inno Setup wraps that directory into a single setup executable in
``build/installer``. Either step can be run on its own; see ``--help``.

Requirements beyond the dev extra: Inno Setup 6, which is not a Python package.
Install it with ``winget install JRSoftware.InnoSetup`` or from jrsoftware.org.

The resulting installer is **unsigned**. Signing requires a code-signing
certificate issued against a verified identity, which this project does not
have; the Inno Setup script carries a commented ``SignTool`` line ready for one.
Until then Windows SmartScreen will warn on first run, and the README says so
rather than leaving people to be surprised by it.
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from sclip.version import __version__  # noqa: E402

_SPEC = _ROOT / "packaging" / "sclip.spec"
_ISS = _ROOT / "packaging" / "sclip.iss"
_ASSETS = _ROOT / "src" / "sclip" / "ui" / "assets"
_ICON_SVG = _ASSETS / "icon.svg"
_ICON_ICO = _ASSETS / "icon.ico"

_DIST = _ROOT / "build" / "dist"
_WORK = _ROOT / "build" / "work"
_INSTALLER_OUT = _ROOT / "build" / "installer"
_FROZEN_DIR = _DIST / "S-Clip"

# Windows shells icons at these sizes; shipping all of them stops the taskbar
# and Alt-Tab from rescaling a single bitmap and making the logo look muddy.
_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

# Roots to search for the Inno Setup compiler. The per-user Programs directory
# matters: `winget install JRSoftware.InnoSetup` installs there by default, not
# under Program Files, and ISCC is not added to PATH either way.
_ISCC_ROOTS = (
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")),
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
)


def refresh_icon() -> Path:
    """Regenerate ``icon.ico`` from the source SVG.

    Qt can write a single-image ICO but not a multi-resolution one, so the
    container is assembled here: each size is rendered and PNG-encoded, then
    wrapped in an ICONDIR. PNG-compressed entries are the Vista-and-later form
    and keep the file a fraction of the size of raw bitmaps.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    renderer = QSvgRenderer(str(_ICON_SVG))
    if not renderer.isValid():
        raise SystemExit(f"Could not parse {_ICON_SVG}")

    blobs: list[tuple[int, bytes]] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        for size in _ICON_SIZES:
            image = QImage(size, size, QImage.Format.Format_ARGB32)
            image.fill(0)
            painter = QPainter()
            if not painter.begin(image):
                raise SystemExit(f"Could not begin painting at {size}px")
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            renderer.render(painter)
            painter.end()
            png = Path(temp_dir) / f"{size}.png"
            if not image.save(str(png), "PNG"):
                raise SystemExit(f"Could not encode the {size}px icon")
            blobs.append((size, png.read_bytes()))

    payload = bytearray(struct.pack("<HHH", 0, 1, len(blobs)))
    offset = 6 + 16 * len(blobs)
    for size, blob in blobs:
        # A 256px entry is recorded as 0; the field is a single byte.
        dimension = 0 if size >= 256 else size
        payload += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for _size, blob in blobs:
        payload += blob

    _ICON_ICO.write_bytes(bytes(payload))
    print(f"  icon: {_ICON_ICO.relative_to(_ROOT)} ({len(payload)} bytes, {len(blobs)} sizes)")
    _ = app  # keep the application object alive until rendering is finished
    return _ICON_ICO


def freeze() -> Path:
    """Run PyInstaller and return the frozen application directory."""
    if not _ICON_ICO.is_file():
        print("icon.ico missing; generating it")
        refresh_icon()

    print("Freezing the application...")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(_SPEC),
            "--noconfirm",
            "--distpath",
            str(_DIST),
            "--workpath",
            str(_WORK),
        ],
        cwd=_ROOT,
        check=True,
    )
    if not (_FROZEN_DIR / "S-Clip.exe").is_file():
        raise SystemExit(f"PyInstaller finished but {_FROZEN_DIR / 'S-Clip.exe'} is missing")
    print(f"  frozen: {_FROZEN_DIR.relative_to(_ROOT)}")
    return _FROZEN_DIR


def find_iscc() -> Path:
    """Locate the Inno Setup compiler, or explain how to get it."""
    from shutil import which

    on_path = which("ISCC")
    if on_path is not None:
        return Path(on_path)

    # Newest major version first, so a machine with several installs uses the
    # most recent compiler rather than whichever sorts first alphabetically.
    for root in _ISCC_ROOTS:
        if not root.is_dir():
            continue
        for directory in sorted(root.glob("Inno Setup *"), reverse=True):
            candidate = directory / "ISCC.exe"
            if candidate.is_file():
                return candidate

    raise SystemExit(
        "Inno Setup 6 was not found. Install it with:\n"
        "    winget install JRSoftware.InnoSetup\n"
        "or download it from https://jrsoftware.org/isdl.php"
    )


def build_installer() -> Path:
    """Compile the installer and return the written setup executable."""
    if not (_FROZEN_DIR / "S-Clip.exe").is_file():
        raise SystemExit(f"No frozen build at {_FROZEN_DIR}. Run without --installer-only first.")

    iscc = find_iscc()
    _INSTALLER_OUT.mkdir(parents=True, exist_ok=True)
    print(f"Compiling the installer with {iscc}...")
    subprocess.run(
        [
            str(iscc),
            f"/DAppVersion={__version__}",
            f"/DSourceDir={_FROZEN_DIR}",
            f"/DOutputDir={_INSTALLER_OUT}",
            str(_ISS),
        ],
        cwd=_ROOT,
        check=True,
    )

    expected = _INSTALLER_OUT / f"S-Clip-{__version__}-windows-x64-setup.exe"
    if not expected.is_file():
        raise SystemExit(f"Inno Setup finished but {expected} is missing")
    size_mb = expected.stat().st_size / (1024 * 1024)
    print(f"  installer: {expected.relative_to(_ROOT)} ({size_mb:.1f} MB)")
    return expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh-icon",
        action="store_true",
        help="regenerate icon.ico from icon.svg before building",
    )
    parser.add_argument(
        "--freeze-only",
        action="store_true",
        help="run PyInstaller but do not build the installer",
    )
    parser.add_argument(
        "--installer-only",
        action="store_true",
        help="build the installer from an existing frozen build",
    )
    args = parser.parse_args()

    print(f"S-Clip {__version__}")
    if args.refresh_icon:
        refresh_icon()
    if not args.installer_only:
        freeze()
    if not args.freeze_only:
        build_installer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
