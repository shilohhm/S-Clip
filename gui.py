from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QSpinBox, QComboBox, QPushButton, QMessageBox, QSizePolicy
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QIcon
from capture import CaptureThread

class ClippingApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("S-Clip")
        self.setWindowIcon(QIcon("sclip_icon.png"))  # Ensure your icon file is in the project folder or adjust the path.
        self.setGeometry(100, 100, 1000, 700)  # Larger window size

        # Modern dark theme with dark gray background and dark purple accents,
        # using Roboto with fallback to Segoe UI.
        self.setStyleSheet("""
            QMainWindow {
                background-color: #2C2C2C;
            }
            QLabel { 
                color: #E0E0E0;
                font-size: 18px;
                font-family: 'Roboto', 'Segoe UI', sans-serif;
            }
            QLineEdit, QSpinBox, QComboBox, QPushButton {
                background-color: #3A3A3A;
                color: #E0E0E0;
                border: 2px solid #6200EE;
                border-radius: 5px;
                padding: 12px 16px;
                font-family: 'Roboto', 'Segoe UI', sans-serif;
                font-size: 16px;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox QAbstractItemView {
                background-color: #3A3A3A;
                color: #E0E0E0;
                selection-background-color: #6200EE;
                selection-color: #E0E0E0;
                border: 1px solid #6200EE;
                font-family: 'Roboto', 'Segoe UI', sans-serif;
                font-size: 16px;
            }
            QPushButton:hover {
                background-color: #6200EE;
            }
            QPushButton:pressed {
                background-color: #7F00FF;
            }
        """)

        # Default settings
        self.settings = {
            "resolution": "1920x1080",
            "fps": 30,
            "encoder": "libx264",
            "preset": "ultrafast",
            "hotkey": "F5"
        }
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setSpacing(20)
        main_layout.setContentsMargins(30, 30, 30, 30)

        # Title label
        title_label = QLabel("S-Clip")
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setFont(QFont("Roboto", 28, QFont.Bold))
        title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        main_layout.addWidget(title_label)

        # Settings layout for resolution, fps, encoder, preset, and hotkey
        settings_layout = QHBoxLayout()
        settings_layout.setSpacing(30)
        main_layout.addLayout(settings_layout)

        # Resolution (custom value via QLineEdit)
        res_layout = QVBoxLayout()
        res_label = QLabel("Resolution (WxH):")
        self.resolution_edit = QLineEdit("1920x1080")
        self.resolution_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        res_layout.addWidget(res_label)
        res_layout.addWidget(self.resolution_edit)
        settings_layout.addLayout(res_layout)

        # FPS (custom value via QSpinBox)
        fps_layout = QVBoxLayout()
        fps_label = QLabel("FPS:")
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 240)
        self.fps_spin.setValue(30)
        self.fps_spin.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        fps_layout.addWidget(fps_label)
        fps_layout.addWidget(self.fps_spin)
        settings_layout.addLayout(fps_layout)

        # Encoder (QComboBox)
        enc_layout = QVBoxLayout()
        enc_label = QLabel("Encoder:")
        self.encoder = QComboBox()
        self.encoder.addItems(["libx264", "h264_nvenc", "hevc_nvenc"])
        self.encoder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        enc_layout.addWidget(enc_label)
        enc_layout.addWidget(self.encoder)
        settings_layout.addLayout(enc_layout)

        # Preset (QComboBox with custom dropdown styling)
        preset_layout = QVBoxLayout()
        preset_label = QLabel("Preset:")
        self.preset = QComboBox()
        self.preset.addItems(["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "veryslow"])
        self.preset.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        preset_layout.addWidget(preset_label)
        preset_layout.addWidget(self.preset)
        settings_layout.addLayout(preset_layout)

        # Hotkey (customizable via QLineEdit)
        hotkey_layout = QVBoxLayout()
        hotkey_label = QLabel("Hotkey:")
        self.hotkey_edit = QLineEdit("F5")
        self.hotkey_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        hotkey_layout.addWidget(hotkey_label)
        hotkey_layout.addWidget(self.hotkey_edit)
        settings_layout.addLayout(hotkey_layout)

        # Buttons layout
        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(20)
        main_layout.addLayout(buttons_layout)

        save_button = QPushButton("Save Settings")
        save_button.clicked.connect(self.save_settings)
        buttons_layout.addWidget(save_button)

        record_button = QPushButton("Start Recording")
        record_button.clicked.connect(self.start_recording)
        buttons_layout.addWidget(record_button)

        stop_button = QPushButton("Stop Recording")
        stop_button.clicked.connect(self.stop_recording)
        buttons_layout.addWidget(stop_button)

        clip_button = QPushButton("Clip Last 30s")
        clip_button.clicked.connect(self.clip_last_30_seconds)
        buttons_layout.addWidget(clip_button)

        main_layout.addStretch()

        # Status label for feedback
        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setFont(QFont("Roboto", 16))
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        main_layout.addWidget(self.status_label)

        self.capture_thread = None

    def update_status(self, message):
        self.status_label.setText(message)

    def save_settings(self):
        self.settings["resolution"] = self.resolution_edit.text().strip()
        self.settings["fps"] = self.fps_spin.value()
        self.settings["encoder"] = self.encoder.currentText()
        self.settings["preset"] = self.preset.currentText()
        self.settings["hotkey"] = self.hotkey_edit.text().strip() or "F5"
        QMessageBox.information(self, "Settings Saved", "Settings have been updated!")
        self.update_status("Settings saved.")
        if self.capture_thread:
            self.capture_thread.stop_recording()
        self.capture_thread = CaptureThread(
            self.settings["resolution"],
            self.settings["fps"],
            self.settings["encoder"],
            preset=self.settings["preset"]
        )
        self.capture_thread.start()

    def start_recording(self):
        if not self.capture_thread:
            # Start continuous (manual) recording
            self.capture_thread = CaptureThread(
                self.resolution_edit.text().strip(),
                self.fps_spin.value(),
                self.encoder.currentText(),
                preset=self.preset.currentText(),
                mode="manual"
            )
            self.capture_thread.start()
            self.update_status("Recording started.")

    def stop_recording(self):
        if self.capture_thread:
            self.capture_thread.stop_recording()
            self.capture_thread = None
            self.update_status("Recording stopped.")

    def clip_last_30_seconds(self):
        # This creates a one-shot thread that captures a 30s clip.
        clip_thread = CaptureThread(
            self.resolution_edit.text().strip(),
            self.fps_spin.value(),
            self.encoder.currentText(),
            preset=self.preset.currentText(),
            mode="clip"
        )
        clip_thread.start()
        self.update_status("Clip captured.")

