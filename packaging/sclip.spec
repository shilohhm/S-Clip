# PyInstaller build definition for S-Clip.
#
# Build from the repository root:
#
#     python -m PyInstaller packaging/sclip.spec --noconfirm
#
# A one-directory build rather than one-file, on purpose. A one-file build
# unpacks itself to a temporary directory on every launch, which costs a second
# or two of startup and — more importantly here — moves the application's own
# location somewhere unpredictable. S-Clip resolves its bundled assets and looks
# for a neighbouring FFmpeg relative to where it lives, so a stable install
# directory is worth more than a single tidy file. The installer hides the
# directory from the user anyway.

from pathlib import Path

from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.hooks import collect_submodules

_ROOT = Path(SPECPATH).resolve().parent
_SRC = _ROOT / "src"
_ASSETS = _SRC / "sclip" / "ui" / "assets"

# Assets are placed under ``sclip/ui/...`` because the application finds them
# relative to its own module files: ``theme`` reads ``styles.qss`` from beside
# itself and ``fonts`` reads ``assets/fonts``. Mirroring the source layout means
# no frozen-specific path handling is needed anywhere in the application.
datas = [
    (str(_SRC / "sclip" / "ui" / "styles.qss"), "sclip/ui"),
]

# The asset directory goes through ``Tree`` rather than a plain directory tuple
# so byte-code caches and the ``icons`` module do not ship as loose files:
# ``icons.py`` is a real module already inside the frozen archive, and a second
# copy on disk beside the fonts would only invite confusion about which one
# runs. A ``Tree`` yields three-element entries, so it is handed to ``COLLECT``
# rather than to ``Analysis``, which expects plain pairs.
asset_tree = Tree(
    str(_ASSETS),
    prefix="sclip/ui/assets",
    excludes=["__pycache__", "*.py"],
)

# These are reached through runtime dispatch rather than a plain import, so the
# dependency analyser cannot see them:
#   * pynput picks its keyboard backend by platform at import time,
#   * screeninfo picks a display enumerator the same way,
#   * PyAudioWPatch is imported lazily by the desktop-audio pump.
hiddenimports = [
    *collect_submodules("pynput"),
    *collect_submodules("screeninfo"),
    "pyaudiowpatch",
]

# Qt ships far more than this application uses. Dropping the large optional
# modules keeps the install a sensible size; none of them are imported by
# S-Clip or by PySide6's own core widgets.
excludes = [
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNfc",
    "PySide6.QtOpenGL",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    # Development-only dependencies that must never reach a user's machine.
    "pytest",
    "mypy",
    "ruff",
    "tkinter",
]

analysis = Analysis(
    [str(_ROOT / "main.pyw")],
    pathex=[str(_SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="S-Clip",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed binaries are a reliable way to get flagged by AV
    console=False,  # a capture tool must not park a console window on screen
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_ASSETS / "icon.ico"),
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    asset_tree,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="S-Clip",
)
