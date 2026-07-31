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
from PySide6.QtGui import QDesktopServices, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sclip.contracts import (
    BufferTelemetry,
    CaptureEngine,
    CaptureMode,
    CaptureState,
    Settings,
    SettingsStore,
)
from sclip.paths import app_paths
from sclip.ui.assets.icons import icon
from sclip.ui.formatting import format_bitrate, format_bytes
from sclip.ui.theme import (
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XL,
    SPACING_XS,
    SPACING_XXS,
    THEME,
)
from sclip.ui.widgets import (
    BufferMeter,
    Card,
    IconButton,
    RecordOrb,
    SegmentedControl,
    StatusPill,
)
from sclip.ui.widgets.record_orb import (
    ANIM_NONE,
    ANIM_PULSE,
    ANIM_SPIN,
    ANIM_SWEEP,
    GLYPH_CIRCLE,
    GLYPH_SPINNER,
    GLYPH_SQUARE,
    ORB_EXTENT_COMPACT,
    ORB_EXTENT_DEFAULT,
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

# Recent-clip tiles use a fixed 16:9 thumbnail so the row stays tidy. The
# thumbnail width is derived from the tile rather than written out, because a
# hand-picked number drifts: it was 168 against a 170px content box, so every
# thumbnail sat narrower than the filename beneath it and nothing in the row
# shared an edge. ``QFrame#ClipTile`` carries a 1px border that Qt removes from
# the content rect before the layout runs, so it comes off here too.
_TILE_WIDTH = 188
_TILE_BORDER = 1
_THUMB_WIDTH = _TILE_WIDTH - 2 * _TILE_BORDER - 2 * SPACING_XS
_THUMB_HEIGHT = round(_THUMB_WIDTH * 9 / 16)

# The two segmented-control options, in display order.
_MODE_OPTIONS: tuple[str, ...] = ("Manual recording", "Replay buffer")
_MODE_BY_INDEX: tuple[CaptureMode, ...] = (CaptureMode.MANUAL, CaptureMode.REPLAY_BUFFER)

# The page sits beside a 218px navigation rail. Below this content width, the
# instrument and readout stack so the documented 960px window minimum never
# needs a horizontal scrollbar.
_COMPACT_BREAKPOINT = 860


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


def _stat_row(layout: QVBoxLayout, label_text: str, parent: QWidget) -> QLabel:
    """Append a ``LABEL ............ value`` row and return its value label."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(SPACING_SM)

    caption = QLabel(label_text, parent)
    caption.setObjectName("StatLabel")
    row.addWidget(caption)
    row.addStretch(1)

    value = QLabel("—", parent)
    value.setObjectName("StatValue")
    row.addWidget(value)

    layout.addLayout(row)
    return value


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

        # Start in the mode the user has enabled. Competitive players should
        # land on the replay control they configured, not a contradictory
        # manual-recording tab while the buffer is already running.
        self._mode: CaptureMode = (
            CaptureMode.REPLAY_BUFFER if self._settings.replay_buffer else CaptureMode.MANUAL
        )
        self._last_error: str = ""
        # Wall-clock start of the current recording, for the elapsed readout.
        self._session_started: float | None = None
        # Most recent buffer snapshot, refreshed on every render. ``None`` means
        # the engine has no rolling window to describe.
        self._telemetry: BufferTelemetry | None = None
        # Tri-state on purpose: ``None`` means "no layout applied yet", so the
        # first resize always composes the stage rather than short-circuiting
        # because it happens to match the default.
        self._compact: bool | None = None
        # How many recent-clip tiles the last rebuild laid out. -1 forces the
        # first resize to populate the row.
        self._recent_capacity: int = -1

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
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(scroll)

        content = QWidget(scroll)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(SPACING_XL, SPACING_XL, SPACING_XL, SPACING_XL)
        content_layout.setSpacing(SPACING_LG)

        content_layout.addLayout(self._build_header_row())
        content_layout.addWidget(self._build_capture_stage())
        content_layout.addWidget(self._build_profile_bar())
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

    def _build_capture_stage(self) -> QFrame:
        """Build the primary instrument stage and its live readout."""
        stage = QFrame(self)
        stage.setObjectName("CaptureStage")
        stage.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        stage.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self._stage_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, stage)
        self._stage_layout.setContentsMargins(
            SPACING_XL,
            SPACING_LG,
            SPACING_XL,
            SPACING_XL,
        )
        self._stage_layout.setSpacing(SPACING_XL)
        self._capture_instrument = self._build_capture_instrument(stage)
        self._stage_layout.addWidget(self._capture_instrument, 3)
        self._capture_readout = self._build_capture_readout(stage)
        self._stage_layout.addWidget(self._capture_readout, 2)
        return stage

    def _build_capture_instrument(self, parent: QWidget) -> QWidget:
        """Build the mode selector and physical record control."""
        instrument = QWidget(parent)
        instrument_layout = QVBoxLayout(instrument)
        instrument_layout.setContentsMargins(0, 0, 0, 0)
        instrument_layout.setSpacing(SPACING_MD)

        mode_label = QLabel("CAPTURE MODE", instrument)
        mode_label.setObjectName("Eyebrow")
        instrument_layout.addWidget(mode_label, 0, Qt.AlignmentFlag.AlignHCenter)

        current_index = _MODE_BY_INDEX.index(self._mode)
        self._mode_selector = SegmentedControl(
            list(_MODE_OPTIONS),
            current_index=current_index,
            parent=instrument,
        )
        self._mode_selector.setMaximumWidth(_MODE_SELECTOR_WIDTH)
        self._mode_selector.current_changed.connect(self._on_mode_changed)
        instrument_layout.addLayout(_centred(self._mode_selector))

        self._orb = RecordOrb(instrument)
        self._orb.setAccessibleName("Capture control")
        self._orb.clicked.connect(self._on_orb_clicked)
        instrument_layout.addLayout(_centred(self._orb))

        self._orb_action_label = QLabel("CLICK TO RECORD", instrument)
        self._orb_action_label.setObjectName("Eyebrow")
        instrument_layout.addWidget(
            self._orb_action_label,
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )
        return instrument

    def _build_capture_readout(self, parent: QWidget) -> QFrame:
        """Build the state copy, keyboard hint, and secondary action."""
        readout = QFrame(parent)
        readout.setObjectName("CaptureReadout")
        readout.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        readout.setMinimumWidth(300)
        readout.setMaximumWidth(380)
        readout_layout = QVBoxLayout(readout)
        readout_layout.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        readout_layout.setSpacing(SPACING_SM)

        readout_label = QLabel("LIVE STATE", readout)
        readout_label.setObjectName("Eyebrow")
        readout_layout.addWidget(readout_label)

        self._headline = QLabel("Ready to record", readout)
        self._headline.setProperty("role", "display")
        self._headline.setWordWrap(True)
        readout_layout.addWidget(self._headline)

        self._caption = QLabel("", readout)
        self._caption.setProperty("role", "caption")
        self._caption.setWordWrap(True)
        readout_layout.addWidget(self._caption)

        readout_layout.addWidget(self._build_telemetry_block(readout))
        readout_layout.addSpacing(SPACING_SM)

        shortcut = QFrame(readout)
        shortcut.setObjectName("ShortcutHint")
        shortcut.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        shortcut_layout = QHBoxLayout(shortcut)
        shortcut_layout.setContentsMargins(SPACING_SM, SPACING_XS, SPACING_SM, SPACING_XS)
        shortcut_layout.setSpacing(SPACING_SM)

        self._shortcut_label = QLabel("START RECORDING", shortcut)
        self._shortcut_label.setObjectName("ShortcutLabel")
        shortcut_layout.addWidget(self._shortcut_label)
        shortcut_layout.addStretch(1)

        self._shortcut_key = QLabel(self._settings.record_hotkey.to_display(), shortcut)
        self._shortcut_key.setObjectName("ShortcutKey")
        shortcut_layout.addWidget(self._shortcut_key)
        readout_layout.addWidget(shortcut)

        self._secondary_button = IconButton(
            text="Disarm replay buffer",
            icon=icon("stop", THEME.text_secondary, 14),
            role="ghost",
            parent=readout,
        )
        self._secondary_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._secondary_button.clicked.connect(self._on_secondary_clicked)
        self._secondary_button.setVisible(False)
        readout_layout.addWidget(self._secondary_button, 0, Qt.AlignmentFlag.AlignLeft)
        return readout

    def _build_telemetry_block(self, parent: QWidget) -> QWidget:
        """Build the live buffer meter and its three figures.

        Everything sits in one container so a single ``setVisible`` hides the
        whole block in states with no rolling window. There is deliberately no
        stretch anywhere in the readout: the panel sizes to its content, so an
        engine that reports nothing leaves a tidy card rather than the 200px
        gap this screen used to hold open.
        """
        box = QWidget(parent)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, SPACING_XS, 0, 0)
        layout.setSpacing(SPACING_XS)

        self._buffer_meter = BufferMeter(box)
        layout.addWidget(self._buffer_meter)

        stats = QVBoxLayout()
        stats.setContentsMargins(0, 0, 0, 0)
        stats.setSpacing(SPACING_XXS)
        self._disk_value = _stat_row(stats, "ON DISK", box)
        self._bitrate_value = _stat_row(stats, "BITRATE", box)
        self._segments_value = _stat_row(stats, "SEGMENTS", box)
        layout.addLayout(stats)

        box.setVisible(False)
        self._telemetry_box = box
        return box

    def _build_profile_bar(self) -> QFrame:
        """Build a compact, one-line summary of the active capture profile."""
        profile = QFrame(self)
        profile.setObjectName("ProfileBar")
        profile.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._profile_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, profile)
        self._profile_layout.setContentsMargins(
            SPACING_MD,
            SPACING_SM,
            SPACING_SM,
            SPACING_SM,
        )
        self._profile_layout.setSpacing(SPACING_SM)

        heading = QVBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(SPACING_XXS)
        title = QLabel("CAPTURE PROFILE", profile)
        title.setObjectName("Eyebrow")
        heading.addWidget(title)
        subtitle = QLabel("Applied to the next clip", profile)
        subtitle.setProperty("role", "muted")
        heading.addWidget(subtitle)
        self._profile_layout.addLayout(heading)

        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(0, 0, 0, 0)
        chip_row.setSpacing(SPACING_SM)

        monitor_chip, self._monitor_value = _chip("MONITOR", profile)
        audio_chip, self._audio_value = _chip("AUDIO", profile)
        quality_chip, self._quality_value = _chip("QUALITY", profile)
        for chip in (monitor_chip, audio_chip, quality_chip):
            chip_row.addWidget(chip)
        self._profile_layout.addLayout(chip_row)
        self._profile_layout.addStretch(1)

        edit_button = IconButton(
            text="Edit",
            icon=icon("settings", THEME.text_secondary, 15),
            role="ghost",
            parent=profile,
        )
        edit_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        edit_button.clicked.connect(lambda: self.request_navigate.emit(NAV_SETTINGS))
        self._profile_layout.addWidget(edit_button)

        self._refresh_captured_info()
        return profile

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Recompose dense capture controls at the compact breakpoint."""
        super().resizeEvent(event)
        self._apply_compact_layout(event.size().width() < _COMPACT_BREAKPOINT)
        # Rebuild the recent row only when the number of tiles that fit
        # actually changes, so an ordinary drag does not re-scan the disk.
        if self._recent_tile_capacity() != self._recent_capacity:
            self._refresh_recent_clips()

    def _apply_compact_layout(self, compact: bool) -> None:
        """Recompose the capture stage for narrow windows.

        Stacking on its own is not enough. With the instrument on top, a
        248px orb plus the mode selector and its labels pushed the readout —
        which carries the save action and its hotkey — clean below the fold at
        the documented 960x640 minimum, so the primary control of the whole
        application was invisible at the smallest supported size.

        In compact mode the readout therefore leads and the orb shrinks. The
        supported window size does not change; the priority order does.
        """
        if compact == self._compact:
            return
        self._compact = compact

        stage_layout = getattr(self, "_stage_layout", None)
        readout = getattr(self, "_capture_readout", None)
        instrument = getattr(self, "_capture_instrument", None)
        if stage_layout is not None and readout is not None and instrument is not None:
            stage_layout.setDirection(
                QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight
            )
            # QBoxLayout cannot reorder in place, so both children are detached
            # and re-added. They stay parented to the stage throughout, so this
            # is a re-seat rather than a rebuild.
            stage_layout.removeWidget(instrument)
            stage_layout.removeWidget(readout)
            if compact:
                stage_layout.addWidget(readout, 0)
                stage_layout.addWidget(instrument, 0)
            else:
                stage_layout.addWidget(instrument, 3)
                stage_layout.addWidget(readout, 2)

            readout.setMinimumWidth(0 if compact else 300)
            readout.setMaximumWidth(16_777_215 if compact else 380)

        orb = getattr(self, "_orb", None)
        if orb is not None:
            orb.set_extent(ORB_EXTENT_COMPACT if compact else ORB_EXTENT_DEFAULT)

        profile_layout = getattr(self, "_profile_layout", None)
        if profile_layout is not None:
            profile_layout.setDirection(
                QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight
            )

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
        """Refresh the live readouts once a second.

        Both the elapsed recording clock and the replay-buffer fill change on
        their own without any engine event to announce it, so they need a
        timer rather than a state transition to stay honest.
        """
        state = self._engine.state
        if state in (CaptureState.RECORDING, CaptureState.BUFFERING, CaptureState.SAVING):
            self._render_state(state)

    def _read_telemetry(self) -> BufferTelemetry | None:
        """Ask the engine for a buffer snapshot, tolerating engines without one.

        This touches the disk — a directory listing plus a ``stat`` per segment
        — on the GUI thread. At one hertz over the sixteen segments a default
        thirty-second window holds, that is comfortably sub-millisecond. Were
        the poll ever made faster, or the window very long, this is the call
        that would need moving to a worker.
        """
        try:
            return self._engine.telemetry()
        except Exception:
            logger.exception("Reading capture telemetry failed")
            return None

    def _render_telemetry(self, state: CaptureState) -> None:
        """Show or hide the live buffer block and refresh its figures."""
        telemetry = self._telemetry
        # Only the rolling-window states have anything to report, and an engine
        # that cannot answer (a stub, or FFmpeg missing) hides the block rather
        # than showing dashes.
        visible = telemetry is not None and state in (
            CaptureState.BUFFERING,
            CaptureState.SAVING,
        )
        self._telemetry_box.setVisible(visible)
        if not visible or telemetry is None:
            return

        self._buffer_meter.set_fraction(telemetry.fill_fraction)
        self._disk_value.setText(format_bytes(telemetry.bytes_on_disk))
        self._bitrate_value.setText(format_bitrate(telemetry.bitrate_bps))
        self._segments_value.setText(f"{telemetry.segment_count} / {telemetry.segment_capacity}")

    def _render_state(self, state: CaptureState) -> None:
        """Turn an engine state into pixels — orb, pill, copy and buttons.

        This is the single place state becomes visible, so it is safe to call
        after a mode switch, on first paint, on a timer tick, or from the
        engine bridge.
        """
        self._sync_mode_to_state(state)
        self._status_pill.set_state(state)
        self._telemetry = self._read_telemetry()

        # The timer drives two live readouts: the elapsed clock while
        # recording, and the buffer fill while the replay window is rolling.
        if state is CaptureState.RECORDING:
            if self._session_started is None:
                self._session_started = time.monotonic()
            if not self._tick_timer.isActive():
                self._tick_timer.start()
        elif state in (CaptureState.BUFFERING, CaptureState.SAVING):
            self._session_started = None
            if not self._tick_timer.isActive():
                self._tick_timer.start()
        else:
            self._session_started = None
            self._tick_timer.stop()

        self._render_telemetry(state)

        self._apply_orb_visual(state)

        headline, caption = self._state_copy(state)
        self._headline.setText(headline)
        self._caption.setText(caption)
        shortcut_label, shortcut_key = self._shortcut_copy(state)
        self._shortcut_label.setText(shortcut_label)
        self._shortcut_key.setText(shortcut_key)
        self._orb_action_label.setText(self._orb_action_copy(state))
        self._orb.setAccessibleDescription(f"{headline}. {caption}")

        # The disarm companion only makes sense while the buffer is rolling.
        self._secondary_button.setVisible(state is CaptureState.BUFFERING)

    def _sync_mode_to_state(self, state: CaptureState) -> None:
        """Keep the selector truthful and lock it while capture is active."""
        forced_mode: CaptureMode | None = None
        if state is CaptureState.BUFFERING:
            forced_mode = CaptureMode.REPLAY_BUFFER
        elif state is CaptureState.RECORDING:
            forced_mode = CaptureMode.MANUAL

        if forced_mode is not None and forced_mode is not self._mode:
            self._mode = forced_mode
            old_blocked = self._mode_selector.blockSignals(True)
            try:
                self._mode_selector.set_current_index(_MODE_BY_INDEX.index(forced_mode))
            finally:
                self._mode_selector.blockSignals(old_blocked)

        self._mode_selector.setEnabled(state in (CaptureState.IDLE, CaptureState.ERROR))

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
            return (
                "Recording",
                f"{_format_elapsed(elapsed)} elapsed. Your session is writing to disk.",
            )
        if state is CaptureState.BUFFERING:
            return self._buffering_copy(seconds)
        if state is CaptureState.SAVING:
            return "Building clip", "Laying down one clean, constant-rate timeline."
        if state is CaptureState.ERROR:
            return (
                "Capture interrupted",
                self._last_error or "The capture engine stopped. Retry when ready.",
            )

        # IDLE — the copy depends on the selected mode.
        if self._mode is CaptureMode.REPLAY_BUFFER:
            return (
                "Replay is disarmed",
                f"Arm the buffer to keep the previous {seconds} seconds ready.",
            )
        return "Ready on command", "Start a full recording from here or from your hotkey."

    def _buffering_copy(self, window_seconds: int) -> tuple[str, str]:
        """Describe the replay window without overstating what is in it.

        The buffer starts empty and fills over ``window_seconds``. Quoting the
        configured target the instant it arms — which is what this screen used
        to do — tells the user they have thirty seconds banked when a save
        would actually produce two. The wording tracks the real fill instead,
        so the number on screen matches the clip they would get.

        When the engine cannot report telemetry we describe the *configuration*
        ("keeping the last N seconds ready") rather than asserting a current
        contents we have no way to know.
        """
        telemetry = self._telemetry
        if telemetry is None:
            return (
                "Replay buffer armed",
                f"Keeping the last {window_seconds} seconds ready.",
            )

        if telemetry.buffered_seconds <= 0:
            return (
                "Buffer warming up",
                "Arming — there is nothing to save yet.",
            )

        if telemetry.is_full:
            return (
                f"{telemetry.window_seconds} seconds ready",
                "The replay buffer is full. Save the moment without leaving the game.",
            )

        remaining = max(1, round(telemetry.window_seconds - telemetry.buffered_seconds))
        return (
            f"{int(telemetry.buffered_seconds)} of {telemetry.window_seconds} seconds buffered",
            f"Filling — the full window is ready in {remaining}s.",
        )

    def _shortcut_copy(self, state: CaptureState) -> tuple[str, str]:
        """Return the action label and key shown in the readout rail."""
        if state is CaptureState.RECORDING:
            return "STOP RECORDING", self._settings.record_hotkey.to_display()
        if state is CaptureState.BUFFERING:
            # The label names what the key would actually save. While the
            # buffer is still filling that is less than the configured window,
            # and promising the full amount here would undo the honesty of the
            # headline directly above it.
            telemetry = self._telemetry
            available = (
                int(min(telemetry.buffered_seconds, telemetry.window_seconds))
                if telemetry is not None
                else int(self._settings.replay_seconds)
            )
            return (
                f"SAVE LAST {max(1, available)} SECONDS",
                self._settings.clip_hotkey.to_display(),
            )
        if state is CaptureState.SAVING:
            return "FINALISING MP4", "BUSY"
        if state is CaptureState.ERROR:
            return "RETRY CAPTURE", "CLICK CONTROL"
        if self._mode is CaptureMode.REPLAY_BUFFER:
            return "ARM REPLAY", "CLICK CONTROL"
        return "TOGGLE RECORDING", self._settings.record_hotkey.to_display()

    def _orb_action_copy(self, state: CaptureState) -> str:
        """Return the short physical-action label beneath the orb."""
        if state is CaptureState.RECORDING:
            return "CLICK TO STOP"
        if state is CaptureState.BUFFERING:
            return "CLICK TO SAVE"
        if state is CaptureState.SAVING:
            return "FINALISING"
        if state is CaptureState.ERROR:
            return "CLICK TO RETRY"
        if self._mode is CaptureMode.REPLAY_BUFFER:
            return "CLICK TO ARM"
        return "CLICK TO RECORD"

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

    def _recent_tile_capacity(self) -> int:
        """How many recent-clip tiles fit the current width, at least one.

        The tiles are fixed-width, so a row of four cannot shrink: at a 960px
        window it claims about 790px against roughly 600px of usable page, and
        because the page is inside a width-resizable scroll area that minimum
        propagates outwards and drags *every* card wider than the viewport.
        The stage overflowed the window as a result. Fitting the row to the
        available width keeps the page inside its viewport at any size.
        """
        # Page padding on both sides, then the card's own padding on both.
        available = self.width() - 2 * SPACING_XL - 2 * SPACING_LG
        step = _TILE_WIDTH + SPACING_SM
        fits = (available + SPACING_SM) // step
        return max(1, min(_RECENT_CLIPS_LIMIT, fits))

    def _refresh_recent_clips(self) -> None:
        """Rescan the clip directories and rebuild the recent-clip tiles."""
        self._recent_capacity = self._recent_tile_capacity()
        clips = self._scan_recent_clips()[: self._recent_capacity]

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
