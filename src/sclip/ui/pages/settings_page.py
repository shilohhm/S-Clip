"""Settings page - the configuration form.

Edits a working copy of :class:`Settings`, validates every field as the user
touches it, and only commits to the :class:`SettingsStore` when Save is hit.
Cancel reloads from the store so the form returns to the persisted state
without restarting the application.

Field validation surfaces inline (small red label below the offending
control) and the Save button is disabled while any field is invalid.

The page has two modes, chosen with a segmented control at the top:

* **Automatic** - S-Clip tunes the capture settings for the user's hardware.
  A read-only "Recommended setup" card summarises the detected configuration;
  the editable Video and Audio cards are hidden because the user is not meant
  to be hand-tuning capture here.
* **Advanced** - the full editable form. The Video and Audio cards are shown;
  the Recommended setup card is hidden.

In either mode the Replay buffer, Hotkeys and Storage cards stay visible -
those are user preferences rather than hardware-derived capture settings, so
it is sensible to edit them whichever mode is active. The chosen mode is
stored on :class:`Settings` as ``auto_configure`` and persisted on Save.

Layout note: the setting cards live inside a :class:`QScrollArea` so a short
window never crushes the form into an unreadable strip. The Save/Cancel footer
sits *outside* that scroll area, which keeps the primary actions pinned and
reachable no matter how far the user has scrolled.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sclip.contracts import (
    ENCODERS,
    DeviceRegistry,
    Hotkey,
    Settings,
    SettingsStore,
    encoder_by_codec,
    encoder_label,
)
from sclip.core.benchmark import EncoderTrial
from sclip.core.hardware import Recommendation, assess_settings, recommend_measured
from sclip.paths import app_paths
from sclip.ui.theme import SPACING_LG, SPACING_MD, SPACING_SM, SPACING_XL, SPACING_XS
from sclip.ui.widgets import Card, HotkeyEdit, IconButton, SegmentedControl

logger = logging.getLogger(__name__)


def _verdict_text(trial: EncoderTrial) -> str:
    """Turn a measurement into a sentence that says what to do about it."""
    if not trial.available:
        return (
            f"{trial.encoder} does not run on this PC. Pick another encoder, or use Automatic mode."
        )
    if trial.sustains_capture:
        return (
            f"Comfortable: {trial.encoder} {trial.preset} encodes "
            f"{trial.achieved_fps:.0f} fps at {trial.width}x{trial.height}, "
            f"{trial.headroom:.1f} times faster than the {trial.fps} fps you asked for."
        )
    # Naming the shortfall in frames-per-second is more use than a ratio,
    # because it is the same unit as the setting the user would change.
    return (
        f"Too slow: {trial.encoder} {trial.preset} manages only "
        f"{trial.achieved_fps:.0f} fps at {trial.width}x{trial.height}. Capturing "
        f"{trial.fps} fps needs more room than that once the game is running too, "
        "so clips will stutter. Try a faster preset, a hardware encoder, or a "
        "lower resolution."
    )


class _HardwareSignals(QObject):
    """Carrier so a runnable can hand its result back to the GUI thread.

    ``QRunnable`` is not a ``QObject`` and cannot own signals, so a small
    companion object bridges the two - the same arrangement the About page
    uses for its FFmpeg probe.
    """

    # Emits the worker's return value, or ``None`` if it raised.
    finished = Signal(object)


class _HardwareWorker(QRunnable):
    """Run one blocking hardware call on a worker thread.

    Both callers measure encoders, which means spawning FFmpeg several times
    over. On a machine with no usable GPU encoder the preset ladder is walked
    from ``medium`` downwards, so this can run for tens of seconds. Doing that
    on the GUI thread would freeze the window mid-measurement, and a frozen
    window during a benchmark looks exactly like a crash.
    """

    def __init__(self, work: Callable[[], object]) -> None:
        super().__init__()
        self._work = work
        self.signals = _HardwareSignals()

    def run(self) -> None:  # pragma: no cover - exercised at runtime only
        try:
            result = self._work()
        except Exception:
            # Hardware probing touches FFmpeg and the device layer, so it has
            # plenty of ways to fail. None of them should take the settings
            # page down: report nothing and leave the form as it was.
            logger.exception("Hardware measurement failed")
            self.signals.finished.emit(None)
            return
        self.signals.finished.emit(result)


# Width / height as ``WIDTHxHEIGHT`` - tolerant of stray spaces and the
# typographic multiplication sign so a paste from anywhere still validates.
_RESOLUTION_PATTERN = re.compile(r"^\s*(\d{2,5})\s*[xX\xd7]\s*(\d{2,5})\s*$")

# A consistent control height is what stops the form rows from collapsing:
# Qt happily shrinks an input to a couple of pixels inside a tight layout, so
# every editable widget gets pinned to this comfortable minimum.
_INPUT_HEIGHT: int = 34

# Width cap for ordinary fields (resolution, encoder, device combos). Wide
# enough for the longest device name the machine is likely to report.
_FIELD_MAX_WIDTH: int = 340

# Width cap for short numeric fields (frame rate, quality, buffer length).
# Wide enough for four digits plus a unit suffix and the stepper buttons.
_NUMERIC_INPUT_WIDTH: int = 150

# The label column is fixed-width so labels and inputs line up across cards.
_LABEL_COLUMN_WIDTH: int = 150

# The two mode indices on the segmented control. Naming them keeps the
# index/auto_configure mapping legible everywhere it is read.
_MODE_AUTOMATIC: int = 0
_MODE_ADVANCED: int = 1

# The mode toggle is left-aligned and width-capped: a full-width segmented
# control would read as a primary navigation strip rather than a small choice.
_MODE_TOGGLE_MAX_WIDTH: int = 320

# The caption under the mode toggle, keyed by mode index. It tells the user in
# one plain line who is in charge of the capture settings.
_MODE_CAPTIONS: dict[int, str] = {
    _MODE_AUTOMATIC: "S-Clip has tuned the capture settings for your hardware.",
    _MODE_ADVANCED: "You are managing every capture setting yourself.",
}


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ErrorLabels:
    """Bundle the small inline error labels so we can clear them in one go."""

    resolution: QLabel
    output_dir: QLabel
    clip_hotkey: QLabel
    record_hotkey: QLabel


class SettingsPage(QWidget):
    """User-editable settings, persisted via :class:`SettingsStore`."""

    settings_saved = Signal(object)  # carries the freshly saved Settings

    def __init__(
        self,
        settings_store: SettingsStore,
        device_registry: DeviceRegistry,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_store = settings_store
        self._device_registry = device_registry

        # The working copy the user is editing; not committed to disk until
        # they hit Save. Cancel discards it and reloads the persisted state.
        self._persisted: Settings = settings_store.load()
        self._working: Settings = self._persisted.copy()

        # Set true when any user-facing field is currently invalid; the Save
        # button is bound to its inverse.
        self._is_valid = True

        # Hardware measurement runs here rather than on the GUI thread. One
        # thread is enough and is also the point: a second concurrent
        # benchmark would contend with the first and measure them both slow.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._measuring = False

        # Created before the cards that host them: building a card populates
        # its fields, which fires the change handlers, which clear these.
        self._recommended_verdict = self._make_hint_label("")
        self._check_verdict = self._make_hint_label("")
        for label in (self._recommended_verdict, self._check_verdict):
            label.setVisible(False)
            # A verdict is a sentence or two rather than a caption, so it has
            # to wrap. Without this it lays out as one line and is clipped by
            # the card the moment the window is anything less than wide.
            label.setWordWrap(True)

        self._build_ui()
        self._populate_from_settings(self._working)
        self._validate_all()

    # ---------------------------------------------------- UI assembly

    def _build_ui(self) -> None:
        """Assemble the page: a scrolling card stack above a pinned footer.

        The root layout has zero margins - the scroll area paints edge to edge
        and the footer hugs the bottom of the window. All visible padding lives
        either inside the scroll content or inside the footer row.
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Field-level error labels are built before the cards so each card
        # builder can drop the relevant label straight into its grid.
        self._errors = _ErrorLabels(
            resolution=self._make_error_label(),
            output_dir=self._make_error_label(),
            clip_hotkey=self._make_error_label(),
            record_hotkey=self._make_error_label(),
        )

        # -- Scrolling card stack ------------------------------------------
        # Without setWidgetResizable(True) the content widget keeps its sizeHint
        # and the cards spill past the viewport instead of wrapping into a
        # scrollable column - that was the original layout bug.
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(SPACING_XL, SPACING_XL, SPACING_XL, SPACING_XL)
        content_layout.setSpacing(SPACING_LG)

        # Page title.
        title = QLabel("Settings", content)
        title.setProperty("role", "display")
        content_layout.addWidget(title)

        # -- Mode toggle ---------------------------------------------------
        # Sits directly under the title so the user picks who manages the
        # capture settings before reading any of the cards below.
        content_layout.addLayout(self._build_mode_toggle(content))

        # Cards are kept as attributes so the mode toggle can show/hide them.
        # The recommended-setup card is built first but only shown in
        # Automatic mode; the editable Video/Audio cards are its counterpart.
        self._recommended_card = self._build_recommended_card(content)
        self._video_card = self._build_video_card(content)
        self._audio_card = self._build_audio_card(content)

        content_layout.addWidget(self._recommended_card)
        content_layout.addWidget(self._video_card)
        content_layout.addWidget(self._audio_card)
        content_layout.addWidget(self._build_replay_card(content))
        content_layout.addWidget(self._build_hotkeys_card(content))
        content_layout.addWidget(self._build_storage_card(content))
        content_layout.addWidget(self._build_updates_card(content))

        # Apply the correct initial visibility now the cards exist. The
        # working copy's auto_configure flag decides which mode opens.
        self._apply_mode_visibility(self._mode_toggle.current_index)

        # The trailing stretch keeps the cards top-aligned: any spare vertical
        # room pools below the last card rather than inflating the gaps.
        content_layout.addStretch(1)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        # -- Hairline divider between the scroll area and the footer -------
        divider = QFrame(self)
        divider.setObjectName("Divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(divider)

        # -- Pinned footer -------------------------------------------------
        # Lives outside the scroll area on purpose: Save and Cancel must stay
        # visible and clickable however far the user has scrolled the cards.
        footer = QHBoxLayout()
        footer.setContentsMargins(SPACING_XL, SPACING_MD, SPACING_XL, SPACING_MD)
        footer.setSpacing(SPACING_SM)
        footer.addStretch(1)

        self._cancel_button = IconButton(text="Cancel", role="ghost", parent=self)
        self._cancel_button.clicked.connect(self._on_cancel)
        footer.addWidget(self._cancel_button)

        self._save_button = IconButton(text="Save", role="primary", parent=self)
        self._save_button.clicked.connect(self._on_save)
        footer.addWidget(self._save_button)

        root.addLayout(footer)

    def _make_error_label(self) -> QLabel:
        """Create a hidden inline error label.

        Styling is left entirely to the ``#FieldError`` QSS rule - keeping the
        colour out of Python means a theme swap recolours these for free.
        """
        label = QLabel("", self)
        label.setObjectName("FieldError")
        label.setWordWrap(True)
        label.setVisible(False)
        return label

    def _make_hint_label(self, text: str) -> QLabel:
        """Create a quiet helper-text label styled by the ``#FieldHint`` rule."""
        label = QLabel(text, self)
        label.setObjectName("FieldHint")
        return label

    def _build_mode_toggle(self, parent: QWidget) -> QVBoxLayout:
        """Build the Automatic/Advanced switch and its explanatory caption.

        The initial selection mirrors the working copy: Automatic when S-Clip
        is managing the settings (``auto_configure`` true), Advanced otherwise.
        """
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACING_XS)

        initial = _MODE_AUTOMATIC if self._working.auto_configure else _MODE_ADVANCED
        self._mode_toggle = SegmentedControl(
            ["Automatic", "Advanced"], current_index=initial, parent=parent
        )
        # Width-capped and left-aligned so it reads as a small local choice,
        # not a full-width navigation bar.
        self._mode_toggle.setMaximumWidth(_MODE_TOGGLE_MAX_WIDTH)
        self._mode_toggle.current_changed.connect(self._on_mode_changed)

        # An explicit left-aligned row stops the cap'd control from being
        # stretched or centred by the parent layout.
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(0, 0, 0, 0)
        toggle_row.addWidget(self._mode_toggle)
        toggle_row.addStretch(1)
        column.addLayout(toggle_row)

        # Caption explaining the active mode in one plain sentence.
        self._mode_caption = QLabel(_MODE_CAPTIONS[initial], parent)
        self._mode_caption.setProperty("role", "caption")
        self._mode_caption.setWordWrap(True)
        column.addWidget(self._mode_caption)

        return column

    def _build_recommended_card(self, parent: QWidget) -> Card:
        """Build the read-only summary of the auto-detected capture setup.

        Only shown in Automatic mode. The values are pulled from the working
        copy on demand via :meth:`_refresh_recommended_card`, so a re-detect
        updates them without rebuilding the card.
        """
        card = Card("Recommended setup", parent=parent)
        grid = self._make_card_grid(card)

        # Each summary row is a fixed label plus a value label. The value
        # labels are kept on the page so a re-detect can update them in place.
        self._recommended_values: dict[str, QLabel] = {}
        summary_keys = (
            "Encoder",
            "Display",
            "Resolution",
            "Frame rate",
            "Quality",
            "Microphone",
            "Desktop audio",
        )
        for row, key in enumerate(summary_keys):
            key_label = QLabel(key, card)
            key_label.setProperty("role", "label")
            value_label = QLabel("", card)
            value_label.setProperty("role", "value")
            value_label.setWordWrap(True)
            grid.addWidget(key_label, row, 0, Qt.AlignmentFlag.AlignVCenter)
            # These are read-only summary values, not inputs: they want the
            # input column *and* the gutter, so a long device name stays on one
            # line instead of wrapping inside a narrow field-width column.
            grid.addWidget(value_label, row, 1, 1, 2)
            self._recommended_values[key] = value_label

        # Re-detect action: a low-emphasis ghost button is right here - the
        # primary action on the page is still Save in the footer.
        self._redetect_button = IconButton(text="Benchmark this PC", role="ghost", parent=card)
        self._redetect_button.clicked.connect(self._on_redetect_hardware)
        redetect_row = QHBoxLayout()
        redetect_row.setContentsMargins(0, 0, 0, 0)
        redetect_row.addWidget(self._redetect_button)
        redetect_row.addStretch(1)
        card.body_layout().addLayout(redetect_row)

        benchmark_hint = self._make_hint_label(
            "Times each encoder on a short test clip at your display's resolution, "
            "then picks the best one that keeps up. Nothing is recorded from your screen."
        )
        benchmark_hint.setWordWrap(True)
        card.body_layout().addWidget(benchmark_hint)

        # Where the measurement result lands. Hidden until there is one.
        card.body_layout().addWidget(self._recommended_verdict)

        # Populate the value labels from the current working copy.
        self._refresh_recommended_card()
        return card

    @staticmethod
    def _make_card_grid(card: Card) -> QGridLayout:
        """Build the three-column grid that lays a card's fields out.

        Column 0 holds fixed-width labels, column 1 holds the input at its own
        comfortable width, and column 2 is an empty gutter that soaks up the
        slack. Adding rows to *this* grid - rather than stacking loose
        ``QHBoxLayout``s into the card's spacing-0 body - is what gives the
        form readable, evenly spaced rows.

        The gutter is load-bearing. Fields are width-capped, so if the stretch
        sat on the input column instead, every column would have a maximum, the
        grid's own maximum would fall below the card width, and Qt would centre
        the whole form in the middle of the card - labels and all - rather than
        leaving it anchored to the left edge.
        """
        grid = QGridLayout()
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(2, 1)
        grid.setColumnMinimumWidth(0, _LABEL_COLUMN_WIDTH)
        grid.setHorizontalSpacing(SPACING_MD)
        grid.setVerticalSpacing(SPACING_SM)
        card.body_layout().addLayout(grid)
        return grid

    def _add_field_row(
        self,
        grid: QGridLayout,
        row: int,
        label_text: str,
        field: QWidget,
    ) -> None:
        """Place a label/field pair on grid ``row`` (label left, field right)."""
        label = QLabel(label_text, field.parentWidget())
        label.setObjectName("FieldLabel")
        # Top-align the label so multi-line inputs (or a tall composite widget)
        # do not drag the caption down to their vertical centre.
        grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(field, row, 1)

    @staticmethod
    def _add_spanning_widget(grid: QGridLayout, row: int, widget: QWidget) -> None:
        """Place ``widget`` across every column of grid ``row``.

        Used for check boxes and for inline error/hint labels, which read
        better spanning the full card width than squeezed into one column.
        """
        grid.addWidget(widget, row, 0, 1, 3)

    @staticmethod
    def _size_input(widget: QWidget) -> QWidget:
        """Pin an editable control to a consistent height and a readable width.

        A text field stretched across the full width of a card is harder to
        scan, not easier - the value sits marooned at the left of a long empty
        trough. Capping the field and letting the grid's gutter take the slack
        keeps the form a readable column.
        """
        widget.setMinimumHeight(_INPUT_HEIGHT)
        widget.setMaximumWidth(_FIELD_MAX_WIDTH)
        return widget

    @staticmethod
    def _size_numeric_input(widget: QWidget) -> QWidget:
        """Size a short numeric control: full height, but a modest width.

        Every field sits in the grid's stretching second column, so without a
        cap a spin box showing "30 s" sprawls the full width of the card -
        several hundred pixels of chrome around two characters. Paths, device
        names and resolutions still want the whole column; only the short
        numeric fields are reined in.
        """
        widget.setMinimumHeight(_INPUT_HEIGHT)
        widget.setMaximumWidth(_NUMERIC_INPUT_WIDTH)
        return widget

    def _build_video_card(self, parent: QWidget) -> Card:
        card = Card("Video", parent=parent)
        grid = self._make_card_grid(card)

        # Resolution - validated live against the WIDTHxHEIGHT pattern.
        self._resolution_edit = QLineEdit(card)
        self._resolution_edit.setPlaceholderText("1920x1080")
        self._size_input(self._resolution_edit)
        self._resolution_edit.textChanged.connect(self._on_resolution_changed)
        self._add_field_row(grid, 0, "Resolution", self._resolution_edit)
        self._add_spanning_widget(grid, 1, self._errors.resolution)

        # Frame rate.
        self._fps_spin = QSpinBox(card)
        self._fps_spin.setRange(1, 240)
        self._fps_spin.setSuffix(" fps")
        self._size_numeric_input(self._fps_spin)
        self._fps_spin.valueChanged.connect(self._on_fps_changed)
        self._add_field_row(grid, 2, "Frame rate", self._fps_spin)

        # Encoder.
        self._encoder_combo = QComboBox(card)
        for spec in ENCODERS:
            display = spec.codec + (" (GPU)" if spec.needs_gpu else "")
            self._encoder_combo.addItem(display, spec.codec)
        self._size_input(self._encoder_combo)
        self._encoder_combo.currentIndexChanged.connect(self._on_encoder_changed)
        self._add_field_row(grid, 3, "Encoder", self._encoder_combo)

        # Preset - repopulated whenever the encoder changes.
        self._preset_combo = QComboBox(card)
        self._size_input(self._preset_combo)
        self._preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self._add_field_row(grid, 4, "Preset", self._preset_combo)

        # Quality (CRF), with a hint clarifying the unintuitive scale.
        self._quality_spin = QSpinBox(card)
        self._quality_spin.setRange(0, 51)
        self._size_numeric_input(self._quality_spin)
        self._quality_spin.valueChanged.connect(self._on_quality_changed)
        self._add_field_row(grid, 5, "Quality", self._quality_spin)
        self._add_spanning_widget(grid, 6, self._make_hint_label("lower = better quality"))

        # Monitor.
        self._monitor_combo = QComboBox(card)
        self._size_input(self._monitor_combo)
        self._populate_monitor_combo()
        self._monitor_combo.currentIndexChanged.connect(self._on_monitor_changed)
        self._add_field_row(grid, 7, "Monitor", self._monitor_combo)

        # Advanced mode accepts any combination of these fields, including
        # ones this machine cannot sustain. That failure is silent - the clip
        # saves, it just stutters - so there has to be a way to ask.
        self._check_button = IconButton(text="Test this setup", role="ghost", parent=card)
        self._check_button.clicked.connect(self._on_check_setup)
        check_row = QHBoxLayout()
        check_row.setContentsMargins(0, 0, 0, 0)
        check_row.addWidget(self._check_button)
        check_row.addStretch(1)
        card.body_layout().addLayout(check_row)

        card.body_layout().addWidget(self._check_verdict)

        return card

    def _build_audio_card(self, parent: QWidget) -> Card:
        card = Card("Audio", parent=parent)
        grid = self._make_card_grid(card)

        # The master toggle spans the full width; the microphone picker and
        # the desktop-audio switch below are gated on it.
        self._capture_audio_check = QCheckBox("Capture audio", card)
        self._capture_audio_check.toggled.connect(self._on_capture_audio_toggled)
        self._add_spanning_widget(grid, 0, self._capture_audio_check)

        self._mic_combo = QComboBox(card)
        self._size_input(self._mic_combo)
        self._mic_combo.currentIndexChanged.connect(self._on_mic_changed)
        self._add_field_row(grid, 1, "Microphone", self._mic_combo)

        # Desktop audio is captured through the Windows WASAPI loopback API,
        # not a DirectShow device, so it is a plain on/off switch - there is
        # no device for the user to pick.
        self._capture_desktop_check = QCheckBox("Capture desktop audio (system sound)", card)
        self._capture_desktop_check.toggled.connect(self._on_capture_desktop_toggled)
        self._add_spanning_widget(grid, 2, self._capture_desktop_check)
        self._add_spanning_widget(
            grid,
            3,
            self._make_hint_label(
                "Captures whatever your speakers are playing - game, voice chat, music."
            ),
        )

        self._populate_audio_combos()
        return card

    def _build_replay_card(self, parent: QWidget) -> Card:
        card = Card("Replay buffer", parent=parent)
        grid = self._make_card_grid(card)

        self._replay_buffer_check = QCheckBox("Enable replay buffer", card)
        self._replay_buffer_check.toggled.connect(self._on_replay_buffer_toggled)
        self._add_spanning_widget(grid, 0, self._replay_buffer_check)

        self._replay_seconds_spin = QSpinBox(card)
        self._replay_seconds_spin.setRange(5, 600)
        self._replay_seconds_spin.setSuffix(" s")
        self._size_numeric_input(self._replay_seconds_spin)
        self._replay_seconds_spin.valueChanged.connect(self._on_replay_seconds_changed)
        self._add_field_row(grid, 1, "Buffer length", self._replay_seconds_spin)

        return card

    def _build_hotkeys_card(self, parent: QWidget) -> Card:
        card = Card("Hotkeys", parent=parent)
        grid = self._make_card_grid(card)

        # The real HotkeyEdit widget already enforces a sensible minimum
        # height, so it does not need _size_input.
        self._clip_hotkey_widget = HotkeyEdit(parent=card)
        self._clip_hotkey_widget.hotkey_changed.connect(self._on_clip_hotkey_changed)
        self._add_field_row(grid, 0, "Save replay clip", self._clip_hotkey_widget)
        self._add_spanning_widget(grid, 1, self._errors.clip_hotkey)

        self._record_hotkey_widget = HotkeyEdit(parent=card)
        self._record_hotkey_widget.hotkey_changed.connect(self._on_record_hotkey_changed)
        self._add_field_row(grid, 2, "Toggle recording", self._record_hotkey_widget)
        self._add_spanning_widget(grid, 3, self._errors.record_hotkey)

        return card

    def _build_storage_card(self, parent: QWidget) -> Card:
        card = Card("Storage", parent=parent)
        grid = self._make_card_grid(card)

        # The clips-folder field is a composite: a read-only path display plus
        # two ghost actions, all packed into column 1 on a single grid row.
        folder_widget = QWidget(card)
        folder_layout = QHBoxLayout(folder_widget)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.setSpacing(SPACING_XS)

        self._output_dir_edit = QLineEdit(folder_widget)
        self._output_dir_edit.setReadOnly(True)
        self._output_dir_edit.setPlaceholderText("(default location)")
        self._size_input(self._output_dir_edit)
        folder_layout.addWidget(self._output_dir_edit, 1)

        change_button = IconButton(text="Change…", role="ghost", parent=folder_widget)
        change_button.clicked.connect(self._on_change_output_dir)
        folder_layout.addWidget(change_button)

        reset_button = IconButton(text="Use default", role="ghost", parent=folder_widget)
        reset_button.clicked.connect(self._on_reset_output_dir)
        folder_layout.addWidget(reset_button)

        self._add_field_row(grid, 0, "Clips folder", folder_widget)
        self._add_spanning_widget(grid, 1, self._errors.output_dir)

        return card

    def _build_updates_card(self, parent: QWidget) -> Card:
        """Build the update-check switch.

        This is the only thing S-Clip does over the network, which is exactly
        why it gets a visible switch and a plain description rather than being
        assumed. Someone who would rather the application never contacted
        anything should be able to see that choice and make it.
        """
        card = Card("Updates", parent=parent)
        grid = self._make_card_grid(card)

        self._check_updates_check = QCheckBox("Tell me when a new version is released", card)
        self._check_updates_check.toggled.connect(self._on_check_updates_toggled)
        self._add_spanning_widget(grid, 0, self._check_updates_check)

        hint = self._make_hint_label(
            "Asks GitHub once a day whether a newer release exists, and shows a link if "
            "one does. Nothing is downloaded or installed, and nothing about you or your "
            "PC is sent. This is the only time S-Clip uses the network."
        )
        hint.setWordWrap(True)
        self._add_spanning_widget(grid, 1, hint)

        return card

    # ---------------------------------------------------- Population

    def _populate_monitor_combo(self) -> None:
        self._monitor_combo.blockSignals(True)
        self._monitor_combo.clear()
        try:
            monitors = self._device_registry.monitors()
        except Exception:
            logger.exception("Could not enumerate monitors")
            monitors = []
        for monitor in monitors:
            label = f"{monitor.name} ({monitor.width}x{monitor.height})"
            self._monitor_combo.addItem(label, monitor.name)
        if not monitors:
            # Keep the form usable when the registry is empty (headless test
            # mode etc.) - the persisted value still shows up below.
            self._monitor_combo.addItem("Monitor 1", "Monitor 1")
        self._monitor_combo.blockSignals(False)

    def _populate_audio_combos(self) -> None:
        """Fill the microphone picker from the enumerated capture devices."""
        self._mic_combo.blockSignals(True)
        self._mic_combo.clear()
        self._mic_combo.addItem("None", "")

        try:
            devices = self._device_registry.audio_devices()
        except Exception:
            logger.exception("Could not enumerate audio devices")
            devices = []

        for device in devices:
            if device.kind == "input":
                self._mic_combo.addItem(device.name, device.name)

        self._mic_combo.blockSignals(False)

    def _populate_preset_combo(self, codec: str) -> None:
        self._preset_combo.blockSignals(True)
        self._preset_combo.clear()
        spec = encoder_by_codec(codec)
        if spec is not None:
            for preset in spec.presets:
                self._preset_combo.addItem(preset, preset)
        self._preset_combo.blockSignals(False)

    def _populate_from_settings(self, settings: Settings) -> None:
        """Mirror the working settings into every widget on the page."""
        # Video.
        self._resolution_edit.blockSignals(True)
        self._resolution_edit.setText(settings.resolution)
        self._resolution_edit.blockSignals(False)

        self._fps_spin.blockSignals(True)
        self._fps_spin.setValue(settings.fps)
        self._fps_spin.blockSignals(False)

        self._set_combo_to_value(self._encoder_combo, settings.encoder)
        # Preset combo follows encoder.
        self._populate_preset_combo(settings.encoder)
        self._set_combo_to_value(self._preset_combo, settings.preset)

        self._quality_spin.blockSignals(True)
        self._quality_spin.setValue(settings.crf)
        self._quality_spin.blockSignals(False)

        self._set_combo_to_value(self._monitor_combo, settings.monitor)

        # Audio.
        self._capture_audio_check.blockSignals(True)
        self._capture_audio_check.setChecked(settings.capture_audio)
        self._capture_audio_check.blockSignals(False)

        self._capture_desktop_check.blockSignals(True)
        self._capture_desktop_check.setChecked(settings.capture_desktop_audio)
        self._capture_desktop_check.blockSignals(False)

        self._apply_audio_enabled_state(settings.capture_audio)

        # Updates.
        self._check_updates_check.blockSignals(True)
        self._check_updates_check.setChecked(settings.check_for_updates)
        self._check_updates_check.blockSignals(False)
        self._set_combo_to_value(self._mic_combo, settings.audio_input or "")

        # Replay buffer.
        self._replay_buffer_check.blockSignals(True)
        self._replay_buffer_check.setChecked(settings.replay_buffer)
        self._replay_buffer_check.blockSignals(False)
        self._replay_seconds_spin.blockSignals(True)
        self._replay_seconds_spin.setValue(settings.replay_seconds)
        self._replay_seconds_spin.blockSignals(False)
        self._replay_seconds_spin.setEnabled(settings.replay_buffer)

        # Hotkeys - set_hotkey does not re-emit, so no signal blocking needed.
        self._clip_hotkey_widget.set_hotkey(settings.clip_hotkey)
        self._record_hotkey_widget.set_hotkey(settings.record_hotkey)

        # Storage.
        self._output_dir_edit.blockSignals(True)
        self._output_dir_edit.setText(settings.output_dir or "")
        self._output_dir_edit.blockSignals(False)

    @staticmethod
    def _set_combo_to_value(combo: QComboBox, value: Any) -> None:
        """Select an item by its userData; appends a new item if not present."""
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)
                return
        # Persisted value isn't in the populated list (device unplugged?).
        # Add it so the user can still see what is configured.
        if value:
            combo.blockSignals(True)
            combo.addItem(str(value), value)
            combo.setCurrentIndex(combo.count() - 1)
            combo.blockSignals(False)

    # ---------------------------------------------------- Field slots

    def _on_resolution_changed(self, value: str) -> None:
        self._working.resolution = value
        self._validate_resolution()
        self._invalidate_verdict()
        self._update_save_state()

    def _on_fps_changed(self, value: int) -> None:
        self._working.fps = int(value)
        self._invalidate_verdict()
        self._update_save_state()

    def _on_encoder_changed(self, _index: int) -> None:
        codec = self._encoder_combo.currentData()
        if codec is None:
            return
        self._working.encoder = str(codec)
        self._invalidate_verdict()
        self._populate_preset_combo(self._working.encoder)
        # Pick the first preset on a fresh encoder; the user can change it.
        if self._preset_combo.count() > 0:
            self._working.preset = str(self._preset_combo.itemData(0) or "")
            self._preset_combo.blockSignals(True)
            self._preset_combo.setCurrentIndex(0)
            self._preset_combo.blockSignals(False)
        self._update_save_state()

    def _on_preset_changed(self, _index: int) -> None:
        data = self._preset_combo.currentData()
        if data is None:
            return
        self._working.preset = str(data)
        self._invalidate_verdict()
        self._update_save_state()

    def _on_quality_changed(self, value: int) -> None:
        self._working.crf = int(value)
        self._invalidate_verdict()
        self._update_save_state()

    def _on_monitor_changed(self, _index: int) -> None:
        data = self._monitor_combo.currentData()
        if data is None:
            return
        self._working.monitor = str(data)
        self._update_save_state()

    def _on_check_updates_toggled(self, checked: bool) -> None:
        self._working.check_for_updates = bool(checked)
        self._update_save_state()

    def _on_capture_audio_toggled(self, checked: bool) -> None:
        self._working.capture_audio = bool(checked)
        self._apply_audio_enabled_state(checked)
        self._update_save_state()

    def _apply_audio_enabled_state(self, enabled: bool) -> None:
        """Grey out the per-source audio controls when capture is switched off."""
        self._mic_combo.setEnabled(enabled)
        self._capture_desktop_check.setEnabled(enabled)

    def _on_mic_changed(self, _index: int) -> None:
        data = self._mic_combo.currentData()
        self._working.audio_input = "" if data is None else str(data)
        self._update_save_state()

    def _on_capture_desktop_toggled(self, checked: bool) -> None:
        self._working.capture_desktop_audio = bool(checked)
        self._update_save_state()

    def _on_replay_buffer_toggled(self, checked: bool) -> None:
        self._working.replay_buffer = bool(checked)
        self._replay_seconds_spin.setEnabled(checked)
        self._update_save_state()

    def _on_replay_seconds_changed(self, value: int) -> None:
        self._working.replay_seconds = int(value)
        self._update_save_state()

    def _on_clip_hotkey_changed(self, hotkey: Hotkey) -> None:
        self._working.clip_hotkey = hotkey
        self._validate_clip_hotkey()
        # The record hotkey's validity depends on this one (chord-clash check),
        # so re-run it whenever the clip hotkey moves.
        self._validate_record_hotkey()
        self._update_save_state()

    def _on_record_hotkey_changed(self, hotkey: Hotkey) -> None:
        self._working.record_hotkey = hotkey
        self._validate_record_hotkey()
        self._update_save_state()

    def _on_change_output_dir(self) -> None:
        start = self._working.output_dir or str(app_paths().clips_dir)
        chosen = QFileDialog.getExistingDirectory(self, "Choose clips folder", start)
        if not chosen:
            return
        self._working.output_dir = chosen
        self._output_dir_edit.blockSignals(True)
        self._output_dir_edit.setText(chosen)
        self._output_dir_edit.blockSignals(False)
        self._validate_output_dir()
        self._update_save_state()

    def _on_reset_output_dir(self) -> None:
        self._working.output_dir = ""
        self._output_dir_edit.blockSignals(True)
        self._output_dir_edit.clear()
        self._output_dir_edit.blockSignals(False)
        self._validate_output_dir()
        self._update_save_state()

    # ---------------------------------------------------- Mode handling

    def _on_mode_changed(self, index: int) -> None:
        """React to the Automatic/Advanced toggle moving.

        Records the choice on the working copy (Automatic means S-Clip
        manages the capture settings), swaps the visible cards, and updates
        the caption. The save state is re-evaluated because the hidden cards
        keep whatever validity they last had.
        """
        self._working.auto_configure = index == _MODE_AUTOMATIC
        self._apply_mode_visibility(index)
        self._mode_caption.setText(_MODE_CAPTIONS.get(index, _MODE_CAPTIONS[_MODE_ADVANCED]))
        self._update_save_state()

    def _apply_mode_visibility(self, index: int) -> None:
        """Show the cards that belong to ``index`` and hide the rest.

        Automatic mode shows the read-only Recommended setup card and hides
        the editable Video and Audio cards. Advanced mode does the reverse.
        The Replay buffer, Hotkeys and Storage cards are deliberately left
        untouched - they are user preferences, fine to edit in either mode.
        """
        is_automatic = index == _MODE_AUTOMATIC
        self._recommended_card.setVisible(is_automatic)
        self._video_card.setVisible(not is_automatic)
        self._audio_card.setVisible(not is_automatic)

    def _refresh_recommended_card(self) -> None:
        """Mirror the working copy into the read-only summary value labels."""
        working = self._working
        self._recommended_values["Encoder"].setText(encoder_label(working.encoder))
        self._recommended_values["Display"].setText(working.monitor)
        self._recommended_values["Resolution"].setText(working.resolution)
        self._recommended_values["Frame rate"].setText(f"{working.fps} fps")
        self._recommended_values["Quality"].setText(f"CQ {working.crf}")
        self._recommended_values["Microphone"].setText(working.audio_input or "Not captured")
        self._recommended_values["Desktop audio"].setText(
            "System sound (WASAPI loopback)" if working.capture_desktop_audio else "Off"
        )

    def _on_redetect_hardware(self) -> None:
        """Benchmark the machine and adopt the configuration it can sustain.

        Unlike the recommendation made on first launch, this one is measured:
        each candidate encoder is timed at the display it would actually be
        capturing. That is worth seconds of the user's time precisely because
        they asked for it, and it is the difference between "this encoder
        exists" and "this encoder keeps up".

        The work runs on a worker thread and the result arrives at
        :meth:`_on_hardware_measured`.
        """
        if self._measuring:
            return
        base = self._working.copy()
        registry = self._device_registry
        self._begin_measuring("Measuring your hardware...")
        worker = _HardwareWorker(lambda: recommend_measured(base, registry))
        worker.signals.finished.connect(self._on_hardware_measured)
        self._pool.start(worker)

    def _on_hardware_measured(self, recommendation: object) -> None:
        """Adopt a freshly measured configuration, or leave the form alone."""
        self._end_measuring()
        if not isinstance(recommendation, Recommendation):
            # The worker already logged the cause. Saying so beats silently
            # leaving the button looking like it did nothing.
            self._set_benchmark_verdict("Could not measure this machine. Settings are unchanged.")
            return
        self._working = recommendation.settings
        # Repopulate the full form so Advanced mode reflects the new values
        # too, then refresh this card's summary and re-run validation.
        self._populate_from_settings(self._working)
        self._refresh_recommended_card()
        self._validate_all()
        # Report the measurement, not just its consequence. Having waited for a
        # benchmark, the user should be able to see what it found.
        trial = recommendation.trial
        if trial is None:
            self._set_benchmark_verdict(
                "Nothing on this PC could keep up with your display, so the fastest "
                "available setup was chosen. Expect some dropped frames.",
                is_warning=True,
            )
        else:
            self._set_benchmark_verdict(_verdict_text(trial))

    # ------------------------------------------- Advanced-mode benchmark

    def _on_check_setup(self) -> None:
        """Measure whether the user's own choices can sustain a capture.

        Advanced mode will accept any combination of encoder, preset,
        resolution and frame rate, and an over-ambitious one does not announce
        itself: the capture still runs and the clip still saves, it just
        stutters, because frames that missed their deadline were replaced by
        repeats. This puts a number on it before that happens.
        """
        if self._measuring:
            return
        candidate = self._working.copy()
        self._begin_measuring("Measuring...")
        worker = _HardwareWorker(lambda: assess_settings(candidate))
        worker.signals.finished.connect(self._on_setup_checked)
        self._pool.start(worker)

    def _on_setup_checked(self, trial: object) -> None:
        """Report what the chosen configuration actually managed."""
        self._end_measuring()
        if not isinstance(trial, EncoderTrial):
            self._set_benchmark_verdict("Could not measure this setup.")
            return
        self._set_benchmark_verdict(_verdict_text(trial), is_warning=not trial.sustains_capture)

    def _begin_measuring(self, message: str) -> None:
        """Enter the measuring state: buttons disabled, progress shown."""
        self._measuring = True
        self._redetect_button.setEnabled(False)
        self._check_button.setEnabled(False)
        self._set_benchmark_verdict(message)

    def _end_measuring(self) -> None:
        self._measuring = False
        self._redetect_button.setEnabled(True)
        self._check_button.setEnabled(True)

    def _invalidate_verdict(self) -> None:
        """Drop a measurement that no longer describes the form.

        Changing the encoder, preset, resolution, frame rate or quality
        invalidates whatever was measured before it. Leaving a stale
        "Comfortable" sitting under settings it was never taken against would
        be worse than showing nothing, because the user would believe it.
        """
        if self._measuring:
            # A measurement in flight owns the label; it will write the result.
            return
        self._set_benchmark_verdict("")

    def _set_benchmark_verdict(self, text: str, *, is_warning: bool = False) -> None:
        """Show a measurement result on both cards, or clear it when empty."""
        for label in (self._recommended_verdict, self._check_verdict):
            label.setText(text)
            label.setVisible(bool(text))
            # Re-polish so the stylesheet picks up the changed role.
            label.setProperty("role", "warning" if is_warning else None)
            style = label.style()
            style.unpolish(label)
            style.polish(label)

    # ---------------------------------------------------- Save / Cancel

    def _on_save(self) -> None:
        if not self._validate_all():
            return
        try:
            self._settings_store.save(self._working)
        except Exception as exc:
            logger.exception("Failed to persist settings")
            self._errors.output_dir.setText(f"Could not save: {exc}")
            self._errors.output_dir.setVisible(True)
            return
        self._persisted = self._working.copy()
        # Reload the working copy from the freshly persisted snapshot so the
        # next Cancel returns to this same baseline.
        self._working = self._persisted.copy()
        self.settings_saved.emit(self._persisted)

    def _on_cancel(self) -> None:
        # Reload from the persisted store - this is more honest than copying
        # _persisted because the user may have edited via another window.
        try:
            self._persisted = self._settings_store.load()
        except Exception:
            logger.exception("Could not reload settings on cancel")
            # Fall back to the in-memory copy.
        self._working = self._persisted.copy()
        self._populate_from_settings(self._working)
        # Restore the mode too: the user may have flipped the toggle before
        # cancelling. SegmentedControl drops a redundant select, so when the
        # mode has not actually changed this is a no-op and emits nothing.
        self._restore_mode_from_working()
        self._validate_all()

    def _restore_mode_from_working(self) -> None:
        """Move the toggle and refresh the mode UI to match the working copy.

        Used by Cancel. ``set_current_index`` only emits ``current_changed``
        when the index genuinely moves, so the caption and card visibility
        are refreshed here explicitly to cover the no-move case as well.
        """
        target = _MODE_AUTOMATIC if self._working.auto_configure else _MODE_ADVANCED
        self._mode_toggle.set_current_index(target)
        self._apply_mode_visibility(target)
        self._mode_caption.setText(_MODE_CAPTIONS[target])
        self._refresh_recommended_card()

    # ---------------------------------------------------- Validation

    def _validate_resolution(self) -> bool:
        text = self._resolution_edit.text()
        match = _RESOLUTION_PATTERN.match(text)
        if match is None:
            self._show_error(self._errors.resolution, "Use WIDTHxHEIGHT, e.g. 1920x1080.")
            return False
        width, height = int(match.group(1)), int(match.group(2))
        if width <= 0 or height <= 0:
            self._show_error(self._errors.resolution, "Width and height must be positive.")
            return False
        if width % 2 or height % 2:
            self._show_error(
                self._errors.resolution,
                "Most encoders need even dimensions - try a multiple of two.",
            )
            return False
        # Normalise the working copy so the saved value is consistent ("x" not "X").
        self._working.resolution = f"{width}x{height}"
        self._clear_error(self._errors.resolution)
        return True

    def _validate_output_dir(self) -> bool:
        value = self._output_dir_edit.text().strip()
        if not value:
            # Blank means "use default" - always valid.
            self._clear_error(self._errors.output_dir)
            return True
        path = Path(value)
        if not path.exists():
            self._show_error(self._errors.output_dir, "That folder does not exist yet.")
            return False
        if not path.is_dir():
            self._show_error(self._errors.output_dir, "Please pick a folder, not a file.")
            return False
        self._clear_error(self._errors.output_dir)
        return True

    def _validate_clip_hotkey(self) -> bool:
        hotkey = self._clip_hotkey_widget.hotkey()
        if hotkey is None:
            self._show_error(
                self._errors.clip_hotkey,
                "Enter a key, optionally with Ctrl/Shift/Alt.",
            )
            return False
        self._clear_error(self._errors.clip_hotkey)
        return True

    def _validate_record_hotkey(self) -> bool:
        hotkey = self._record_hotkey_widget.hotkey()
        if hotkey is None:
            self._show_error(
                self._errors.record_hotkey,
                "Enter a key, optionally with Ctrl/Shift/Alt.",
            )
            return False
        # Soft warning when the two hotkeys collide - same chord can't drive both.
        if self._working.clip_hotkey == hotkey:
            self._show_error(
                self._errors.record_hotkey,
                "This chord is already bound to Save replay clip.",
            )
            return False
        self._clear_error(self._errors.record_hotkey)
        return True

    def _validate_all(self) -> bool:
        results = [
            self._validate_resolution(),
            self._validate_output_dir(),
            self._validate_clip_hotkey(),
            self._validate_record_hotkey(),
        ]
        self._is_valid = all(results)
        self._update_save_state()
        return self._is_valid

    def _show_error(self, label: QLabel, message: str) -> None:
        label.setText(message)
        label.setVisible(True)

    def _clear_error(self, label: QLabel) -> None:
        label.clear()
        label.setVisible(False)

    def _update_save_state(self) -> None:
        self._save_button.setEnabled(self._is_valid)


__all__ = ["SettingsPage"]
