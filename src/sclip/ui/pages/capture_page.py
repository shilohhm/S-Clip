"""Capture page — the front door of the app.

The whole screen is built around one control: the record orb. Whatever the
engine is doing, the orb shows it and the orb acts on it — start a recording,
arm the replay buffer, save a clip. A status pill, a summary of what will be
captured, and a strip of recent clips sit around it.

The page is a passive view. It never writes settings; it only reads the
current :class:`Settings` to fill the summary card, and emits
``request_navigate`` when it wants the host window to switch screens.

Threading note: the capture engine publishes its updates to listeners
registered through ``add_state_listener``, ``add_clip_listener`` and
``add_error_listener``, and those listeners may fire on a worker thread. We
never touch a widget from inside them — each listener emits a :class:`Signal`
defined here, and Qt marshals the slot back onto the GUI thread.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sclip.contracts import CaptureEngine, CaptureMode, CaptureState, Settings, SettingsStore
from sclip.paths import app_paths
from sclip.ui.assets.icons import icon
from sclip.ui.theme import (
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XL,
    SPACING_XS,
    SPACING_XXS,
    THEME,
)
from sclip.ui.widgets import Card, IconButton, RecordOrb, SegmentedControl, StatusPill
from sclip.ui.widgets.record_orb import (
    ANIM_NONE,
    ANIM_PULSE,
    ANIM_SPIN,
    ANIM_SWEEP,
    GLYPH_CIRCLE,
    GLYPH_SPINNER,
    GLYPH_SQUARE,
)

logger = logging.getLogger(__name__)


# Destinations carried by ``request_navigate``. Plain strings so the host
# window can match on them without importing an enum.
NAV_LIBRARY = "library"
NAV_SETTINGS = "settings"

# How many recent clips the capture page surfaces. Four tiles fill one row;
# the full set lives in the Library.
_RECENT_CLIPS_LIMIT = 4

# The mode selector is capped so a two-option control does not span the card.
_MODE_SELECTOR_WIDTH = 360

# Recent-clip tiles use a fixed 16:9 thumbnail so the row stays tidy.
_THUMB_WIDTH = 168
_THUMB_HEIGHT = 94
_TILE_WIDTH = 188

# The two segmented-control options, in display order.
_MODE_OPTIONS: tuple[str, ...] = ("Manual recording", "Replay buffer")
_MODE_BY_INDEX: tuple[CaptureMode, ...] = (CaptureMode.MANUAL, CaptureMode.REPLAY_BUFFER)


def _format_elapsed(seconds: float) -> str:
    """Render an elapsed-seconds count as ``M:SS`` (or ``H:MM:SS`` past an hour)."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _audio_summary(settings: Settings) -> str:
    """Condense the audio settings into a short chip value."""
    if not settings.capture_audio:
        return "Off"
    has_mic = bool(settings.audio_input)
    has_desktop = settings.capture_desktop_audio
    if has_mic and has_desktop:
        return "Mic + desktop"
    if has_mic:
        return "Mic only"
    if has_desktop:
        return "Desktop only"
    return "No device"


def _quality_summary(settings: Settings) -> str:
    """Build the quality chip value, e.g. ``2560x1440 · 60fps``."""
    return f"{settings.resolution} · {settings.fps}fps"


def _existing_thumbnail(clip: Path) -> QPixmap | None:
    """Pick up a cached thumbnail beside a clip, if the library produced one.

    Thumbnails are never generated here — that is the library's job. We only
    look for the ``<clip>.thumb.jpg`` cache the library leaves behind.
    """
    candidates = (
        clip.with_suffix(clip.suffix + ".thumb.jpg"),
        clip.parent / f"{clip.stem}.thumb.jpg",
    )
    for thumb_path in candidates:
        try:
            if thumb_path.is_file():
                pixmap = QPixmap(str(thumb_path))
                if not pixmap.isNull():
                    return pixmap
        except OSError as exc:
            logger.debug("Could not read thumbnail %s: %s", thumb_path, exc)
    return None


def _chip(label_text: str, parent: QWidget) -> tuple[QFrame, QLabel]:
    """Build one ``#Chip`` frame and return ``(frame, value_label)``."""
    frame = QFrame(parent)
    frame.setObjectName("Chip")
    frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    layout = QVBoxLayout(frame)
    layout.setContentsMargins(SPACING_SM, SPACING_SM, SPACING_SM, SPACING_SM)
    layout.setSpacing(SPACING_XXS)

    caption = QLabel(label_text, frame)
    caption.setObjectName("ChipLabel")
    layout.addWidget(caption)

    value = QLabel("—", frame)
    value.setObjectName("ChipValue")
    layout.addWidget(value)
    return frame, value


class _ClipTile(QFrame):
    """One clickable thumbnail tile in the "Recent clips" row."""

    activated = Signal(object)  # emits the clip Path on double-click

    def __init__(
        self, clip: Path, thumbnail: QPixmap | None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._clip = clip
        self.setObjectName("ClipTile")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(_TILE_WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{clip.name}\nDouble-click to open")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING_XS, SPACING_XS, SPACING_XS, SPACING_XS)
        layout.setSpacing(SPACING_XS)

        thumb = QLabel(self)
        thumb.setObjectName("ClipThumb")
        thumb.setFixedSize(_THUMB_WIDTH, _THUMB_HEIGHT)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if thumbnail is not None and not thumbnail.isNull():
            thumb.setPixmap(
                thumbnail.scaled(
                    _THUMB_WIDTH,
                    _THUMB_HEIGHT,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            thumb.setText("No preview")
        layout.addWidget(thumb)

        name = QLabel(self)
        name.setProperty("role", "caption")
        elided = name.fontMetrics().elidedText(
            clip.name, Qt.TextElideMode.ElideMiddle, _THUMB_WIDTH
        )
        name.setText(elided)
        layout.addWidget(name)

    def mouseDoubleClickEvent(self, event: object) -> None:
        self.activated.emit(self._clip)
        super().mouseDoubleClickEvent(event)  # type: ignore[arg-type]


class CapturePage(QWidget):
    """Top-level page that drives the capture engine through the record orb."""

    request_navigate = Signal(str)
    clip_saved = Signal(object)

    # Internal bridge signals — the engine callbacks emit these so the slots
    # always run on the GUI thread (see the module docstring).
    _engine_state_changed = Signal(object)
    _engine_clip_saved = Signal(object)
    _engine_error = Signal(str)

    def __init__(
        self,
        engine: CaptureEngine,
        settings_store: SettingsStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._settings_store = settings_store
        self._settings: Settings = settings_store.load()

        # Which capture flavour the user has selected — purely local view state.
        self._mode: CaptureMode = CaptureMode.MANUAL
        self._last_error: str = ""
        # Wall-clock start of the current recording, for the elapsed readout.
        self._session_started: float | None = None

        # Ticks once a second while recording so the elapsed time stays live.
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)

        self._build_ui()
        self._wire_bridge_signals()
        self._wire_engine_callbacks()

        # File IO never belongs in a constructor — defer the clips scan and the
        # first paint so the window can show immediately.
        QTimer.singleShot(0, self._refresh_recent_clips)
        QTimer.singleShot(0, lambda: self._render_state(self._engine.state))

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        """Assemble the page: a scroll area wrapping the hero and two cards.

        The scroll area is load-bearing — without it, content taller than the
        window crushes instead of scrolling.
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        root.addWidget(scroll)

        content = QWidget(scroll)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(SPACING_XL, SPACING_XL, SPACING_XL, SPACING_XL)
        content_layout.setSpacing(SPACING_LG)

        content_layout.addLayout(self._build_header_row())
        content_layout.addWidget(self._build_hero_card())
        content_layout.addWidget(self._build_captured_card())
        content_layout.addWidget(self._build_recent_card())
        content_layout.addStretch(1)

        scroll.setWidget(content)

    def _build_header_row(self) -> QHBoxLayout:
        """The page title, with the status pill opposite it."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACING_SM)

        title = QLabel("Capture", self)
        title.setProperty("role", "display")
        row.addWidget(title)
        row.addStretch(1)

        self._status_pill = StatusPill(parent=self)
        row.addWidget(self._status_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _build_hero_card(self) -> Card:
        """The hero — mode selector, the record orb, and its state copy."""
        card = Card(parent=self)
        body = card.body_layout()
        body.setSpacing(SPACING_LG)

        # -- Mode selector, centred above the orb -------------------------
        self._mode_selector = SegmentedControl(list(_MODE_OPTIONS), parent=card)
        self._mode_selector.setMaximumWidth(_MODE_SELECTOR_WIDTH)
        self._mode_selector.current_changed.connect(self._on_mode_changed)
        body.addLayout(_centred(self._mode_selector))

        # -- The orb ------------------------------------------------------
        self._orb = RecordOrb(card)
        self._orb.clicked.connect(self._on_orb_clicked)
        body.addLayout(_centred(self._orb))

        # -- State copy ---------------------------------------------------
        self._headline = QLabel("Ready to record", card)
        self._headline.setProperty("role", "display")
        self._headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(self._headline)

        self._caption = QLabel("", card)
        self._caption.setProperty("role", "caption")
        self._caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._caption.setWordWrap(True)
        body.addWidget(self._caption)

        # -- Secondary action (only shown while the buffer is armed) ------
        self._secondary_button = IconButton(
            text="Disarm replay buffer",
            icon=icon("stop", THEME.text_secondary, 14),
            role="ghost",
            parent=card,
        )
        self._secondary_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._secondary_button.clicked.connect(self._on_secondary_clicked)
        self._secondary_button.setVisible(False)
        body.addLayout(_centred(self._secondary_button))

        return card

    def _build_captured_card(self) -> Card:
        """A card of three chips summarising what the engine will capture."""
        card = Card(title="What's being captured", parent=self)
        body = card.body_layout()
        body.setSpacing(SPACING_MD)

        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(0, 0, 0, 0)
        chip_row.setSpacing(SPACING_SM)

        monitor_chip, self._monitor_value = _chip("MONITOR", card)
        audio_chip, self._audio_value = _chip("AUDIO", card)
        quality_chip, self._quality_value = _chip("QUALITY", card)
        for chip in (monitor_chip, audio_chip, quality_chip):
            chip_row.addWidget(chip)
        chip_row.addStretch(1)
        body.addLayout(chip_row)

        edit_button = IconButton(
            text="Edit in settings",
            icon=icon("settings", THEME.text_secondary, 15),
            role="ghost",
            parent=card,
        )
        edit_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        edit_button.clicked.connect(lambda: self.request_navigate.emit(NAV_SETTINGS))

        edit_row = QHBoxLayout()
        edit_row.setContentsMargins(0, 0, 0, 0)
        edit_row.addWidget(edit_button)
        edit_row.addStretch(1)
        body.addLayout(edit_row)

        self._refresh_captured_info()
        return card

    def _build_recent_card(self) -> Card:
        """A row of recent-clip tiles plus a link into the library."""
        card = Card(title="Recent clips", parent=self)
        body = card.body_layout()
        body.setSpacing(SPACING_MD)

        self._recent_container = QWidget(card)
        self._recent_row = QHBoxLayout(self._recent_container)
        self._recent_row.setContentsMargins(0, 0, 0, 0)
        self._recent_row.setSpacing(SPACING_SM)
        body.addWidget(self._recent_container)

        self._recent_empty = QLabel(
            "Clips you record will show up here. Use the orb above to make your first one.",
            card,
        )
        self._recent_empty.setProperty("role", "muted")
        self._recent_empty.setWordWrap(True)
        self._recent_empty.setVisible(False)
        body.addWidget(self._recent_empty)

        library_button = IconButton(
            text="Open library",
            icon=icon("library", THEME.text_secondary, 15),
            role="ghost",
            parent=card,
        )
        library_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        library_button.clicked.connect(lambda: self.request_navigate.emit(NAV_LIBRARY))

        library_row = QHBoxLayout()
        library_row.setContentsMargins(0, 0, 0, 0)
        library_row.addWidget(library_button)
        library_row.addStretch(1)
        body.addLayout(library_row)
        return card

    # ----------------------------------------------------- Engine wiring

    def _wire_bridge_signals(self) -> None:
        """Connect the internal bridge signals to their GUI-thread slots."""
        self._engine_state_changed.connect(self._render_state)
        self._engine_clip_saved.connect(self._on_clip_saved)
        self._engine_error.connect(self._on_engine_error)

    def _wire_engine_callbacks(self) -> None:
        """Register this page's bridge signals as engine listeners.

        The engine fans each event out to every registered listener, so the
        page coexists with the main window's own subscriptions. The listeners
        may fire on a worker thread; each one only emits a :class:`Signal`, and
        Qt marshals the connected slot back onto the GUI thread.
        """
        self._engine.add_state_listener(self._engine_state_changed.emit)
        self._engine.add_clip_listener(self._engine_clip_saved.emit)
        self._engine.add_error_listener(self._engine_error.emit)

    # --------------------------------------------------------- Slots

    def _on_mode_changed(self, index: int) -> None:
        """The user flipped the segmented control between the two modes."""
        if 0 <= index < len(_MODE_BY_INDEX):
            self._mode = _MODE_BY_INDEX[index]
        # The orb copy and visuals are mode-sensitive while idle, so re-render.
        self._render_state(self._engine.state)

    def _on_orb_clicked(self) -> None:
        """Handle a click on the orb — the action depends on the engine state.

        The orb is always "the obvious next thing": stop a recording, save a
        clip while the buffer rolls, or otherwise start whichever capture the
        selected mode implies.
        """
        state = self._engine.state
        try:
            if state is CaptureState.RECORDING:
                self._engine.stop_manual_recording()
            elif state is CaptureState.BUFFERING:
                self._engine.save_replay_clip()
            elif state is CaptureState.SAVING:
                return  # a save is already in flight — the orb is inert
            elif self._mode is CaptureMode.REPLAY_BUFFER:
                self._engine.start_replay_buffer()
            else:
                self._engine.start_manual_recording()
        except Exception:
            logger.exception("Capture action failed")

    def _on_secondary_clicked(self) -> None:
        """Stop the replay buffer — the orb's quieter companion action."""
        try:
            self._engine.stop_replay_buffer()
        except Exception:
            logger.exception("Could not stop the replay buffer")

    def _on_tick(self) -> None:
        """Refresh the elapsed-time readout once a second while recording."""
        if self._engine.state is CaptureState.RECORDING:
            self._render_state(CaptureState.RECORDING)

    def _render_state(self, state: CaptureState) -> None:
        """Turn an engine state into pixels — orb, pill, copy and buttons.

        This is the single place state becomes visible, so it is safe to call
        after a mode switch, on first paint, on a timer tick, or from the
        engine bridge.
        """
        self._status_pill.set_state(state)

        # Keep the elapsed timer running only while a recording is live.
        if state is CaptureState.RECORDING:
            if self._session_started is None:
                self._session_started = time.monotonic()
            if not self._tick_timer.isActive():
                self._tick_timer.start()
        else:
            self._session_started = None
            self._tick_timer.stop()

        self._apply_orb_visual(state)

        headline, caption = self._state_copy(state)
        self._headline.setText(headline)
        self._caption.setText(caption)

        # The disarm companion only makes sense while the buffer is rolling.
        self._secondary_button.setVisible(state is CaptureState.BUFFERING)

    def _apply_orb_visual(self, state: CaptureState) -> None:
        """Drive the orb's colour, glyph and animation from the engine state."""
        if state is CaptureState.RECORDING:
            self._orb.set_visual(
                accent=THEME.danger, glyph=GLYPH_SQUARE, animation=ANIM_PULSE, interactive=True
            )
        elif state is CaptureState.BUFFERING:
            self._orb.set_visual(
                accent=THEME.accent_secondary,
                glyph=GLYPH_CIRCLE,
                animation=ANIM_SWEEP,
                interactive=True,
            )
        elif state is CaptureState.SAVING:
            self._orb.set_visual(
                accent=THEME.accent_primary,
                glyph=GLYPH_SPINNER,
                animation=ANIM_SPIN,
                interactive=False,
            )
        elif state is CaptureState.ERROR:
            self._orb.set_visual(
                accent=THEME.warning, glyph=GLYPH_CIRCLE, animation=ANIM_NONE, interactive=True
            )
        else:  # IDLE
            self._orb.set_visual(
                accent=THEME.accent_primary,
                glyph=GLYPH_CIRCLE,
                animation=ANIM_NONE,
                interactive=True,
            )

    def _state_copy(self, state: CaptureState) -> tuple[str, str]:
        """Return the ``(headline, caption)`` shown beneath the orb."""
        seconds = max(1, int(self._settings.replay_seconds))

        if state is CaptureState.RECORDING:
            elapsed = time.monotonic() - (self._session_started or time.monotonic())
            return "Recording", f"{_format_elapsed(elapsed)} elapsed  ·  click the orb to stop"
        if state is CaptureState.BUFFERING:
            return (
                "Replay buffer armed",
                f"The last {seconds} seconds are always ready — click the orb to save a clip.",
            )
        if state is CaptureState.SAVING:
            return "Saving your clip", "Stitching the footage into one smooth file."
        if state is CaptureState.ERROR:
            return (
                "Something went wrong",
                self._last_error or "The capture engine reported an error.",
            )

        # IDLE — the copy depends on the selected mode.
        if self._mode is CaptureMode.REPLAY_BUFFER:
            return (
                "Replay buffer is off",
                f"Arm it to keep the last {seconds} seconds ready to clip at any moment.",
            )
        return "Ready to record", "Click the orb, or press your hotkey, to start."

    def _on_clip_saved(self, path: object) -> None:
        """Engine dropped a clip on disk — re-emit upwards and refresh Card 3."""
        clip_path = path if isinstance(path, Path) else Path(str(path))
        logger.info("Clip saved: %s", clip_path)
        self.clip_saved.emit(clip_path)
        QTimer.singleShot(50, self._refresh_recent_clips)

    def _on_engine_error(self, message: str) -> None:
        """Remember a recoverable engine error and surface it on the orb."""
        logger.error("Engine error: %s", message)
        self._last_error = message
        self._render_state(CaptureState.ERROR)

    # ----------------------------------------------- Public hooks

    def refresh_from_settings(self, settings: Settings) -> None:
        """Re-read monitor/audio/quality into the summary card.

        The host window calls this after the settings page saves, so the chips
        and the mode-sensitive orb copy stay in step with the engine.
        """
        self._settings = settings
        self._refresh_captured_info()
        self._render_state(self._engine.state)

    # ----------------------------------------------- Internal helpers

    def _refresh_captured_info(self) -> None:
        """Repaint the three summary chips from the cached settings."""
        settings = self._settings
        self._monitor_value.setText(settings.monitor or "Default")
        self._audio_value.setText(_audio_summary(settings))
        self._quality_value.setText(_quality_summary(settings))

    def _refresh_recent_clips(self) -> None:
        """Rescan the clip directories and rebuild the recent-clip tiles."""
        clips = self._scan_recent_clips()

        while self._recent_row.count():
            item = self._recent_row.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

        if not clips:
            self._recent_container.setVisible(False)
            self._recent_empty.setVisible(True)
            return

        self._recent_empty.setVisible(False)
        self._recent_container.setVisible(True)
        for clip in clips:
            tile = _ClipTile(clip, _existing_thumbnail(clip), self._recent_container)
            tile.activated.connect(self._open_clip)
            self._recent_row.addWidget(tile)
        self._recent_row.addStretch(1)

    def _scan_recent_clips(self) -> list[Path]:
        """Return the newest ``.mp4`` files across the clip directories."""
        directories: list[Path] = [app_paths().clips_dir]
        output_dir = (self._settings.output_dir or "").strip()
        if output_dir:
            extra = Path(output_dir)
            if extra not in directories:
                directories.append(extra)

        found: dict[Path, float] = {}
        for directory in directories:
            try:
                if not directory.is_dir():
                    continue
                for entry in directory.iterdir():
                    if entry.is_file() and entry.suffix.lower() == ".mp4":
                        found[entry.resolve()] = entry.stat().st_mtime
            except OSError as exc:
                logger.warning("Could not list clips in %s: %s", directory, exc)

        newest = sorted(found.items(), key=lambda pair: pair[1], reverse=True)
        return [path for path, _mtime in newest[:_RECENT_CLIPS_LIMIT]]

    def _open_clip(self, path: object) -> None:
        """Hand a clip to the user's default media player."""
        clip_path = path if isinstance(path, Path) else Path(str(path))
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(clip_path))):
            logger.warning("Could not open clip %s with the default handler", clip_path)


def _centred(widget: QWidget) -> QHBoxLayout:
    """Wrap ``widget`` in a horizontal layout that centres it."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    row.addStretch(1)
    row.addWidget(widget)
    row.addStretch(1)
    return row


__all__ = ["NAV_LIBRARY", "NAV_SETTINGS", "CapturePage"]
