"""Library page -- the clip browser.

Lays every recording in the clips directory out as a responsive grid of
thumbnail tiles. Thumbnails are generated on demand by FFmpeg on a worker
thread so the GUI never stalls; each one is cached alongside its clip as
``<clip>.mp4.thumb.jpg`` so we only pay the cost once per file.

The page is self-contained. It reads :func:`app_paths().clips_dir` directly
and watches it with ``QFileSystemWatcher``, so a clip written by the capture
engine (or dropped in by external tooling) appears without a manual refresh.

The layout is deliberately flat: a fixed header row, then a scroll area whose
content widget holds the tile grid. The grid re-flows its column count in
``resizeEvent`` so the tiles stay a comfortable width as the window resizes.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QFileSystemWatcher,
    QObject,
    QPoint,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices, QFontMetrics, QMouseEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sclip.paths import app_paths
from sclip.ui.theme import (
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XL,
    SPACING_XS,
    SPACING_XXS,
)
from sclip.ui.widgets import IconButton

# FFmpeg discovery is best-effort -- the library still lists clips without it,
# we just fall back to a "No preview" placeholder for the thumbnails.
try:  # pragma: no cover - exercised only when FFmpeg cannot be imported
    from sclip.core.ffmpeg import ffprobe_path, find_ffmpeg, popen_kwargs
except ImportError:  # pragma: no cover
    find_ffmpeg = None  # type: ignore[assignment]
    ffprobe_path = None  # type: ignore[assignment]
    popen_kwargs = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


# -- Tile geometry ---------------------------------------------------------
# A tile is roughly _TILE_WIDTH wide; the thumbnail keeps a 16:9 ratio. These
# are starting hints, not hard contracts -- the grid reads them to decide how
# many columns fit, and the thumbnail label is fixed to the size below.
_TILE_WIDTH: int = 260
_THUMB_WIDTH: int = 232
_THUMB_HEIGHT: int = 130  # 16:9-ish of the width above

# Each column needs the tile width plus the grid's horizontal spacing; 280 is
# that budget rounded up, so ``available_width // 280`` lands on a sensible
# count (3-4 columns at the default 1160px window).
_COLUMN_BUDGET: int = 280

# Debounce window for filesystem-watcher bursts. A new recording can trigger
# several directoryChanged events in quick succession; 400 ms folds them into
# a single rebuild without being perceptibly laggy.
_REFRESH_DEBOUNCE_MS: int = 400

# FFmpeg writes a thumbnail no wider than this; -1 keeps the aspect ratio.
_THUMB_SCALE_WIDTH: int = 320


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------


def _format_size(num_bytes: int) -> str:
    """Render a byte count in a friendly unit, e.g. ``"1.2 MB"``.

    The previous form divided ``value`` *before* the ``TB`` branch fired, so a
    file ≥ 1 TB was reported in the wrong order of magnitude. This version
    only divides while there is still a larger unit to fall through to.
    """
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def _format_duration(seconds: float) -> str:
    """Render seconds as ``m:ss`` (or ``h:mm:ss`` for long clips)."""
    if seconds <= 0:
        return ""
    total = int(seconds + 0.5)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_date(timestamp: float) -> str:
    """Render an mtime as a compact human date, e.g. ``"20 May 2026"``."""
    # %d is zero-padded across platforms; lstrip keeps the day un-padded
    # without relying on the non-portable %-d / %#d strftime extensions.
    return datetime.fromtimestamp(timestamp).strftime("%d %b %Y").lstrip("0")


def _thumb_path_for(clip: Path) -> Path:
    """Canonical cache location for a clip's thumbnail."""
    return clip.with_suffix(clip.suffix + ".thumb.jpg")


# ---------------------------------------------------------------------------
# Worker plumbing -- all FFmpeg/ffprobe work happens off the GUI thread
# ---------------------------------------------------------------------------


class _ThumbnailSignals(QObject):
    """Result carrier for a thumbnail worker.

    A ``QRunnable`` cannot emit signals itself, so it is handed this companion
    object. The clip path travels as a string because Qt's auto-conversion of
    :class:`pathlib.Path` across the thread boundary is not consistent.
    """

    ready = Signal(str)  # clip path -- the cached thumbnail now exists
    failed = Signal(str)  # clip path -- no thumbnail could be produced


class _ThumbnailWorker(QRunnable):
    """Spawn FFmpeg once to produce a thumbnail next to its clip."""

    def __init__(self, clip: Path, thumb: Path, ffmpeg: Path | None) -> None:
        super().__init__()
        self._clip = clip
        self._thumb = thumb
        self._ffmpeg = ffmpeg
        self.signals = _ThumbnailSignals()

    def run(self) -> None:  # pragma: no cover - exercised at runtime only
        # Without FFmpeg there is nothing to do; report failure so the tile
        # settles on its "No preview" placeholder rather than spinning.
        if self._ffmpeg is None:
            self.signals.failed.emit(str(self._clip))
            return

        argv = [
            str(self._ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "1",
            "-i",
            str(self._clip),
            "-frames:v",
            "1",
            "-vf",
            f"scale={_THUMB_SCALE_WIDTH}:-1",
            "-y",
            str(self._thumb),
        ]
        try:
            kwargs = popen_kwargs() if popen_kwargs is not None else {}
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20.0,
                check=False,
                **kwargs,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Thumbnail generation failed for %s: %s", self._clip, exc)
            self.signals.failed.emit(str(self._clip))
            return

        if result.returncode != 0 or not self._thumb.is_file():
            logger.debug(
                "FFmpeg thumbnail failed for %s (rc=%s): %s",
                self._clip,
                result.returncode,
                result.stderr.strip()[-200:],
            )
            self.signals.failed.emit(str(self._clip))
            return

        self.signals.ready.emit(str(self._clip))


class _DurationSignals(QObject):
    """Result carrier for a duration probe worker."""

    ready = Signal(str, float)  # clip path, duration in seconds


class _DurationWorker(QRunnable):
    """Probe a clip's duration with ffprobe, off the GUI thread.

    ffprobe prints just the duration in seconds with the flags below. If
    ffprobe is unavailable we fall back to parsing ``ffmpeg -i`` output, which
    every distribution can do -- portable Windows builds occasionally ship
    ffmpeg without ffprobe beside it.
    """

    _FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")

    def __init__(self, clip: Path, ffprobe: Path | None, ffmpeg: Path | None) -> None:
        super().__init__()
        self._clip = clip
        self._ffprobe = ffprobe
        self._ffmpeg = ffmpeg
        self.signals = _DurationSignals()

    def run(self) -> None:  # pragma: no cover - exercised at runtime only
        seconds = self._probe_with_ffprobe()
        if seconds is None:
            seconds = self._probe_with_ffmpeg()
        # Zero is a harmless sentinel: the tile simply omits the duration.
        self.signals.ready.emit(str(self._clip), seconds or 0.0)

    def _probe_with_ffprobe(self) -> float | None:
        if self._ffprobe is None:
            return None
        argv = [
            str(self._ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(self._clip),
        ]
        text = self._run(argv, timeout=10.0)
        if text is None:
            return None
        try:
            return float(text.strip())
        except ValueError:
            return None

    def _probe_with_ffmpeg(self) -> float | None:
        if self._ffmpeg is None:
            return None
        text = self._run(
            [str(self._ffmpeg), "-hide_banner", "-i", str(self._clip)],
            timeout=10.0,
        )
        if text is None:
            return None
        match = self._FFMPEG_DURATION_RE.search(text)
        if match is None:
            return None
        hours, minutes, secs = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(secs)

    def _run(self, argv: list[str], *, timeout: float) -> str | None:
        """Run a probe command, returning combined output or ``None`` on error."""
        try:
            kwargs = popen_kwargs() if popen_kwargs is not None else {}
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                **kwargs,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("Duration probe failed for %s: %s", self._clip, exc)
            return None
        # ffmpeg prints the duration on stderr; ffprobe on stdout.
        return (result.stdout or "") + (result.stderr or "")


# ---------------------------------------------------------------------------
# Clip tile
# ---------------------------------------------------------------------------


class _ClipTile(QFrame):
    """One thumbnail card in the library grid.

    The tile is a self-contained presentational widget: it owns its thumbnail,
    name and caption labels, and emits high-level intent signals (selected,
    activated, context-menu requested). The page wires those signals to the
    filesystem actions -- the tile never touches disk itself.
    """

    selected = Signal(Path)
    activated = Signal(Path)
    context_requested = Signal(Path, object)  # clip path, global QPoint

    def __init__(self, clip: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._clip = clip
        self.setObjectName("ClipTile")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(_TILE_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._emit_context_request)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING_XS, SPACING_XS, SPACING_XS, SPACING_XS)
        layout.setSpacing(SPACING_XS)

        # -- Thumbnail -----------------------------------------------------
        self._thumb = QLabel(self)
        self._thumb.setObjectName("ClipThumb")
        self._thumb.setFixedSize(QSize(_THUMB_WIDTH, _THUMB_HEIGHT))
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setText("Generating preview…")
        layout.addWidget(self._thumb)

        # -- File name (elided to the tile width) --------------------------
        self._name = QLabel(self)
        self._name.setProperty("role", "value")
        self._name.setToolTip(clip.name)
        layout.addWidget(self._name)

        # -- Caption: duration, size, date ---------------------------------
        self._caption = QLabel(self)
        self._caption.setProperty("role", "muted")
        layout.addWidget(self._caption)

        self._duration_seconds: float = 0.0
        self._populate_static_caption()
        self._update_name_label()

    # -- Properties --------------------------------------------------------

    @property
    def clip(self) -> Path:
        return self._clip

    # -- Static metadata ---------------------------------------------------

    def _populate_static_caption(self) -> None:
        """Fill the caption with the bits readable without FFmpeg."""
        try:
            stat = self._clip.stat()
        except OSError:
            self._size_text = ""
            self._date_text = ""
        else:
            self._size_text = _format_size(stat.st_size)
            self._date_text = _format_date(stat.st_mtime)
        self._refresh_caption()

    def _refresh_caption(self) -> None:
        # A middle dot with hair spaces reads as a calm separator; empty parts
        # (e.g. a missing duration) are dropped so we never show a stray dot.
        parts = [
            text
            for text in (
                _format_duration(self._duration_seconds),
                self._size_text,
                self._date_text,
            )
            if text
        ]
        self._caption.setText("  ·  ".join(parts))

    def _update_name_label(self) -> None:
        """Elide the file name to the label's current width."""
        metrics = QFontMetrics(self._name.font())
        # The available width is the tile interior; fall back to the thumbnail
        # width before the first layout pass has given the label a real size.
        available = self._name.width() or _THUMB_WIDTH
        self._name.setText(
            metrics.elidedText(self._clip.name, Qt.TextElideMode.ElideMiddle, available)
        )

    # -- Updates from workers ---------------------------------------------

    def apply_thumbnail(self, pixmap: QPixmap) -> None:
        """Show ``pixmap`` as the thumbnail, or a placeholder if it is null."""
        if pixmap.isNull():
            self._thumb.setText("No preview")
            return
        # KeepAspectRatioByExpanding fills the whole frame; the label clips the
        # overflow, so a 16:9 frame never shows letterboxing bars.
        scaled = pixmap.scaled(
            self._thumb.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumb.setPixmap(scaled)

    def show_no_preview(self) -> None:
        """Settle the thumbnail on its placeholder after a failed job."""
        if self._thumb.pixmap() is None or self._thumb.pixmap().isNull():
            self._thumb.setText("No preview")

    def apply_duration(self, seconds: float) -> None:
        self._duration_seconds = seconds
        self._refresh_caption()

    def set_selected(self, selected: bool) -> None:
        """Toggle the violet selection outline via the QSS dynamic property."""
        self.setProperty("selected", "true" if selected else "false")
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)

    # -- Qt event overrides ------------------------------------------------

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        # The name label width tracks the tile, so re-elide on every resize.
        self._update_name_label()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self._clip)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self._clip)
        super().mouseDoubleClickEvent(event)

    def _emit_context_request(self, pos: QPoint) -> None:
        # customContextMenuRequested hands a widget-local point; the menu wants
        # a global one, so map it before forwarding.
        self.context_requested.emit(self._clip, self.mapToGlobal(pos))


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


class LibraryPage(QWidget):
    """Browse, open, rename, reveal and delete recorded clips."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._clips_dir: Path = app_paths().clips_dir

        # Background workers. Thumbnail generation is cheap individually but
        # bursty when the page first opens, so a small pool keeps the GUI
        # responsive without spawning an unbounded number of FFmpeg processes.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(2, min(4, self._pool.maxThreadCount())))

        # FFmpeg/ffprobe locations resolved once -- shelling the disk per
        # worker would be wasteful and slow.
        self._ffmpeg: Path | None = self._resolve_ffmpeg()
        self._ffprobe: Path | None = self._resolve_ffprobe()

        # Tile lookup keyed by absolute clip-path string. Strings, not Path
        # objects, because the worker signals carry strings across the thread
        # boundary and a string key makes the slot lookup trivial.
        self._tiles: dict[str, _ClipTile] = {}
        self._selected_clip: Path | None = None

        # Current grid column count; -1 forces the first refresh to lay out.
        self._columns: int = -1

        # Debounce timer for filesystem-watcher bursts, created on the GUI
        # thread so its timeout fires there.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)

        self._build_ui()

        # Watch the clips directory so externally written clips appear without
        # a manual refresh. The directory may not exist on first launch.
        try:
            self._clips_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create clips dir %s: %s", self._clips_dir, exc)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_directory_changed)
        self._watcher.addPath(str(self._clips_dir))

        # Defer the first population so the window gets paint priority.
        QTimer.singleShot(0, self.refresh)

    # ---------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        # Page root: a fixed header, then the scroll area. Zero margins -- the
        # header and the scroll content supply their own padding.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        # -- Scroll area holding the grid ----------------------------------
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("LibraryContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(SPACING_XL, SPACING_XL, SPACING_XL, SPACING_XL)
        content_layout.setSpacing(SPACING_LG)

        # The grid lives in its own widget so we can recompute its columns
        # without disturbing the empty-state panel or the trailing stretch.
        self._grid_host = QWidget(content)
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(SPACING_MD)
        self._grid.setVerticalSpacing(SPACING_MD)
        # Tiles are left-aligned so a partial final row hugs the left edge
        # rather than floating in the centre of the page.
        self._grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        content_layout.addWidget(self._grid_host)

        # Empty-state panel, shown instead of the grid when there are no clips.
        self._empty_panel = self._build_empty_panel(content)
        content_layout.addWidget(self._empty_panel)

        content_layout.addStretch(1)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def _build_header(self) -> QWidget:
        """The fixed title row sitting above the scroll area."""
        header = QWidget(self)
        header.setObjectName("LibraryHeader")
        layout = QHBoxLayout(header)
        # The header carries the page's top/side padding; the scroll content
        # below it adds its own, so the header omits a bottom margin.
        layout.setContentsMargins(SPACING_XL, SPACING_XL, SPACING_XL, SPACING_SM)
        layout.setSpacing(SPACING_SM)

        title = QLabel("Library", header)
        title.setProperty("role", "display")
        layout.addWidget(title)

        layout.addStretch(1)

        open_folder = IconButton(text="Open folder", role="ghost", parent=header)
        open_folder.clicked.connect(self._open_clips_folder)
        layout.addWidget(open_folder)

        refresh = IconButton(text="Refresh", role="ghost", parent=header)
        refresh.clicked.connect(self.refresh)
        layout.addWidget(refresh)

        return header

    def _build_empty_panel(self, parent: QWidget) -> QFrame:
        """A centred friendly panel shown when the clips folder is empty."""
        panel = QFrame(parent)
        panel.setObjectName("Inset")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        panel.setVisible(False)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(SPACING_XL, SPACING_XL, SPACING_XL, SPACING_XL)
        layout.setSpacing(SPACING_XXS)

        message = QLabel("No clips yet — your saved clips will appear here.", panel)
        message.setProperty("role", "muted")
        message.setWordWrap(True)
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(message)

        return panel

    # ------------------------------------------------------- Public API

    def refresh(self) -> None:
        """Re-scan the clips folder and rebuild the tile grid."""
        clips = self._discover_clips()
        new_paths = {str(clip) for clip in clips}

        # Drop tiles for clips that have gone away.
        for stale in set(self._tiles) - new_paths:
            tile = self._tiles.pop(stale, None)
            if tile is not None:
                tile.setParent(None)
                tile.deleteLater()

        # Empty folder: hide the grid, show the friendly panel, and stop.
        if not clips:
            self._grid_host.setVisible(False)
            self._empty_panel.setVisible(True)
            self._clear_selection_if_gone(new_paths)
            return

        self._empty_panel.setVisible(False)
        self._grid_host.setVisible(True)

        # Create any tiles we have not seen before and kick off their workers.
        ordered: list[_ClipTile] = []
        for clip in clips:
            key = str(clip)
            tile = self._tiles.get(key)
            if tile is None:
                tile = self._create_tile(clip)
                self._tiles[key] = tile
            ordered.append(tile)

        # -1 forces the (re)flow; otherwise reuse the already-computed count.
        columns = self._columns if self._columns > 0 else self._column_count()
        self._columns = columns
        self._populate_grid(ordered, columns)

        self._clear_selection_if_gone(new_paths)

    # --------------------------------------------------- Grid management

    def _create_tile(self, clip: Path) -> _ClipTile:
        """Build a tile for ``clip`` and wire its signals and workers."""
        tile = _ClipTile(clip, self._grid_host)
        tile.selected.connect(self._on_tile_selected)
        tile.activated.connect(self._open_clip)
        tile.context_requested.connect(self._show_tile_menu)
        self._enqueue_thumbnail(clip, tile)
        self._enqueue_duration(clip)
        return tile

    def _populate_grid(self, tiles: Sequence[_ClipTile], columns: int) -> None:
        """Lay ``tiles`` into the grid using ``columns`` columns per row."""
        # takeAt detaches every item without deleting the tiles -- they are
        # re-added immediately below, so they never lose their thumbnails.
        while self._grid.count():
            self._grid.takeAt(0)

        for index, tile in enumerate(tiles):
            row, column = divmod(index, columns)
            self._grid.addWidget(tile, row, column)
            # Selection state survives a reflow because the tile object is
            # reused; re-assert it so the outline stays on the right tile.
            tile.set_selected(self._selected_clip is not None and tile.clip == self._selected_clip)

    def _column_count(self) -> int:
        """Columns that fit the current width, at least one."""
        # viewport width minus the content margins on both sides.
        available = self.width() - 2 * SPACING_XL
        return max(1, available // _COLUMN_BUDGET)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        # Re-flow only when the column count actually changes -- a width tweak
        # that does not cross a column boundary needs no relayout work.
        columns = self._column_count()
        if columns != self._columns and self._tiles:
            self._columns = columns
            ordered = [
                self._tiles[str(clip)]
                for clip in self._discover_clips()
                if str(clip) in self._tiles
            ]
            if ordered:
                self._populate_grid(ordered, columns)

    # ------------------------------------------------------------ Slots

    def _on_tile_selected(self, clip: Path) -> None:
        """Mark ``clip`` selected and clear the outline on every other tile."""
        self._selected_clip = clip
        for key, tile in self._tiles.items():
            tile.set_selected(key == str(clip))

    def _on_directory_changed(self, _path: str) -> None:
        # Restart the debounce window; refresh fires once the dust settles.
        self._refresh_timer.start(_REFRESH_DEBOUNCE_MS)

    # --------------------------------------------------- Context actions

    def _show_tile_menu(self, clip: Path, global_pos: QPoint) -> None:
        """Show the right-click menu for a tile."""
        # A right-click also selects the tile so the menu's target is obvious.
        self._on_tile_selected(clip)

        menu = QMenu(self)
        open_action = menu.addAction("Open")
        reveal_action = menu.addAction("Reveal in folder")
        menu.addSeparator()
        rename_action = menu.addAction("Rename…")
        delete_action = menu.addAction("Delete…")

        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is open_action:
            self._open_clip(clip)
        elif chosen is reveal_action:
            self._reveal_clip(clip)
        elif chosen is rename_action:
            self._rename_clip(clip)
        elif chosen is delete_action:
            self._delete_clip(clip)

    def _open_clip(self, clip: Path) -> None:
        """Open a clip with the platform's default video handler."""
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(clip))):
            logger.warning("Could not open clip %s", clip)

    def _reveal_clip(self, clip: Path) -> None:
        """Open the folder containing the clip."""
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(clip.parent)))

    def _open_clips_folder(self) -> None:
        """Open the clips directory in the system file browser."""
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._clips_dir)))

    def _rename_clip(self, clip: Path) -> None:
        """Prompt for a new stem, then rename the clip and its thumbnail."""
        new_stem, accepted = QInputDialog.getText(
            self,
            "Rename clip",
            "New name:",
            text=clip.stem,
        )
        if not accepted:
            return
        new_stem = new_stem.strip()
        if not new_stem:
            return
        # Reject path separators so a rename can never escape the folder.
        if any(ch in new_stem for ch in ("/", "\\", "\0")):
            QMessageBox.warning(
                self,
                "Invalid name",
                "A clip name cannot contain path separators.",
            )
            return

        target = clip.with_name(new_stem + clip.suffix)
        if target == clip:
            return
        if target.exists():
            QMessageBox.warning(
                self,
                "Name already used",
                f"A clip called {target.name} already exists.",
            )
            return

        try:
            clip.rename(target)
        except OSError as exc:
            logger.warning("Rename failed for %s -> %s: %s", clip, target, exc)
            QMessageBox.warning(self, "Rename failed", str(exc))
            return

        # Move the cached thumbnail alongside its clip so the next refresh
        # finds it without a fresh FFmpeg invocation. A failure here is benign
        # -- the worker simply regenerates the thumbnail.
        old_thumb = _thumb_path_for(clip)
        if old_thumb.is_file():
            try:
                old_thumb.rename(_thumb_path_for(target))
            except OSError as exc:
                logger.debug("Could not move thumbnail for %s: %s", clip, exc)

        if self._selected_clip == clip:
            self._selected_clip = target
        self.refresh()

    def _delete_clip(self, clip: Path) -> None:
        """Confirm, then delete the clip and its cached thumbnail."""
        answer = QMessageBox.question(
            self,
            "Delete clip",
            f"Permanently delete {clip.name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return

        try:
            clip.unlink()
        except OSError as exc:
            logger.warning("Could not delete %s: %s", clip, exc)
            QMessageBox.warning(self, "Delete failed", str(exc))
            return

        # Tidy the thumbnail too; its loss does not change the user's sense
        # of "deleted", so a failure here is only worth a debug line.
        thumb = _thumb_path_for(clip)
        try:
            thumb.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Could not delete thumbnail for %s: %s", clip, exc)

        if self._selected_clip == clip:
            self._selected_clip = None
        self.refresh()

    # ------------------------------------------------- Worker plumbing

    def _enqueue_thumbnail(self, clip: Path, tile: _ClipTile) -> None:
        """Show a cached thumbnail, or schedule a worker to generate one."""
        thumb = _thumb_path_for(clip)
        if thumb.is_file():
            pixmap = QPixmap(str(thumb))
            if not pixmap.isNull():
                tile.apply_thumbnail(pixmap)
                return

        # No usable cache. Without FFmpeg there is nothing to generate, so
        # settle the placeholder straight away.
        if self._ffmpeg is None:
            tile.show_no_preview()
            return

        worker = _ThumbnailWorker(clip, thumb, self._ffmpeg)
        worker.signals.ready.connect(self._on_thumbnail_ready)
        worker.signals.failed.connect(self._on_thumbnail_failed)
        self._pool.start(worker)

    def _enqueue_duration(self, clip: Path) -> None:
        """Schedule a background duration probe for ``clip``."""
        # Skip entirely when neither probe binary is available.
        if self._ffprobe is None and self._ffmpeg is None:
            return
        worker = _DurationWorker(clip, self._ffprobe, self._ffmpeg)
        worker.signals.ready.connect(self._on_duration_ready)
        self._pool.start(worker)

    def _on_thumbnail_ready(self, clip_path: str) -> None:
        tile = self._tiles.get(clip_path)
        if tile is None:
            return  # tile was removed (clip deleted) before the worker finished
        pixmap = QPixmap(str(_thumb_path_for(Path(clip_path))))
        if pixmap.isNull():
            tile.show_no_preview()
            return
        tile.apply_thumbnail(pixmap)

    def _on_thumbnail_failed(self, clip_path: str) -> None:
        tile = self._tiles.get(clip_path)
        if tile is not None:
            tile.show_no_preview()

    def _on_duration_ready(self, clip_path: str, seconds: float) -> None:
        tile = self._tiles.get(clip_path)
        if tile is not None:
            tile.apply_duration(seconds)

    # -------------------------------------------------------- Helpers

    @staticmethod
    def _resolve_ffmpeg() -> Path | None:
        """Locate FFmpeg, returning ``None`` if it cannot be found."""
        if find_ffmpeg is None:
            return None
        try:
            return find_ffmpeg()
        except Exception as exc:
            logger.info("FFmpeg not available for thumbnails: %s", exc)
            return None

    @staticmethod
    def _resolve_ffprobe() -> Path | None:
        """Locate ffprobe, returning ``None`` if it cannot be found."""
        if ffprobe_path is None:
            return None
        try:
            return ffprobe_path()
        except Exception as exc:
            logger.info("ffprobe not available for duration probes: %s", exc)
            return None

    def _discover_clips(self) -> list[Path]:
        """List ``*.mp4`` files in the clips folder, newest first."""
        try:
            if not self._clips_dir.exists():
                return []
            entries = [
                entry
                for entry in self._clips_dir.iterdir()
                if entry.is_file() and entry.suffix.lower() == ".mp4"
            ]
        except OSError as exc:
            logger.warning("Could not list clips dir %s: %s", self._clips_dir, exc)
            return []
        # Sort by modification time, newest first; a clip that vanishes between
        # the listing and the stat is skipped rather than crashing the sort.
        entries.sort(key=self._safe_mtime, reverse=True)
        return entries

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _clear_selection_if_gone(self, live_paths: set[str]) -> None:
        """Drop the remembered selection if its clip no longer exists."""
        if self._selected_clip is not None and str(self._selected_clip) not in live_paths:
            self._selected_clip = None


__all__ = ["LibraryPage"]
