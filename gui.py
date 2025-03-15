import json
import re
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QLineEdit, QSpinBox, QComboBox, QPushButton, QMessageBox, 
    QSizePolicy, QCheckBox, QApplication, QGroupBox, QFormLayout
)
from PyQt5.QtCore import Qt, QFile, QTextStream
from PyQt5.QtGui import QFont, QIcon, QPixmap
from capture import CaptureThread
import settings

class ClippingApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("S-Clip")
        self.setWindowIcon(QIcon("sclip_icon.png"))
        self.setGeometry(100, 100, 1000, 700)

        self.settings = settings.load_settings()

        # Encoder Preset Dictionary
        self.encoder_presets = {
            "libx264": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "veryslow"],
            "h264_nvenc": ["default", "hp", "llhp", "fast", "medium", "slow", "bq", "hq", "ll", "llhq", "lossless", "losslesshp"],
            "hevc_nvenc": ["fast", "medium", "slow"]
        }

        self.load_styles()
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setSpacing(20)
        main_layout.setContentsMargins(40, 40, 40, 40)

        # Title Label
        logo_widget = QWidget()
        logo_layout = QHBoxLayout(logo_widget)  
        logo_layout.setAlignment(Qt.AlignCenter)  
        # Create the QLabel for the image
        title_label = QLabel(self)
        title_label.setObjectName("title_label")  
        pixmap = QPixmap("Rose -4.png")  

        # Scale the image so it's not cut off
        title_label.setPixmap(pixmap.scaled(200, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        title_label.setAlignment(Qt.AlignCenter)  
        # Add the label to the layout
        logo_layout.addWidget(title_label)
        main_layout.addWidget(logo_widget)
                

        # Status Bar (Live Updates)
        self.status_bar = QLabel("🔴 Idle")
        self.status_bar.setAlignment(Qt.AlignCenter)
        self.status_bar.setFont(QFont("Segoe UI", 14))
        self.status_bar.setStyleSheet("color: #DC143C; background-color: #333; padding: 10px; border-radius: 5px;")
        main_layout.addWidget(self.status_bar)

        # Video Settings Group
        video_group = QGroupBox("🎥 Video Settings")
        video_layout = QFormLayout()
        
        self.resolution_edit = QLineEdit(self.settings["resolution"])
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 240)
        self.fps_spin.setValue(self.settings["fps"])
        
        self.encoder = QComboBox()
        self.encoder.addItems(self.encoder_presets.keys())  
        self.encoder.setCurrentText(self.settings["encoder"])
        self.encoder.currentTextChanged.connect(self.update_presets)  

        self.preset = QComboBox()
        self.update_presets()  
        self.preset.setCurrentText(self.settings["preset"])

        self.monitor_combo = QComboBox()
        self.monitor_combo.addItems(CaptureThread.list_monitors().keys())
        self.monitor_combo.setCurrentText(self.settings.get("monitor", "Monitor 1"))

        video_layout.addRow("Resolution:", self.resolution_edit)
        video_layout.addRow("FPS:", self.fps_spin)
        video_layout.addRow("Encoder:", self.encoder)
        video_layout.addRow("Preset:", self.preset)
        video_layout.addRow("Monitor:", self.monitor_combo)
        video_group.setLayout(video_layout)
        main_layout.addWidget(video_group)

        # Audio Settings Group
        audio_group = QGroupBox("🎵 Audio Settings")
        audio_layout = QFormLayout()

        self.audio_input_combo = QComboBox()
        self.audio_input_combo.addItems(CaptureThread.list_audio_devices())
        self.audio_input_combo.setCurrentText(self.settings.get("audio_device", ""))

        self.audio_output_combo = QComboBox()
        self.audio_output_combo.addItems(CaptureThread.list_audio_devices())

        audio_layout.addRow("Audio Input:", self.audio_input_combo)
        audio_layout.addRow("Audio Output:", self.audio_output_combo)
        audio_group.setLayout(audio_layout)
        main_layout.addWidget(audio_group)

        # Replay Buffer Checkbox
        self.replay_buffer_checkbox = QCheckBox("Enable Replay Buffer")
        self.replay_buffer_checkbox.setChecked(self.settings["replay_buffer"])
        self.replay_buffer_checkbox.stateChanged.connect(self.toggle_replay_buffer)
        main_layout.addWidget(self.replay_buffer_checkbox)

        # Buttons for Actions
        self.add_buttons(main_layout)

        self.capture_thread = None

    def load_styles(self):
        style_file = QFile("styles.qss")
        if style_file.open(QFile.ReadOnly):
            stream = QTextStream(style_file)
            self.setStyleSheet(stream.readAll())
            print("QSS file loaded")
        else:
            print("QSS file didn't load!")

    def update_presets(self):
        """Update the preset dropdown based on selected encoder."""
        current_encoder = self.encoder.currentText()
        self.preset.clear()
        self.preset.addItems(self.encoder_presets.get(current_encoder, []))

    def save_settings(self):
        resolution_value = self.resolution_edit.text().strip()
        if not re.match(r"^\d{3,5}x\d{3,5}$", resolution_value):
            self.update_status("❌ Invalid Resolution! Format: 1920x1080", "#FF0000")
            return

        fps_value = self.fps_spin.value()
        if fps_value < 1 or fps_value > 240:
            self.update_status("❌ FPS must be between 1 and 240!", "#FF0000")
            return

        if self.monitor_combo.currentText() not in CaptureThread.list_monitors():
            self.update_status("❌ Invalid Monitor Selected!", "#FF0000")
            return

        settings_dict = {
            "resolution": self.resolution_edit.text().strip(),
            "fps": self.fps_spin.value(),
            "encoder": self.encoder.currentText(),
            "preset": self.preset.currentText(),
            "audio_device": self.audio_input_combo.currentText(),
            "replay_buffer": self.replay_buffer_checkbox.isChecked(),
            "monitor": self.monitor_combo.currentText(),
        }
        settings.save_settings(settings_dict)
        self.update_status("✅ Settings saved!", "#4CAF50")

    def toggle_replay_buffer(self, state):
        self.settings["replay_buffer"] = state == Qt.Checked
        settings.save_settings(self.settings)
        self.update_status("🔄 Replay buffer updated")

    def add_buttons(self, main_layout):
        buttons_layout = QHBoxLayout()

        save_button = QPushButton("💾 Save Settings")
        save_button.clicked.connect(self.save_settings)
        buttons_layout.addWidget(save_button)

        record_button = QPushButton("🎥 Start Recording")
        record_button.clicked.connect(self.start_recording)
        buttons_layout.addWidget(record_button)

        stop_button = QPushButton("🛑 Stop Recording")
        stop_button.clicked.connect(self.stop_recording)
        buttons_layout.addWidget(stop_button)

        clip_button = QPushButton("✂️ Clip Last 30s")
        clip_button.clicked.connect(self.clip_last_30_seconds)
        buttons_layout.addWidget(clip_button)

        main_layout.addLayout(buttons_layout)

    def update_status(self, message, color="#E0E0E0"):
        self.status_bar.setText(message)
        self.status_bar.setStyleSheet(f"color: {color}; background-color: #333; padding: 10px; border-radius: 5px;")

    def start_recording(self):
        if self.capture_thread and self.capture_thread.is_alive():
            print("⚠️ Already recording!")
            return

        self.capture_thread = CaptureThread(
            resolution=self.resolution_edit.text().strip(),
            fps=self.fps_spin.value(),
            encoder=self.encoder.currentText(),
            preset=self.preset.currentText(),
            mode="manual",
            audio_input=f"audio={self.audio_input_combo.currentText()}",
            audio_output=f"audio={self.audio_output_combo.currentText()}",
            monitor=self.monitor_combo.currentText(),
        )

        self.capture_thread.start()
        self.update_status("🔴 Recording...", "#DC143C")

    def stop_recording(self):
        if self.capture_thread and self.capture_thread.is_alive():
            self.capture_thread.stop_recording()
            self.capture_thread.join()
            self.capture_thread = None
            self.update_status("✅ Recording Stopped", "#4CAF50")

    def clip_last_30_seconds(self):
        clip_thread = CaptureThread(
            resolution=self.resolution_edit.text().strip(),
            fps=self.fps_spin.value(),
            encoder=self.encoder.currentText(),
            preset=self.preset.currentText(),
            mode="clip",
            audio_input=f"audio={self.audio_input_combo.currentText()}",
            audio_output=f"audio={self.audio_output_combo.currentText()}",
            monitor=self.monitor_combo.currentText(),
        )

        clip_thread.start()
        self.update_status("🎬 Clip Captured!", "#FFA500")
